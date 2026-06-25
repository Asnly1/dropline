from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

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


def bootstrap_session_memory(settings: Settings) -> None:
    ensure_entries_file(settings.documents.user_profile)
    ensure_entries_file(settings.documents.relationship_memory)
    ensure_entries_file(settings.documents.active_arc)
    ensure_entries_file(settings.documents.conflict_notes)
    settings.documents.user_episode_dir.mkdir(parents=True, exist_ok=True)
    settings.documents.character_episode_dir.mkdir(parents=True, exist_ok=True)


class MemoryManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def bootstrap(self) -> None:
        bootstrap_session_memory(self.settings)

    def recent_user_episodes(self, *, days: int, now: datetime) -> list[dict]:
        return self._recent_episode_payloads(self.settings.documents.user_episode_dir, days=days, now=now)

    def recent_character_episodes(self, *, days: int, now: datetime) -> list[dict]:
        return self._recent_episode_payloads(self.settings.documents.character_episode_dir, days=days, now=now)

    def write_user_side(
        self,
        *,
        stage1_result: Stage1Result,
        batch_id: str,
        now: datetime,
        source_message_ids: list[str],
        source_dates: list[str],
    ) -> None:
        self.bootstrap()
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
                ]
            },
            source_message_ids=source_message_ids,
            source_dates=source_dates,
        )
        for candidate in stage1_result.candidate_memories:
            self._promote_candidate(candidate, source_message_ids=source_message_ids, source_dates=source_dates, now=now)

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

    def _build_user_episode_payload(self, stage1_result: Stage1Result) -> dict:
        return stage1_result.user_digest.to_dict()

    def _build_character_episode_payload(self, stage2_result: Stage2Result) -> dict:
        return stage2_result.character_episode.to_dict()

    def _promote_candidate(
        self,
        candidate: CandidateMemory,
        *,
        source_message_ids: list[str],
        source_dates: list[str],
        now: datetime,
    ) -> None:
        if candidate.type == "user_profile":
            if not self._qualifies_user_profile(candidate, now=now):
                return
            self._upsert_memory_entry(
                self.settings.documents.user_profile,
                candidate=candidate,
                source_message_ids=source_message_ids,
                source_dates=source_dates,
                now=now,
            )
            return
        if candidate.type == "relationship_memory":
            if candidate.confidence < 0.8 or candidate.durability == "transient":
                return
            self._upsert_memory_entry(
                self.settings.documents.relationship_memory,
                candidate=candidate,
                source_message_ids=source_message_ids,
                source_dates=source_dates,
                now=now,
            )
            return
        if candidate.type == "active_arc":
            if not self._qualifies_active_arc(candidate, now=now):
                return
            self._upsert_active_arc(candidate, source_message_ids=source_message_ids, source_dates=source_dates, now=now)

    def _qualifies_user_profile(self, candidate: CandidateMemory, *, now: datetime) -> bool:
        if candidate.confidence < 0.85 or candidate.durability != "stable":
            return False
        if "记住" in candidate.evidence or "长期" in candidate.evidence:
            return True
        return self._count_user_observations(candidate, days=90, now=now) >= 2

    def _qualifies_active_arc(self, candidate: CandidateMemory, *, now: datetime) -> bool:
        if candidate.confidence < 0.75 or candidate.durability == "transient":
            return False
        entries = self._load_memory_entries(self.settings.documents.active_arc)
        if any(self._same_memory(entry.content, candidate.content) for entry in entries):
            return True
        return self._count_user_observations(candidate, days=7, now=now) >= 2

    def _count_user_observations(self, candidate: CandidateMemory, *, days: int, now: datetime) -> int:
        count = 0
        for entry in self._recent_episode_entries(self.settings.documents.user_episode_dir, days=days, now=now):
            for observed in entry.get("memory_observations", []):
                observed_type = str(observed.get("type", ""))
                observed_content = str(observed.get("content", ""))
                if observed_type == candidate.type and self._same_memory(observed_content, candidate.content):
                    count += 1
        return count

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
        if any(entry.get("batch_id") == batch_id for entry in entries):
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

    def _upsert_memory_entry(
        self,
        path: Path,
        *,
        candidate: CandidateMemory,
        source_message_ids: list[str],
        source_dates: list[str],
        now: datetime,
    ) -> None:
        raw_entries = load_entries(path)
        entries = [ManagedMemoryEntry.from_dict(item) for item in raw_entries]
        for entry in entries:
            if self._same_memory(entry.content, candidate.content):
                entry.source_message_ids = _merged_unique(entry.source_message_ids, source_message_ids)
                entry.source_dates = _merged_unique(entry.source_dates, source_dates)
                entry.evidence = _merged_unique(entry.evidence, [candidate.evidence])
                entry.updated_at = now.isoformat()
                write_json_file(path, [item.to_dict() for item in entries])
                return
            if self._is_conflict(entry.content, candidate.content):
                self._append_conflict(entry=entry, candidate=candidate, source_message_ids=source_message_ids, now=now)
                return
        entries.append(
            ManagedMemoryEntry(
                key=hash_text(f"{candidate.type}:{candidate.content}", prefix=f"{candidate.type}-"),
                content=candidate.content,
                memory_type=candidate.type,
                confidence=candidate.confidence,
                durability=candidate.durability,
                evidence=[candidate.evidence],
                source_dates=source_dates,
                source_message_ids=source_message_ids,
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
                why_it_matters=candidate.why_it_matters,
            )
        )
        write_json_file(path, [item.to_dict() for item in entries])

    def _upsert_active_arc(
        self,
        candidate: CandidateMemory,
        *,
        source_message_ids: list[str],
        source_dates: list[str],
        now: datetime,
    ) -> None:
        raw_entries = load_entries(self.settings.documents.active_arc)
        entries = [ManagedMemoryEntry.from_dict(item) for item in raw_entries]
        for entry in entries:
            if self._same_memory(entry.content, candidate.content):
                entry.source_message_ids = _merged_unique(entry.source_message_ids, source_message_ids)
                entry.source_dates = _merged_unique(entry.source_dates, source_dates)
                entry.updated_at = now.isoformat()
                write_json_file(self.settings.documents.active_arc, [item.to_dict() for item in entries])
                return
        if len(entries) >= 2:
            return
        entries.append(
            ManagedMemoryEntry(
                key=hash_text(f"active_arc:{candidate.content}", prefix="active-"),
                content=candidate.content,
                memory_type="active_arc",
                confidence=candidate.confidence,
                durability=candidate.durability,
                evidence=[candidate.evidence],
                source_dates=source_dates,
                source_message_ids=source_message_ids,
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
                why_it_matters=candidate.why_it_matters,
            )
        )
        write_json_file(self.settings.documents.active_arc, [item.to_dict() for item in entries])

    def _apply_active_arc_suggestion(self, stage2_result: Stage2Result, *, batch_id: str, now: datetime) -> None:
        suggestion = stage2_result.active_arc_update_suggestion
        if suggestion.action == "none":
            return
        content = suggestion.content.strip()
        raw_entries = load_entries(self.settings.documents.active_arc)
        entries = [ManagedMemoryEntry.from_dict(item) for item in raw_entries]
        matching_index = next(
            (index for index, item in enumerate(entries) if content and self._same_memory(item.content, content)),
            None,
        )
        if suggestion.action == "archive":
            if matching_index is not None:
                entries.pop(matching_index)
                write_json_file(self.settings.documents.active_arc, [item.to_dict() for item in entries])
            return
        if suggestion.action == "keep":
            if matching_index is not None:
                entry = entries[matching_index]
                entry.source_message_ids = _merged_unique(entry.source_message_ids, [batch_id])
                entry.source_dates = _merged_unique(entry.source_dates, [now.date().isoformat()])
                entry.updated_at = now.isoformat()
                write_json_file(self.settings.documents.active_arc, [item.to_dict() for item in entries])
            return

        if suggestion.action != "update" or not content:
            return

        if matching_index is not None:
            entry = entries[matching_index]
            entry.content = content
            entry.source_message_ids = _merged_unique(entry.source_message_ids, [batch_id])
            entry.source_dates = _merged_unique(entry.source_dates, [now.date().isoformat()])
            entry.evidence = _merged_unique(entry.evidence, ["stage2 active arc suggestion"])
            entry.updated_at = now.isoformat()
        else:
            entries.append(
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
        entries = sorted(entries, key=lambda item: item.updated_at, reverse=True)[:2]
        write_json_file(self.settings.documents.active_arc, [item.to_dict() for item in entries])

    def _append_conflict(
        self,
        *,
        entry: ManagedMemoryEntry,
        candidate: CandidateMemory,
        source_message_ids: list[str],
        now: datetime,
    ) -> None:
        raw_entries = load_entries(self.settings.documents.conflict_notes)
        notes = [ConflictNote.from_dict(item) for item in raw_entries]
        key = hash_text(f"{entry.content}:{candidate.content}", prefix="conflict-")
        if any(note.key == key for note in notes):
            return
        notes.append(
            ConflictNote(
                key=key,
                memory_type=entry.memory_type,
                existing_content=entry.content,
                new_content=candidate.content,
                reason="Detected negation-style conflict between existing and new long-term memory.",
                source_date=candidate.source_date or now.date().isoformat(),
                source_message_ids=source_message_ids,
                created_at=now.isoformat(),
            )
        )
        write_json_file(self.settings.documents.conflict_notes, [note.to_dict() for note in notes])

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
                entries_in_range.append(dict(item))
        return entries_in_range

    def _load_memory_entries(self, path: Path) -> list[ManagedMemoryEntry]:
        raw_entries = load_entries(path)
        return [ManagedMemoryEntry.from_dict(item) for item in raw_entries]

    def _same_memory(self, left: str, right: str) -> bool:
        normalized_left = _normalize_memory(left)
        normalized_right = _normalize_memory(right)
        if normalized_left == normalized_right:
            return True
        if len(normalized_left) >= 8 and normalized_left in normalized_right:
            return True
        if len(normalized_right) >= 8 and normalized_right in normalized_left:
            return True
        return False

    def _is_conflict(self, existing_content: str, new_content: str) -> bool:
        existing_norm = _normalize_memory(_strip_negation(existing_content))
        new_norm = _normalize_memory(_strip_negation(new_content))
        return existing_norm == new_norm and _has_negation(existing_content) != _has_negation(new_content)


def _normalize_memory(value: str) -> str:
    return "".join(value.lower().split())


def _has_negation(value: str) -> bool:
    return any(token in value for token in NEGATION_TOKENS)


def _strip_negation(value: str) -> str:
    stripped = value
    for token in NEGATION_TOKENS:
        stripped = stripped.replace(token, "")
    return stripped


NEGATION_TOKENS = ("不喜欢", "不需要", "不愿意", "不是", "不再", "不想", "并非", "没有", "从不", "没")


def _merged_unique(existing: Iterable[str], new_values: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for item in [*existing, *new_values]:
        if item and item not in seen:
            seen.append(item)
    return seen
