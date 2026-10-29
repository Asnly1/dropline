from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from pathlib import Path

from .config import Settings
from .documents import (
    ensure_entries_file,
    list_json_files,
    load_entries,
    load_json_file,
    write_json_file,
)
from .models import CandidateMemory, ConflictNote, ManagedMemoryEntry, Stage1Result, Stage2Result
from .utils import hash_text


LOGGER = logging.getLogger(__name__)

MEMORY_STATUS_ACTIVE = "active"
MEMORY_STATUS_SUPERSEDED = "superseded"
SUPPRESSED_FILENAME = "suppressed.json"
MEMORY_FILE_KEYS = ("user_profile", "relationship_memory", "active_arc")
ACTIVE_ARC_LIMIT = 2

USER_PROFILE_OBSERVATION_DAYS = 90
ACTIVE_ARC_OBSERVATION_DAYS = 7
USER_PROFILE_MIN_CONFIDENCE = 0.85
RELATIONSHIP_MIN_CONFIDENCE = 0.8
ACTIVE_ARC_MIN_CONFIDENCE = 0.75
# docs/memory_promotion_rules.md §7: how many entries one inbound batch may add per type.
PER_BATCH_PROMOTION_LIMITS = {"user_profile": 2, "relationship_memory": 1, "active_arc": 1}

# The user asking, in their own words, to have something remembered. Negated forms
# ("不要记住", "I don't remember") and plain recollection ("我记得小时候", "Do you remember")
# are excluded; only imperative uses count.
EXPLICIT_REQUEST_PATTERN = re.compile(
    r"(?<!不要)(?<!别)(?<!別)(?<!不)(?:记住|記住)"
    r"|(?:请|請|要|一定要)(?:记得|記得)|(?:记得|記得)(?:我|以后|以後)"
    r"|(?:别|別)忘(?:了|记|記)"
    r"|以[后後](?:都|就)?(?:叫|称呼|稱呼)我"
    r"|(?<!i )(?<!you )(?<!don't )(?<!do not )(?<!can't )(?<!cannot )(?<!not )remember\b"
    r"|don'?t forget|do not forget|keep in mind"
    r"|call me (?!when\b|if\b|back\b|later\b|at\b|on\b|after\b|once\b|whenever\b|tomorrow\b|tonight\b|now\b)\S",
    re.IGNORECASE,
)
# The user correcting something they (or the character) said earlier. Strong markers count on
# their own; weak ones ("其实", "actually") only together with a negation or a back-reference.
CORRECTION_PATTERN = re.compile(
    r"我说错了|说错了|我說錯了|更正|纠正|糾正|不是.{0,12}而是|i misspoke|correction|to be clear|i meant",
    re.IGNORECASE,
)
WEAK_CORRECTION_PATTERN = re.compile(r"其实|其實|actually", re.IGNORECASE)
BACK_REFERENCE_PATTERN = re.compile(
    r"之前|上次|刚才|剛才|以前|上封信|上一封|earlier|before|last time|previously|last letter",
    re.IGNORECASE,
)
# Wording that marks a statement as about right now rather than a stable fact.
TRANSIENT_PATTERN = re.compile(
    r"今天|今晚|今早|今儿|刚才|刚刚|这几天|这两天|这周|本周|这个星期|好累|好烦|累死|烦死|崩溃"
    r"|today|tonight|this morning|just now|this week|these days|exhausted",
    re.IGNORECASE,
)
# Sentence boundaries: CJK and ASCII terminators, plus an ASCII period that is not inside a number.
SENTENCE_SPLIT_PATTERN = re.compile(r"[。！？!?；;\n\r]+|(?<!\d)\.(?!\d)")
SOURCE_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LATIN_WORD_PATTERN = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
CJK_CHAR_PATTERN = re.compile(r"[㐀-鿿]")
GROUNDING_NGRAM = 3
GROUNDING_COVERAGE_THRESHOLD = 0.8
REQUEST_OVERLAP_THRESHOLD = 0.5
MIN_EVIDENCE_CJK_CHARS = 4
MIN_EVIDENCE_LATIN_WORDS = 2
SAME_MEMORY_MIN_CONTAINMENT_LENGTH = 8

# Negated phrasing -> affirmative phrasing. Longer tokens come first so that "不喜欢" is
# rewritten as a unit before any shorter token could split it. A bare "不" is intentionally
# absent: it sits inside too many ordinary words ("不确定", "不错").
NEGATION_MAP: tuple[tuple[str, str], ...] = (
    ("再也不", ""),
    ("不喜欢", "喜欢"),
    ("不需要", "需要"),
    ("不愿意", "愿意"),
    ("不希望", "希望"),
    ("不打算", "打算"),
    ("不习惯", "习惯"),
    ("不想", "想"),
    ("不是", "是"),
    ("没有", "有"),
    ("不再", ""),
    ("并非", ""),
    ("从不", ""),
    ("从未", ""),
    ("不爱", "爱"),
    ("不会", "会"),
    ("不能", "能"),
    ("不吃", "吃"),
    ("不敢", "敢"),
    ("没", ""),
    ("no longer ", ""),
    ("does not ", ""),
    ("do not ", ""),
    ("doesn't ", ""),
    ("don't ", ""),
    ("is not ", "is "),
    ("isn't ", "is "),
    ("never ", ""),
    ("stopped ", ""),
    ("not ", ""),
)
NEGATION_TOKENS = tuple(token for token, _ in NEGATION_MAP)
# "watches" -> "watch", then "likes" -> "like" / "cats" -> "cat"; stems shorter than 3 letters are left alone.
LATIN_ES_PATTERN = re.compile(r"\b([a-z]+(?:s|x|z|ch|sh))es\b")
LATIN_S_PATTERN = re.compile(r"\b([a-z]{3,})s\b")


def bootstrap_session_memory(settings: Settings) -> None:
    ensure_entries_file(settings.documents.user_profile)
    ensure_entries_file(settings.documents.relationship_memory)
    ensure_entries_file(settings.documents.active_arc)
    ensure_entries_file(settings.documents.conflict_notes)
    ensure_entries_file(suppressed_memory_path(settings))
    settings.documents.user_episode_dir.mkdir(parents=True, exist_ok=True)
    settings.documents.character_episode_dir.mkdir(parents=True, exist_ok=True)


def suppressed_memory_path(settings: Settings) -> Path:
    return settings.documents.user_profile.parent / SUPPRESSED_FILENAME


def evidence_is_grounded(evidence: str, texts: Sequence[str]) -> bool:
    """Return True when the quoted evidence can be found in the user's own text.

    Exact containment after normalization counts anywhere in the text. A near-verbatim quote is
    accepted only against a single sentence whose negation state matches the quote, so a
    paraphrase that drops a "不" or a "don't" is rejected. Very short quotes never count.
    """
    if not _evidence_long_enough(evidence):
        return False
    needle = _normalize_for_grounding(evidence)
    for text in texts:
        haystack = _normalize_for_grounding(text)
        if needle and needle in haystack:
            return True
        for sentence in _sentences(text):
            if _fuzzy_sentence_match(evidence, sentence):
                return True
    return False


def user_explicitly_requested(candidate: CandidateMemory, texts: Sequence[str]) -> bool:
    """True when a sentence the user wrote asks to remember this and quotes the candidate's evidence."""
    return any(_marked_sentence_matches(candidate, sentence) for sentence in _request_sentences(texts))


def user_corrected(candidate: CandidateMemory, texts: Sequence[str]) -> bool:
    """True when a sentence the user wrote corrects something and quotes the candidate's evidence."""
    return any(_marked_sentence_matches(candidate, sentence) for sentence in _correction_sentences(texts))


class MemoryManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def bootstrap(self) -> None:
        bootstrap_session_memory(self.settings)

    def recent_user_episodes(self, *, days: int, now: datetime) -> list[dict]:
        return self._recent_episode_payloads(self.settings.documents.user_episode_dir, days=days, now=now)

    def recent_character_episodes(self, *, days: int, now: datetime) -> list[dict]:
        return self._recent_episode_payloads(self.settings.documents.character_episode_dir, days=days, now=now)

    def active_entries_text(self, path: Path) -> str:
        """Render a memory file for a prompt, leaving superseded entries out."""
        if not path.exists():
            return ""
        entries = [item for item in load_entries(path) if _entry_status(item) == MEMORY_STATUS_ACTIVE]
        return json.dumps(entries, ensure_ascii=False, indent=2) + "\n"

    def write_user_side(
        self,
        *,
        stage1_result: Stage1Result,
        batch_id: str,
        now: datetime,
        source_message_ids: list[str],
        source_dates: list[str],
        inbound_texts: Sequence[str],
    ) -> None:
        """Record the user's letters as an episode and promote qualifying candidates.

        ``source_dates`` and ``inbound_texts`` are aligned per letter (an unknown date may be
        an empty string); the dates stored on entries drop the empty ones.
        """
        self.bootstrap()
        letters = _letters(source_dates, inbound_texts, now)
        known_dates = [date for date in source_dates if date]
        self._append_episode(
            directory=self.settings.documents.user_episode_dir,
            batch_id=batch_id,
            now=now,
            episode=self._build_user_episode_payload(stage1_result),
            metadata={
                "memory_observations": [
                    {
                        "content": item.content,
                        "type": item.type,
                    }
                    for item in stage1_result.candidate_memories
                ],
                "open_loops": list(stage1_result.open_loops),
            },
            source_message_ids=source_message_ids,
            source_dates=known_dates,
        )
        promotable = [
            candidate for candidate in stage1_result.candidate_memories if candidate.type in PER_BATCH_PROMOTION_LIMITS
        ]
        promotable.sort(key=lambda candidate: candidate.confidence, reverse=True)
        written: dict[str, int] = {}
        for candidate in promotable:
            limit = PER_BATCH_PROMOTION_LIMITS[candidate.type]
            if written.get(candidate.type, 0) >= limit:
                LOGGER.info(
                    "Skipping %s candidate beyond the per-letter limit of %s: %s",
                    candidate.type,
                    limit,
                    candidate.content,
                )
                continue
            if self._promote_candidate(
                candidate,
                batch_id=batch_id,
                source_message_ids=source_message_ids,
                letters=letters,
                now=now,
            ):
                written[candidate.type] = written.get(candidate.type, 0) + 1

    def write_character_side(self, *, stage2_result: Stage2Result, batch_id: str, now: datetime) -> None:
        self.bootstrap()
        self._append_episode(
            directory=self.settings.documents.character_episode_dir,
            batch_id=batch_id,
            now=now,
            episode=self._build_character_episode_payload(stage2_result),
            source_message_ids=[batch_id],
            source_dates=[now.date().isoformat()],
        )
        self._apply_active_arc_suggestion(stage2_result, batch_id=batch_id, now=now)

    # ------------------------------------------------------------------ manual management
    def memory_path(self, file_key: str) -> Path:
        if file_key not in MEMORY_FILE_KEYS:
            raise ValueError(f"Unknown memory file: {file_key}")
        return getattr(self.settings.documents, file_key)

    def list_entries(self, file_key: str) -> list[dict]:
        path = self.memory_path(file_key)
        return [dict(item) for item in load_entries(path) if isinstance(item, dict)] if path.exists() else []

    def add_manual_entry(self, file_key: str, *, content: str, why_it_matters: str = "", now: datetime) -> dict:
        path = self.memory_path(file_key)
        content = content.strip()
        if not content:
            raise ValueError("Memory content must not be empty.")
        self.bootstrap()
        entries = self._load_memory_entries(path)
        for entry in entries:
            if entry.status == MEMORY_STATUS_ACTIVE and self._same_memory(entry.content, content):
                return entry.to_dict()
        if file_key == "active_arc" and sum(1 for item in entries if item.status == MEMORY_STATUS_ACTIVE) >= ACTIVE_ARC_LIMIT:
            raise ValueError(f"active_arc holds at most {ACTIVE_ARC_LIMIT} entries; archive one first.")
        self._remove_suppression_for(content)
        candidate = CandidateMemory(
            content=content,
            type=file_key,
            confidence=1.0,
            durability="stable" if file_key != "active_arc" else "short",
            evidence="manual entry",
            why_it_matters=why_it_matters.strip(),
            source_date=now.date().isoformat(),
        )
        entry = self._append_or_revive(
            entries,
            candidate,
            source_message_ids=["manual"],
            source_dates=[now.date().isoformat()],
            now=now,
        )
        if why_it_matters.strip():
            entry.why_it_matters = why_it_matters.strip()
        # A statement the user adds by hand wins over any active memory that contradicts it.
        for other in entries:
            if other is entry or other.status != MEMORY_STATUS_ACTIVE:
                continue
            if self._is_conflict(other.content, entry.content):
                other.status = MEMORY_STATUS_SUPERSEDED
                other.superseded_by = entry.key
                other.updated_at = now.isoformat()
        write_json_file(path, [item.to_dict() for item in entries])
        return entry.to_dict()

    def update_entry(
        self,
        file_key: str,
        key: str,
        *,
        content: str | None = None,
        why_it_matters: str | None = None,
        now: datetime,
    ) -> dict | None:
        path = self.memory_path(file_key)
        entries = self._load_memory_entries(path)
        for entry in entries:
            if entry.key != key:
                continue
            if content is not None:
                stripped = content.strip()
                if not stripped:
                    raise ValueError("Memory content must not be empty.")
                entry.content = stripped
                entry.evidence = _merged_unique(entry.evidence, ["manual edit"])
            if why_it_matters is not None:
                entry.why_it_matters = why_it_matters.strip()
            entry.updated_at = now.isoformat()
            write_json_file(path, [item.to_dict() for item in entries])
            return entry.to_dict()
        return None

    def delete_entry(self, file_key: str, key: str, *, suppress: bool = True, now: datetime) -> bool:
        """Remove an entry; by default also leave a tombstone so Stage 1 cannot write it back.

        Clearing out an already superseded entry never leaves a tombstone: the statement is not
        something the user rejected, only history they no longer want to see.
        """
        path = self.memory_path(file_key)
        entries = self._load_memory_entries(path)
        remaining = [entry for entry in entries if entry.key != key]
        if len(remaining) == len(entries):
            return False
        removed = next(entry for entry in entries if entry.key == key)
        write_json_file(path, [item.to_dict() for item in remaining])
        if suppress and removed.status == MEMORY_STATUS_ACTIVE:
            self.suppress(removed.content, reason=f"deleted from {file_key}", now=now)
        return True

    def suppress(self, content: str, *, reason: str, now: datetime) -> dict:
        path = suppressed_memory_path(self.settings)
        entries = self.list_suppressed()
        normalized = _normalize_memory(content)
        for item in entries:
            if str(item.get("normalized", "")) == normalized:
                return dict(item)
        record = {
            "key": hash_text(f"suppressed:{normalized}", prefix="suppressed-"),
            "content": content.strip(),
            "normalized": normalized,
            "reason": reason,
            "created_at": now.isoformat(),
        }
        entries.append(record)
        write_json_file(path, entries)
        return record

    def list_suppressed(self) -> list[dict]:
        path = suppressed_memory_path(self.settings)
        return [dict(item) for item in load_entries(path) if isinstance(item, dict)] if path.exists() else []

    def unsuppress(self, key: str) -> bool:
        path = suppressed_memory_path(self.settings)
        entries = self.list_suppressed()
        remaining = [item for item in entries if str(item.get("key", "")) != key]
        if len(remaining) == len(entries):
            return False
        write_json_file(path, remaining)
        return True

    def is_suppressed(self, content: str) -> bool:
        for item in self.list_suppressed():
            if self._same_memory(str(item.get("content", "")), content):
                return True
        return False

    def list_conflicts(self) -> list[dict]:
        path = self.settings.documents.conflict_notes
        return [dict(item) for item in load_entries(path) if isinstance(item, dict)] if path.exists() else []

    def resolve_conflict(self, key: str, *, choice: str, now: datetime) -> bool:
        """Settle a conflict note by hand: keep the existing memory or take the new one."""
        if choice not in {"keep_existing", "take_new"}:
            raise ValueError("choice must be 'keep_existing' or 'take_new'")
        notes_path = self.settings.documents.conflict_notes
        notes = [ConflictNote.from_dict(item) for item in self.list_conflicts()]
        note = next((item for item in notes if item.key == key), None)
        if note is None:
            return False
        if choice == "take_new":
            path = self._path_for_memory_type(note.memory_type)
            entries = self._load_memory_entries(path)
            existing = next(
                (
                    entry
                    for entry in entries
                    if entry.status == MEMORY_STATUS_ACTIVE and self._same_memory(entry.content, note.existing_content)
                ),
                None,
            )
            candidate = CandidateMemory(
                content=note.new_content,
                type=note.memory_type,
                confidence=1.0,
                durability="stable",
                evidence="resolved by user",
                why_it_matters=existing.why_it_matters if existing else "",
                source_date=now.date().isoformat(),
            )
            if existing is not None:
                self._supersede(
                    entries,
                    existing,
                    candidate,
                    source_message_ids=note.source_message_ids,
                    source_dates=[now.date().isoformat()],
                    now=now,
                )
            else:
                self._append_or_revive(
                    entries,
                    candidate,
                    source_message_ids=note.source_message_ids,
                    source_dates=[now.date().isoformat()],
                    now=now,
                )
            write_json_file(path, [item.to_dict() for item in entries])
        write_json_file(notes_path, [item.to_dict() for item in notes if item.key != key])
        return True

    # ------------------------------------------------------------------ promotion
    def _build_user_episode_payload(self, stage1_result: Stage1Result) -> dict:
        return stage1_result.user_digest.to_dict()

    def _build_character_episode_payload(self, stage2_result: Stage2Result) -> dict:
        return stage2_result.character_episode.to_dict()

    def _promote_candidate(
        self,
        candidate: CandidateMemory,
        *,
        batch_id: str,
        source_message_ids: list[str],
        letters: Sequence[tuple[str, str]],
        now: datetime,
    ) -> bool:
        """Decide whether one Stage 1 candidate reaches long-term memory. Returns True on a write."""
        grounded = [(date, text) for date, text in letters if evidence_is_grounded(candidate.evidence, [text])]
        if not grounded:
            LOGGER.info(
                "Rejecting %s candidate whose evidence is not found in the inbound mail: %s",
                candidate.type,
                candidate.content,
            )
            return False
        if self.is_suppressed(candidate.content):
            LOGGER.info("Rejecting %s candidate the user chose to forget: %s", candidate.type, candidate.content)
            return False
        grounded_texts = [text for _, text in grounded]
        grounded_dates = sorted({date for date, _ in grounded})
        if candidate.type == "active_arc":
            if not self._qualifies_active_arc(candidate, now=now, batch_id=batch_id, current_dates=grounded_dates):
                return False
            return self._upsert_active_arc(
                candidate, source_message_ids=source_message_ids, source_dates=grounded_dates, now=now
            )
        explicit = user_explicitly_requested(candidate, grounded_texts)
        corrected = user_corrected(candidate, grounded_texts)
        if not self._passes_hard_gate(candidate, explicit=explicit, corrected=corrected):
            return False
        path = self._path_for_memory_type(candidate.type)
        entries = self._load_memory_entries(path)
        active = [entry for entry in entries if entry.status == MEMORY_STATUS_ACTIVE]
        conflicting = next((entry for entry in active if self._is_conflict(entry.content, candidate.content)), None)
        if conflicting is not None:
            resolved = self._append_conflict(
                entry=conflicting,
                candidate=candidate,
                source_message_ids=source_message_ids,
                occurrence_date=grounded_dates[0],
                now=now,
                force=corrected,
            )
            if not resolved:
                return False
            self._supersede(
                entries,
                conflicting,
                candidate,
                source_message_ids=source_message_ids,
                source_dates=grounded_dates,
                now=now,
            )
            write_json_file(path, [item.to_dict() for item in entries])
            return True
        same = next((entry for entry in active if self._same_memory(entry.content, candidate.content)), None)
        if same is not None:
            same.source_message_ids = _merged_unique(same.source_message_ids, source_message_ids)
            same.source_dates = _merged_unique(same.source_dates, grounded_dates)
            same.evidence = _merged_unique(same.evidence, [candidate.evidence])
            same.updated_at = now.isoformat()
            write_json_file(path, [item.to_dict() for item in entries])
            return False
        if not (explicit or corrected):
            observed_dates = self._distinct_observation_dates(
                candidate,
                days=USER_PROFILE_OBSERVATION_DAYS,
                now=now,
                batch_id=batch_id,
                current_dates=grounded_dates,
            )
            if observed_dates < 2:
                return False
        self._append_or_revive(
            entries, candidate, source_message_ids=source_message_ids, source_dates=grounded_dates, now=now
        )
        write_json_file(path, [item.to_dict() for item in entries])
        return True

    def _passes_hard_gate(self, candidate: CandidateMemory, *, explicit: bool, corrected: bool) -> bool:
        """Model-reported fields are necessary, never sufficient.

        A transient or low-confidence candidate cannot enter long-term memory, and cannot
        override an existing entry either, whatever the letter says around it.
        """
        if candidate.durability == "transient":
            return False
        if explicit or corrected:
            return True
        if candidate.type == "user_profile":
            if candidate.confidence < USER_PROFILE_MIN_CONFIDENCE or candidate.durability != "stable":
                return False
            return not TRANSIENT_PATTERN.search(candidate.content)
        return candidate.confidence >= RELATIONSHIP_MIN_CONFIDENCE

    def _qualifies_active_arc(
        self,
        candidate: CandidateMemory,
        *,
        now: datetime,
        batch_id: str,
        current_dates: Sequence[str],
    ) -> bool:
        if candidate.confidence < ACTIVE_ARC_MIN_CONFIDENCE or candidate.durability == "transient":
            return False
        active = [
            entry
            for entry in self._load_memory_entries(self.settings.documents.active_arc)
            if entry.status == MEMORY_STATUS_ACTIVE
        ]
        if any(self._same_memory(entry.content, candidate.content) for entry in active):
            return True
        observed_dates = self._distinct_observation_dates(
            candidate,
            days=ACTIVE_ARC_OBSERVATION_DAYS,
            now=now,
            batch_id=batch_id,
            current_dates=current_dates,
        )
        return observed_dates >= 2

    def _distinct_observation_dates(
        self,
        candidate: CandidateMemory,
        *,
        days: int,
        now: datetime,
        batch_id: str,
        current_dates: Iterable[str],
    ) -> int:
        """Count the distinct mail dates on which the user has stated this.

        ``current_dates`` are the dates of the letters in this batch where the evidence was
        actually found; earlier batches contribute the dates recorded on their episodes.
        """
        dates: set[str] = {date for date in current_dates if date} or {now.date().isoformat()}
        for entry in self._recent_episode_entries(self.settings.documents.user_episode_dir, days=days, now=now):
            if str(entry.get("batch_id", "")) == batch_id:
                continue
            observations = entry.get("memory_observations", [])
            if not isinstance(observations, list):
                continue
            observed_here = any(
                str(observed.get("type", "")) == candidate.type
                and self._same_memory(str(observed.get("content", "")), candidate.content)
                for observed in observations
                if isinstance(observed, dict)
            )
            if not observed_here:
                continue
            raw_dates = entry.get("source_dates", [])
            entry_dates = [str(date) for date in raw_dates if date] if isinstance(raw_dates, list) else []
            if not entry_dates:
                entry_dates = [str(entry.get("_file_date", ""))]
            dates.update(date for date in entry_dates if date)
        return len(dates)

    def _append_episode(
        self,
        *,
        directory: Path,
        batch_id: str,
        now: datetime,
        episode: dict,
        metadata: dict | None = None,
        source_message_ids: list[str],
        source_dates: list[str],
    ) -> None:
        path = directory / f"{now.date().isoformat()}.json"
        entries = load_json_file(path, default=[])
        if not isinstance(entries, list):
            raise ValueError(f"Episode file must contain a JSON array: {path}")
        if any(isinstance(entry, dict) and entry.get("batch_id") == batch_id for entry in entries):
            return
        entry = {
            "entry_id": f"episode:{batch_id}",
            "batch_id": batch_id,
            "created_at": now.isoformat(),
            "source_message_ids": source_message_ids,
            "source_dates": source_dates,
            "episode": episode,
        }
        if metadata:
            entry.update(metadata)
        entries.append(entry)
        write_json_file(path, entries)

    def _new_entry(
        self,
        candidate: CandidateMemory,
        *,
        source_message_ids: list[str],
        source_dates: list[str],
        now: datetime,
    ) -> ManagedMemoryEntry:
        return ManagedMemoryEntry(
            key=hash_text(f"{candidate.type}:{candidate.content}", prefix=f"{candidate.type}-"),
            content=candidate.content,
            memory_type=candidate.type,
            confidence=candidate.confidence,
            durability=candidate.durability,
            evidence=[candidate.evidence],
            source_dates=[date for date in source_dates if date],
            source_message_ids=source_message_ids,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
            why_it_matters=candidate.why_it_matters,
        )

    def _append_or_revive(
        self,
        entries: list[ManagedMemoryEntry],
        candidate: CandidateMemory,
        *,
        source_message_ids: list[str],
        source_dates: list[str],
        now: datetime,
    ) -> ManagedMemoryEntry:
        """Add the candidate, reviving a retired entry with the same key instead of duplicating it."""
        key = hash_text(f"{candidate.type}:{candidate.content}", prefix=f"{candidate.type}-")
        existing = next((entry for entry in entries if entry.key == key), None)
        if existing is None:
            entry = self._new_entry(candidate, source_message_ids=source_message_ids, source_dates=source_dates, now=now)
            entries.append(entry)
            return entry
        existing.status = MEMORY_STATUS_ACTIVE
        existing.superseded_by = ""
        existing.source_message_ids = _merged_unique(existing.source_message_ids, source_message_ids)
        existing.source_dates = _merged_unique(existing.source_dates, source_dates)
        existing.evidence = _merged_unique(existing.evidence, [candidate.evidence])
        existing.updated_at = now.isoformat()
        return existing

    def _supersede(
        self,
        entries: list[ManagedMemoryEntry],
        old: ManagedMemoryEntry,
        candidate: CandidateMemory,
        *,
        source_message_ids: list[str],
        source_dates: list[str],
        now: datetime,
    ) -> ManagedMemoryEntry:
        """Retire `old` in favour of the candidate, keeping the old entry for audit."""
        new_entry = self._append_or_revive(
            entries, candidate, source_message_ids=source_message_ids, source_dates=source_dates, now=now
        )
        if new_entry is old:
            return old
        old.status = MEMORY_STATUS_SUPERSEDED
        old.superseded_by = new_entry.key
        old.updated_at = now.isoformat()
        return new_entry

    def _upsert_active_arc(
        self,
        candidate: CandidateMemory,
        *,
        source_message_ids: list[str],
        source_dates: list[str],
        now: datetime,
    ) -> bool:
        entries = self._load_memory_entries(self.settings.documents.active_arc)
        active = [entry for entry in entries if entry.status == MEMORY_STATUS_ACTIVE]
        for entry in active:
            if self._same_memory(entry.content, candidate.content):
                entry.source_message_ids = _merged_unique(entry.source_message_ids, source_message_ids)
                entry.source_dates = _merged_unique(entry.source_dates, source_dates)
                entry.updated_at = now.isoformat()
                write_json_file(self.settings.documents.active_arc, [item.to_dict() for item in entries])
                return False
        if len(active) >= ACTIVE_ARC_LIMIT:
            LOGGER.info("active_arc is full; not adding: %s", candidate.content)
            return False
        self._append_or_revive(entries, candidate, source_message_ids=source_message_ids, source_dates=source_dates, now=now)
        write_json_file(self.settings.documents.active_arc, [item.to_dict() for item in entries])
        return True

    def _apply_active_arc_suggestion(self, stage2_result: Stage2Result, *, batch_id: str, now: datetime) -> None:
        suggestion = stage2_result.active_arc_update_suggestion
        if suggestion.action == "none":
            return
        content = suggestion.content.strip()
        entries = self._load_memory_entries(self.settings.documents.active_arc)
        active = [entry for entry in entries if entry.status == MEMORY_STATUS_ACTIVE]
        retired = [entry for entry in entries if entry.status != MEMORY_STATUS_ACTIVE]
        matching_index = next(
            (index for index, item in enumerate(active) if content and self._same_memory(item.content, content)),
            None,
        )
        if suggestion.action == "archive":
            if matching_index is not None:
                active.pop(matching_index)
                write_json_file(self.settings.documents.active_arc, [item.to_dict() for item in active + retired])
            return
        if suggestion.action == "keep":
            if matching_index is not None:
                entry = active[matching_index]
                entry.source_message_ids = _merged_unique(entry.source_message_ids, [batch_id])
                entry.source_dates = _merged_unique(entry.source_dates, [now.date().isoformat()])
                entry.updated_at = now.isoformat()
                write_json_file(self.settings.documents.active_arc, [item.to_dict() for item in active + retired])
            return

        if suggestion.action != "update" or not content:
            return
        if self.is_suppressed(content):
            LOGGER.info("Ignoring active_arc update the user chose to forget: %s", content)
            return

        if matching_index is not None:
            entry = active[matching_index]
            entry.content = content
            entry.source_message_ids = _merged_unique(entry.source_message_ids, [batch_id])
            entry.source_dates = _merged_unique(entry.source_dates, [now.date().isoformat()])
            entry.evidence = _merged_unique(entry.evidence, ["stage2 active arc suggestion"])
            entry.updated_at = now.isoformat()
        else:
            active.append(
                ManagedMemoryEntry(
                    key=hash_text(f"active_arc:{content}", prefix="active-"),
                    content=content,
                    memory_type="active_arc",
                    confidence=0.75,
                    durability="short",
                    evidence=["stage2 active arc suggestion"],
                    source_dates=[now.date().isoformat()],
                    source_message_ids=[batch_id],
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                    why_it_matters="Stage 2 continuity suggestion.",
                )
            )
        active = sorted(active, key=lambda item: datetime.fromisoformat(item.updated_at), reverse=True)[:ACTIVE_ARC_LIMIT]
        write_json_file(self.settings.documents.active_arc, [item.to_dict() for item in active + retired])

    def _append_conflict(
        self,
        *,
        entry: ManagedMemoryEntry,
        candidate: CandidateMemory,
        source_message_ids: list[str],
        occurrence_date: str,
        now: datetime,
        force: bool = False,
    ) -> bool:
        """Record a conflict; return True once the new value may replace the old one.

        Replacement happens when the user explicitly corrected themselves (`force`) or when
        the same conflict has been observed on two distinct mail dates.
        """
        notes = [ConflictNote.from_dict(item) for item in self.list_conflicts()]
        key = hash_text(f"{entry.content}:{candidate.content}", prefix="conflict-")
        existing_note = next((note for note in notes if note.key == key), None)
        if force:
            LOGGER.info("User corrected a long-term memory; replacing: %s -> %s", entry.content, candidate.content)
            if existing_note is not None:
                notes = [note for note in notes if note.key != key]
                write_json_file(self.settings.documents.conflict_notes, [note.to_dict() for note in notes])
            return True
        if existing_note is None:
            notes.append(
                ConflictNote(
                    key=key,
                    memory_type=entry.memory_type,
                    existing_content=entry.content,
                    new_content=candidate.content,
                    reason="Detected negation-style conflict between existing and new long-term memory.",
                    source_date=occurrence_date,
                    source_message_ids=source_message_ids,
                    created_at=now.isoformat(),
                    occurrence_dates=[occurrence_date],
                )
            )
            write_json_file(self.settings.documents.conflict_notes, [note.to_dict() for note in notes])
            return False

        if occurrence_date not in existing_note.occurrence_dates:
            existing_note.occurrence_dates.append(occurrence_date)
        existing_note.source_message_ids = _merged_unique(existing_note.source_message_ids, source_message_ids)

        resolved = len(existing_note.occurrence_dates) >= 2
        if resolved:
            notes = [note for note in notes if note.key != key]
        write_json_file(self.settings.documents.conflict_notes, [note.to_dict() for note in notes])
        return resolved

    def _recent_episode_payloads(self, directory: Path, *, days: int, now: datetime) -> list[dict]:
        payloads: list[dict] = []
        for item in self._recent_episode_entries(directory, days=days, now=now):
            if "episode" in item:
                payloads.append(dict(item.get("episode", {})))
            elif "payload" in item:
                payloads.append(dict(item.get("payload", {})))
        return payloads

    def _recent_episode_entries(self, directory: Path, *, days: int, now: datetime) -> list[dict]:
        cutoff = now.date() - timedelta(days=days)
        entries_in_range: list[dict] = []
        seen_batch_ids: set[str] = set()
        for path in list_json_files(directory):
            try:
                file_date = datetime.strptime(path.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if file_date < cutoff:
                continue
            entries = load_json_file(path, default=[])
            if not isinstance(entries, list):
                continue
            for item in entries:
                if not isinstance(item, dict):
                    continue
                batch_id = str(item.get("batch_id", ""))
                if batch_id and batch_id in seen_batch_ids:
                    continue
                if batch_id:
                    seen_batch_ids.add(batch_id)
                copied = dict(item)
                copied["_file_date"] = path.stem
                entries_in_range.append(copied)
        return entries_in_range

    def _path_for_memory_type(self, memory_type: str) -> Path:
        if memory_type == "user_profile":
            return self.settings.documents.user_profile
        if memory_type == "relationship_memory":
            return self.settings.documents.relationship_memory
        if memory_type == "active_arc":
            return self.settings.documents.active_arc
        raise ValueError(f"Unsupported long-term memory type: {memory_type}")

    def _remove_suppression_for(self, content: str) -> None:
        path = suppressed_memory_path(self.settings)
        if not path.exists():
            return
        entries = self.list_suppressed()
        remaining = [item for item in entries if not self._same_memory(str(item.get("content", "")), content)]
        if len(remaining) != len(entries):
            write_json_file(path, remaining)

    def _load_memory_entries(self, path: Path) -> list[ManagedMemoryEntry]:
        raw_entries = load_entries(path) if path.exists() else []
        return [ManagedMemoryEntry.from_dict(item) for item in raw_entries if isinstance(item, dict)]

    def _same_memory(self, left: str, right: str) -> bool:
        normalized_left = _normalize_memory(left)
        normalized_right = _normalize_memory(right)
        if normalized_left == normalized_right:
            return True
        if len(normalized_left) >= SAME_MEMORY_MIN_CONTAINMENT_LENGTH and normalized_left in normalized_right:
            return True
        if len(normalized_right) >= SAME_MEMORY_MIN_CONTAINMENT_LENGTH and normalized_right in normalized_left:
            return True
        return False

    def _is_conflict(self, existing_content: str, new_content: str) -> bool:
        if _has_negation(existing_content) == _has_negation(new_content):
            return False
        existing_norm = _conflict_form(existing_content)
        new_norm = _conflict_form(new_content)
        if not existing_norm or not new_norm:
            return False
        if existing_norm == new_norm:
            return True
        shorter, longer = sorted((existing_norm, new_norm), key=len)
        return len(shorter) >= SAME_MEMORY_MIN_CONTAINMENT_LENGTH and shorter in longer


def _entry_status(item: dict) -> str:
    return str(item.get("status", MEMORY_STATUS_ACTIVE)) or MEMORY_STATUS_ACTIVE


def _letters(source_dates: Sequence[str], inbound_texts: Sequence[str], now: datetime) -> list[tuple[str, str]]:
    """Pair each letter's text with its mail date; unknown dates fall back to the run date."""
    today = now.date().isoformat()
    texts = list(inbound_texts)
    dates = list(source_dates)
    if len(dates) != len(texts):
        fallback = next((date for date in dates if date), today)
        return [(fallback, text) for text in texts]
    return [(date or today, text) for date, text in zip(dates, texts)]


def _normalize_memory(value: str) -> str:
    return "".join(value.lower().split())


def _conflict_form(value: str) -> str:
    """Normalize a statement for conflict comparison: drop negation, fold plurals, drop spaces."""
    return _normalize_memory(_stem_latin(_strip_negation(value)))


def _normalize_for_grounding(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).lower()
    return "".join(char for char in folded if char.isalnum())


def _latin_words(value: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", value).lower()
    return LATIN_WORD_PATTERN.findall(folded)


def _cjk_chars(value: str) -> str:
    return "".join(CJK_CHAR_PATTERN.findall(unicodedata.normalize("NFKC", value)))


def _evidence_long_enough(evidence: str) -> bool:
    if len(_latin_words(evidence)) >= MIN_EVIDENCE_LATIN_WORDS:
        return True
    return len(_cjk_chars(evidence)) >= MIN_EVIDENCE_CJK_CHARS


def _ngrams(value: str, size: int) -> set[str]:
    if not value:
        return set()
    if len(value) < size:
        return {value}
    return {value[index : index + size] for index in range(len(value) - size + 1)}


def _ngram_coverage(needle: str, haystack: str) -> float:
    needle_grams = _ngrams(needle, GROUNDING_NGRAM)
    if not needle_grams:
        return 0.0
    haystack_grams = _ngrams(haystack, GROUNDING_NGRAM)
    return sum(1 for gram in needle_grams if gram in haystack_grams) / len(needle_grams)


def _fuzzy_sentence_match(evidence: str, sentence: str) -> bool:
    """Near-verbatim match of a quote against one sentence, with matching negation state."""
    if _has_negation(evidence) != _has_negation(sentence):
        return False
    evidence_words = _latin_words(evidence)
    matched_something = False
    if len(evidence_words) >= MIN_EVIDENCE_LATIN_WORDS:
        sentence_words = set(_latin_words(sentence))
        covered = sum(1 for word in evidence_words if word in sentence_words) / len(evidence_words)
        if covered < GROUNDING_COVERAGE_THRESHOLD:
            return False
        matched_something = True
    evidence_cjk = _cjk_chars(evidence)
    if evidence_cjk:
        sentence_cjk = _cjk_chars(sentence)
        if len(evidence_cjk) < GROUNDING_NGRAM:
            if evidence_cjk not in sentence_cjk:
                return False
        elif _ngram_coverage(evidence_cjk, sentence_cjk) < GROUNDING_COVERAGE_THRESHOLD:
            return False
        matched_something = True
    return matched_something


def _sentences(text: str) -> list[str]:
    return [segment.strip() for segment in SENTENCE_SPLIT_PATTERN.split(text) if segment.strip()]


def _request_sentences(texts: Sequence[str]) -> list[str]:
    return [sentence for text in texts for sentence in _sentences(text) if EXPLICIT_REQUEST_PATTERN.search(sentence)]


def _correction_sentences(texts: Sequence[str]) -> list[str]:
    return [sentence for text in texts for sentence in _sentences(text) if _is_correction_sentence(sentence)]


def _is_correction_sentence(sentence: str) -> bool:
    if CORRECTION_PATTERN.search(sentence):
        return True
    if not WEAK_CORRECTION_PATTERN.search(sentence):
        return False
    return bool(BACK_REFERENCE_PATTERN.search(sentence)) or _has_negation(sentence)


def _marked_sentence_matches(candidate: CandidateMemory, sentence: str) -> bool:
    """The marked sentence must carry the candidate's quoted evidence, not just any topic."""
    probe = _normalize_for_grounding(candidate.evidence)
    normalized_sentence = _normalize_for_grounding(sentence)
    if not probe or not normalized_sentence:
        return False
    if probe in normalized_sentence or normalized_sentence in probe:
        return True
    return (
        _ngram_coverage(probe, normalized_sentence) >= REQUEST_OVERLAP_THRESHOLD
        or _ngram_coverage(normalized_sentence, probe) >= REQUEST_OVERLAP_THRESHOLD
    )


def _has_negation(value: str) -> bool:
    lowered = value.lower()
    return any(token in lowered for token in NEGATION_TOKENS)


def _strip_negation(value: str) -> str:
    stripped = value.lower()
    for token, replacement in NEGATION_MAP:
        stripped = stripped.replace(token, replacement)
    return stripped


def _stem_latin(value: str) -> str:
    """Fold simple plurals and third-person -s so "likes cats" and "like cats" compare equal."""
    return LATIN_S_PATTERN.sub(r"\1", LATIN_ES_PATTERN.sub(r"\1", value))


def _merged_unique(existing: Iterable[str], new_values: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for item in [*existing, *new_values]:
        if item and item not in seen:
            seen.append(item)
    return seen
