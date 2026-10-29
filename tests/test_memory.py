from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dropline.documents import load_entries, load_json_file, write_json_file
from dropline.memory import MemoryManager
from dropline.models import (
    ActiveArcUpdateSuggestion,
    CandidateMemory,
    CharacterEpisode,
    ConflictNote,
    Stage1Result,
    Stage2Result,
    UserDigest,
)
from dropline.utils import hash_text

from tests.helpers import create_test_repo, load_test_settings


def _texts_for(stage1: Stage1Result) -> list[str]:
    """Pretend the letter consisted of exactly the quoted evidence sentences."""
    return ["\n".join(candidate.evidence for candidate in stage1.candidate_memories)]


def _active_contents(entries: list[dict]) -> list[str]:
    return [item["content"] for item in entries if item.get("status", "active") == "active"]


class MemoryManagerTests(unittest.TestCase):
    def _stage2_with_arc_suggestion(self, *, action: str, content: str) -> Stage2Result:
        return Stage2Result(
            subject="今夜先缓一缓",
            body="我会记得这件还在推进的事。",
            character_episode=CharacterEpisode(
                summary="继续留意用户正在推进的事。",
                continuity_value="保持后续连贯。",
            ),
            active_arc_update_suggestion=ActiveArcUpdateSuggestion(action=action, content=content),
            scene_card=None,
        )

    def test_user_profile_write_is_idempotent_for_same_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            now = datetime(2026, 3, 31, 22, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
            stage1 = Stage1Result(
                user_digest=UserDigest(main_event="你说自己在准备长期考试。", state="认真", possible_needs=["回应"]),
                candidate_memories=[
                    CandidateMemory(
                        content="用户长期目标是准备重要考试",
                        type="user_profile",
                        confidence=0.95,
                        durability="stable",
                        evidence="记住，我最近一直在准备这场考试。",
                        why_it_matters="会影响后续安慰和建议方式。",
                        source_date="2026-03-31",
                    )
                ],
                emotional_priority="advice",
                open_loops=[],
            )

            manager.write_user_side(
                stage1_result=stage1,
                inbound_texts=_texts_for(stage1),
                batch_id="batch-1",
                now=now,
                source_message_ids=["<msg-1@example.com>"],
                source_dates=["2026-03-31"],
            )
            manager.write_user_side(
                stage1_result=stage1,
                inbound_texts=_texts_for(stage1),
                batch_id="batch-1",
                now=now,
                source_message_ids=["<msg-1@example.com>"],
                source_dates=["2026-03-31"],
            )

            profile_entries = load_entries(settings.documents.user_profile)
            episode_entries = load_json_file(settings.documents.user_episode_dir / "2026-03-31.json", default=[])
            self.assertEqual(len(profile_entries), 1)
            self.assertEqual(len(episode_entries), 1)
            self.assertEqual(
                episode_entries[0]["episode"],
                {
                    "main_event": "你说自己在准备长期考试。",
                    "state": "认真",
                    "possible_needs": ["回应"],
                },
            )
            self.assertIn("memory_observations", episode_entries[0])

    def test_active_arc_requires_repeat_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)

            first = Stage1Result(
                user_digest=UserDigest(main_event="课业很忙。", state="疲惫", possible_needs=["陪伴"]),
                candidate_memories=[
                    CandidateMemory(
                        content="用户最近持续被课业压着走",
                        type="active_arc",
                        confidence=0.9,
                        durability="short",
                        evidence="这周作业一直很多。",
                        why_it_matters="未来几天可能还会继续提到。",
                        source_date="2026-03-31",
                    )
                ],
                emotional_priority="comfort",
                open_loops=[],
            )
            second = Stage1Result(
                user_digest=UserDigest(main_event="课业仍然很忙。", state="疲惫", possible_needs=["陪伴"]),
                candidate_memories=[
                    CandidateMemory(
                        content="用户最近持续被课业压着走",
                        type="active_arc",
                        confidence=0.92,
                        durability="short",
                        evidence="今天还是被作业压着。",
                        why_it_matters="未来几天可能还会继续提到。",
                        source_date="2026-04-01",
                    )
                ],
                emotional_priority="comfort",
                open_loops=[],
            )

            manager.write_user_side(
                stage1_result=first,
                inbound_texts=_texts_for(first),
                batch_id="batch-1",
                now=datetime(2026, 3, 31, 22, 0, tzinfo=ZoneInfo("America/Los_Angeles")),
                source_message_ids=["<msg-1@example.com>"],
                source_dates=["2026-03-31"],
            )
            entries_after_first = load_entries(settings.documents.active_arc)
            self.assertEqual(len(entries_after_first), 0)

            manager.write_user_side(
                stage1_result=second,
                inbound_texts=_texts_for(second),
                batch_id="batch-2",
                now=datetime(2026, 4, 1, 22, 0, tzinfo=ZoneInfo("America/Los_Angeles")),
                source_message_ids=["<msg-2@example.com>"],
                source_dates=["2026-04-01"],
            )
            entries_after_second = load_entries(settings.documents.active_arc)
            self.assertEqual(len(entries_after_second), 1)

    def test_active_arc_update_replaces_content_and_merges_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            write_json_file(
                settings.documents.active_arc,
                [
                    {
                        "key": "active-exam",
                        "content": "用户仍在准备考试",
                        "memory_type": "active_arc",
                        "confidence": 0.75,
                        "durability": "short",
                        "evidence": ["initial evidence"],
                        "source_dates": ["2026-03-31"],
                        "source_message_ids": ["batch-old"],
                        "created_at": "2026-03-31T22:00:00-07:00",
                        "updated_at": "2026-03-31T22:00:00-07:00",
                        "why_it_matters": "持续主线。",
                    }
                ],
            )
            now = datetime(2026, 4, 1, 22, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

            manager.write_character_side(
                stage2_result=self._stage2_with_arc_suggestion(
                    action="update",
                    content="用户仍在准备考试，需要把复习节奏拆小",
                ),
                batch_id="batch-new",
                now=now,
            )

            entries = load_entries(settings.documents.active_arc)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["key"], "active-exam")
            self.assertEqual(entries[0]["content"], "用户仍在准备考试，需要把复习节奏拆小")
            self.assertEqual(entries[0]["source_message_ids"], ["batch-old", "batch-new"])
            self.assertEqual(entries[0]["source_dates"], ["2026-03-31", "2026-04-01"])
            self.assertEqual(entries[0]["evidence"], ["initial evidence", "stage2 active arc suggestion"])
            self.assertEqual(entries[0]["updated_at"], now.isoformat())

    def test_active_arc_keep_only_touches_matching_entry_and_merges_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            write_json_file(
                settings.documents.active_arc,
                [
                    {
                        "key": "active-exam",
                        "content": "用户仍在准备考试",
                        "memory_type": "active_arc",
                        "confidence": 0.75,
                        "durability": "short",
                        "evidence": ["initial evidence"],
                        "source_dates": ["2026-03-31"],
                        "source_message_ids": ["batch-old"],
                        "created_at": "2026-03-31T22:00:00-07:00",
                        "updated_at": "2026-03-31T22:00:00-07:00",
                        "why_it_matters": "持续主线。",
                    }
                ],
            )
            now = datetime(2026, 4, 1, 22, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

            manager.write_character_side(
                stage2_result=self._stage2_with_arc_suggestion(action="keep", content="用户仍在准备考试"),
                batch_id="batch-new",
                now=now,
            )

            entries = load_entries(settings.documents.active_arc)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["content"], "用户仍在准备考试")
            self.assertEqual(entries[0]["source_message_ids"], ["batch-old", "batch-new"])
            self.assertEqual(entries[0]["source_dates"], ["2026-03-31", "2026-04-01"])
            self.assertEqual(entries[0]["evidence"], ["initial evidence"])
            self.assertEqual(entries[0]["updated_at"], now.isoformat())

    def test_active_arc_archive_removes_match_and_none_does_not_change_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            original_entry = {
                "key": "active-exam",
                "content": "用户仍在准备考试",
                "memory_type": "active_arc",
                "confidence": 0.75,
                "durability": "short",
                "evidence": ["initial evidence"],
                "source_dates": ["2026-03-31"],
                "source_message_ids": ["batch-old"],
                "created_at": "2026-03-31T22:00:00-07:00",
                "updated_at": "2026-03-31T22:00:00-07:00",
                "why_it_matters": "持续主线。",
            }
            write_json_file(settings.documents.active_arc, [original_entry])

            manager.write_character_side(
                stage2_result=self._stage2_with_arc_suggestion(action="none", content=""),
                batch_id="batch-none",
                now=datetime(2026, 4, 1, 22, 0, tzinfo=ZoneInfo("America/Los_Angeles")),
            )
            entries_after_none = load_entries(settings.documents.active_arc)
            self.assertEqual(entries_after_none, [original_entry])

            manager.write_character_side(
                stage2_result=self._stage2_with_arc_suggestion(action="archive", content="用户仍在准备考试"),
                batch_id="batch-archive",
                now=datetime(2026, 4, 2, 22, 0, tzinfo=ZoneInfo("America/Los_Angeles")),
            )
            entries_after_archive = load_entries(settings.documents.active_arc)
            self.assertEqual(entries_after_archive, [])

    def test_active_arc_trim_uses_actual_chronological_order_across_dst(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)

            # "2026-11-01T01:30:00-07:00" (08:30 UTC) is lexically larger than
            # "2026-11-01T01:15:00-08:00" (09:15 UTC) but is actually the earlier instant
            # (US DST fall-back repeats the 1am hour). arc-z (09:05 UTC) is actually more
            # recent than arc-y (08:30 UTC) despite sorting lower as a plain string.
            write_json_file(
                settings.documents.active_arc,
                [
                    {
                        "key": "arc-x",
                        "content": "用户在推进考试复习(X)",
                        "memory_type": "active_arc",
                        "confidence": 0.8,
                        "durability": "short",
                        "evidence": ["evidence-x"],
                        "source_dates": ["2026-11-01"],
                        "source_message_ids": ["batch-x"],
                        "created_at": "2026-11-01T01:15:00-08:00",
                        "updated_at": "2026-11-01T01:15:00-08:00",
                        "why_it_matters": "",
                    },
                    {
                        "key": "arc-y",
                        "content": "用户在推进考试复习(Y)",
                        "memory_type": "active_arc",
                        "confidence": 0.8,
                        "durability": "short",
                        "evidence": ["evidence-y"],
                        "source_dates": ["2026-11-01"],
                        "source_message_ids": ["batch-y"],
                        "created_at": "2026-11-01T01:30:00-07:00",
                        "updated_at": "2026-11-01T01:30:00-07:00",
                        "why_it_matters": "",
                    },
                    {
                        "key": "arc-z",
                        "content": "用户在推进考试复习(Z)",
                        "memory_type": "active_arc",
                        "confidence": 0.8,
                        "durability": "short",
                        "evidence": ["evidence-z"],
                        "source_dates": ["2026-11-01"],
                        "source_message_ids": ["batch-z"],
                        "created_at": "2026-11-01T01:05:00-08:00",
                        "updated_at": "2026-11-01T01:05:00-08:00",
                        "why_it_matters": "",
                    },
                ],
            )

            manager.write_character_side(
                stage2_result=self._stage2_with_arc_suggestion(
                    action="update",
                    content="用户开始新的连续主线(NEW)",
                ),
                batch_id="batch-new",
                now=datetime(2026, 11, 2, 12, 0, tzinfo=ZoneInfo("UTC")),
            )

            entries = load_entries(settings.documents.active_arc)
            keys = {item["key"] for item in entries}
            self.assertEqual(len(entries), 2)
            # arc-x (09:15 UTC) is the actual second-most-recent entry and must survive;
            # arc-y (08:30 UTC) is actually the oldest of the three and must be evicted,
            # even though its timestamp string sorts highest lexically.
            self.assertIn("arc-x", keys)
            self.assertNotIn("arc-y", keys)

    def test_conflict_detection_does_not_treat_broad_bu_as_negation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            now = datetime(2026, 3, 31, 22, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
            existing = Stage1Result(
                user_digest=UserDigest(main_event="用户提到计划。", state="平稳", possible_needs=[]),
                candidate_memories=[
                    CandidateMemory(
                        content="用户最近状态确定",
                        type="user_profile",
                        confidence=0.95,
                        durability="stable",
                        evidence="记住，我最近状态确定。",
                        why_it_matters="后续可参考计划稳定性。",
                        source_date="2026-03-31",
                    )
                ],
                emotional_priority="comfort",
                open_loops=[],
            )
            new_observation = Stage1Result(
                user_digest=UserDigest(main_event="用户补充计划细节。", state="平稳", possible_needs=[]),
                candidate_memories=[
                    CandidateMemory(
                        content="用户最近状态不确定",
                        type="user_profile",
                        confidence=0.95,
                        durability="stable",
                        evidence="记住，最近状态不确定。",
                        why_it_matters="后续可参考计划稳定性。",
                        source_date="2026-04-01",
                    )
                ],
                emotional_priority="comfort",
                open_loops=[],
            )

            manager.write_user_side(
                stage1_result=existing,
                inbound_texts=_texts_for(existing),
                batch_id="batch-old",
                now=now,
                source_message_ids=["<msg-old@example.com>"],
                source_dates=["2026-03-31"],
            )
            manager.write_user_side(
                stage1_result=new_observation,
                inbound_texts=_texts_for(new_observation),
                batch_id="batch-new",
                now=now,
                source_message_ids=["<msg-new@example.com>"],
                source_dates=["2026-04-01"],
            )

            profile_entries = load_entries(settings.documents.user_profile)
            conflict_entries = load_entries(settings.documents.conflict_notes)
            self.assertEqual([item["content"] for item in profile_entries], ["用户最近状态确定", "用户最近状态不确定"])
            self.assertEqual(conflict_entries, [])

    def test_conflict_detection_still_blocks_clear_negation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            now = datetime(2026, 3, 31, 22, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
            existing = Stage1Result(
                user_digest=UserDigest(main_event="用户提到夜跑。", state="平稳", possible_needs=[]),
                candidate_memories=[
                    CandidateMemory(
                        content="用户喜欢夜跑",
                        type="user_profile",
                        confidence=0.95,
                        durability="stable",
                        evidence="记住，我喜欢夜跑。",
                        why_it_matters="后续可参考运动习惯。",
                        source_date="2026-03-31",
                    )
                ],
                emotional_priority="comfort",
                open_loops=[],
            )
            negated = Stage1Result(
                user_digest=UserDigest(main_event="用户更新夜跑偏好。", state="平稳", possible_needs=[]),
                candidate_memories=[
                    CandidateMemory(
                        content="用户不再喜欢夜跑",
                        type="user_profile",
                        confidence=0.95,
                        durability="stable",
                        evidence="记住，我不再喜欢夜跑。",
                        why_it_matters="后续可参考运动习惯。",
                        source_date="2026-04-01",
                    )
                ],
                emotional_priority="comfort",
                open_loops=[],
            )

            manager.write_user_side(
                stage1_result=existing,
                inbound_texts=_texts_for(existing),
                batch_id="batch-old",
                now=now,
                source_message_ids=["<msg-old@example.com>"],
                source_dates=["2026-03-31"],
            )
            manager.write_user_side(
                stage1_result=negated,
                inbound_texts=_texts_for(negated),
                batch_id="batch-new",
                now=now,
                source_message_ids=["<msg-new@example.com>"],
                source_dates=["2026-04-01"],
            )

            profile_entries = load_entries(settings.documents.user_profile)
            conflict_entries = load_entries(settings.documents.conflict_notes)
            self.assertEqual([item["content"] for item in profile_entries], ["用户喜欢夜跑"])
            self.assertEqual(len(conflict_entries), 1)
            self.assertEqual(conflict_entries[0]["existing_content"], "用户喜欢夜跑")
            self.assertEqual(conflict_entries[0]["new_content"], "用户不再喜欢夜跑")
            self.assertEqual(conflict_entries[0]["source_message_ids"], ["<msg-new@example.com>"])

    def _negated_night_run_stage1(self, source_date: str) -> Stage1Result:
        return Stage1Result(
            user_digest=UserDigest(main_event="用户更新夜跑偏好。", state="平稳", possible_needs=[]),
            candidate_memories=[
                CandidateMemory(
                    content="用户不再喜欢夜跑",
                    type="user_profile",
                    confidence=0.95,
                    durability="stable",
                    evidence="记住，我不再喜欢夜跑。",
                    why_it_matters="后续可参考运动习惯。",
                    source_date=source_date,
                )
            ],
            emotional_priority="comfort",
            open_loops=[],
        )

    def test_conflict_note_same_day_repeat_does_not_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            now = datetime(2026, 3, 31, 22, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
            existing = Stage1Result(
                user_digest=UserDigest(main_event="用户提到夜跑。", state="平稳", possible_needs=[]),
                candidate_memories=[
                    CandidateMemory(
                        content="用户喜欢夜跑",
                        type="user_profile",
                        confidence=0.95,
                        durability="stable",
                        evidence="记住，我喜欢夜跑。",
                        why_it_matters="后续可参考运动习惯。",
                        source_date="2026-03-31",
                    )
                ],
                emotional_priority="comfort",
                open_loops=[],
            )
            manager.write_user_side(
                stage1_result=existing,
                inbound_texts=_texts_for(existing),
                batch_id="batch-old",
                now=now,
                source_message_ids=["<msg-old@example.com>"],
                source_dates=["2026-03-31"],
            )

            # Fires the conflict once on 2026-04-01.
            manager.write_user_side(
                stage1_result=self._negated_night_run_stage1("2026-04-01"),
                inbound_texts=_texts_for(self._negated_night_run_stage1("2026-04-01")),
                batch_id="batch-day1",
                now=now,
                source_message_ids=["<msg-day1@example.com>"],
                source_dates=["2026-04-01"],
            )
            # Repeats on the SAME date -> must not count as a second distinct occurrence.
            manager.write_user_side(
                stage1_result=self._negated_night_run_stage1("2026-04-01"),
                inbound_texts=_texts_for(self._negated_night_run_stage1("2026-04-01")),
                batch_id="batch-day1-dup",
                now=now,
                source_message_ids=["<msg-day1-dup@example.com>"],
                source_dates=["2026-04-01"],
            )

            profile_entries = load_entries(settings.documents.user_profile)
            conflict_entries = load_entries(settings.documents.conflict_notes)
            self.assertEqual([item["content"] for item in profile_entries], ["用户喜欢夜跑"])
            self.assertEqual(len(conflict_entries), 1)
            self.assertEqual(conflict_entries[0]["occurrence_dates"], ["2026-04-01"])

    def test_conflict_note_overwrites_after_two_distinct_occurrence_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            now = datetime(2026, 3, 31, 22, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
            existing = Stage1Result(
                user_digest=UserDigest(main_event="用户提到夜跑。", state="平稳", possible_needs=[]),
                candidate_memories=[
                    CandidateMemory(
                        content="用户喜欢夜跑",
                        type="user_profile",
                        confidence=0.95,
                        durability="stable",
                        evidence="记住，我喜欢夜跑。",
                        why_it_matters="后续可参考运动习惯。",
                        source_date="2026-03-31",
                    )
                ],
                emotional_priority="comfort",
                open_loops=[],
            )
            manager.write_user_side(
                stage1_result=existing,
                inbound_texts=_texts_for(existing),
                batch_id="batch-old",
                now=now,
                source_message_ids=["<msg-old@example.com>"],
                source_dates=["2026-03-31"],
            )

            manager.write_user_side(
                stage1_result=self._negated_night_run_stage1("2026-04-01"),
                inbound_texts=_texts_for(self._negated_night_run_stage1("2026-04-01")),
                batch_id="batch-day1",
                now=now,
                source_message_ids=["<msg-day1@example.com>"],
                source_dates=["2026-04-01"],
            )
            profile_after_day1 = load_entries(settings.documents.user_profile)
            self.assertEqual([item["content"] for item in profile_after_day1], ["用户喜欢夜跑"])

            # Second DISTINCT date -> the repeated-occurrence override path fires.
            manager.write_user_side(
                stage1_result=self._negated_night_run_stage1("2026-04-02"),
                inbound_texts=_texts_for(self._negated_night_run_stage1("2026-04-02")),
                batch_id="batch-day2",
                now=now,
                source_message_ids=["<msg-day2@example.com>"],
                source_dates=["2026-04-02"],
            )

            profile_after_override = load_entries(settings.documents.user_profile)
            conflict_entries_after_override = load_entries(settings.documents.conflict_notes)
            self.assertEqual(_active_contents(profile_after_override), ["用户不再喜欢夜跑"])
            self.assertEqual(conflict_entries_after_override, [])

    def test_legacy_conflict_note_without_occurrence_dates_loads_and_still_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            now = datetime(2026, 4, 2, 22, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

            write_json_file(
                settings.documents.user_profile,
                [
                    {
                        "key": "user-profile-existing",
                        "content": "用户喜欢夜跑",
                        "memory_type": "user_profile",
                        "confidence": 0.95,
                        "durability": "stable",
                        "evidence": ["记住，我喜欢夜跑。"],
                        "source_dates": ["2026-03-31"],
                        "source_message_ids": ["batch-old"],
                        "created_at": "2026-03-31T22:00:00-07:00",
                        "updated_at": "2026-03-31T22:00:00-07:00",
                        "why_it_matters": "",
                    }
                ],
            )
            legacy_key = hash_text("用户喜欢夜跑:用户不再喜欢夜跑", prefix="conflict-")
            # Old-format conflict note: no "occurrence_dates" field at all.
            write_json_file(
                settings.documents.conflict_notes,
                [
                    {
                        "key": legacy_key,
                        "memory_type": "user_profile",
                        "existing_content": "用户喜欢夜跑",
                        "new_content": "用户不再喜欢夜跑",
                        "reason": "Detected negation-style conflict between existing and new long-term memory.",
                        "source_date": "2026-04-01",
                        "source_message_ids": ["<msg-old@example.com>"],
                        "created_at": "2026-04-01T22:00:00-07:00",
                    }
                ],
            )

            manager.write_user_side(
                stage1_result=self._negated_night_run_stage1("2026-04-02"),
                inbound_texts=_texts_for(self._negated_night_run_stage1("2026-04-02")),
                batch_id="batch-day2",
                now=now,
                source_message_ids=["<msg-day2@example.com>"],
                source_dates=["2026-04-02"],
            )

            profile_entries = load_entries(settings.documents.user_profile)
            conflict_entries = load_entries(settings.documents.conflict_notes)
            self.assertEqual(_active_contents(profile_entries), ["用户不再喜欢夜跑"])
            self.assertEqual(conflict_entries, [])

    def test_conflict_note_from_dict_backfills_missing_occurrence_dates(self) -> None:
        payload = {
            "key": "conflict-legacy",
            "memory_type": "user_profile",
            "existing_content": "用户喜欢夜跑",
            "new_content": "用户不再喜欢夜跑",
            "reason": "Detected negation-style conflict between existing and new long-term memory.",
            "source_date": "2026-04-01",
            "source_message_ids": ["<msg@example.com>"],
            "created_at": "2026-04-01T22:00:00-07:00",
        }
        note = ConflictNote.from_dict(payload)
        self.assertEqual(note.occurrence_dates, ["2026-04-01"])


    def _stage1_with(self, *candidates: CandidateMemory) -> Stage1Result:
        return Stage1Result(
            user_digest=UserDigest(main_event="来信。", state="平稳", possible_needs=[]),
            candidate_memories=list(candidates),
            emotional_priority="comfort",
            open_loops=[],
        )

    def _candidate(
        self,
        content: str,
        evidence: str,
        *,
        memory_type: str = "user_profile",
        confidence: float = 0.95,
        durability: str = "stable",
        source_date: str = "2026-03-31",
    ) -> CandidateMemory:
        return CandidateMemory(
            content=content,
            type=memory_type,
            confidence=confidence,
            durability=durability,
            evidence=evidence,
            why_it_matters="",
            source_date=source_date,
        )

    def _write(self, manager: MemoryManager, stage1: Stage1Result, *, batch_id: str, date: str, texts: list[str]) -> None:
        manager.write_user_side(
            stage1_result=stage1,
            batch_id=batch_id,
            now=datetime.fromisoformat(f"{date}T22:00:00-07:00"),
            source_message_ids=[f"<{batch_id}@example.com>"],
            source_dates=[date],
            inbound_texts=texts,
        )

    def test_candidate_whose_evidence_is_not_in_the_letter_is_not_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            stage1 = self._stage1_with(self._candidate("用户讨厌自己的工作", "记住，用户表示长期不满意工作"))

            self._write(manager, stage1, batch_id="batch-1", date="2026-03-31", texts=["今天被加班气到了。"])

            self.assertEqual(load_entries(settings.documents.user_profile), [])
            episode = load_json_file(settings.documents.user_episode_dir / "2026-03-31.json", default=[])
            self.assertEqual(len(episode), 1)
            self.assertEqual(episode[0]["memory_observations"][0]["content"], "用户讨厌自己的工作")

    def test_explicit_request_in_the_letter_promotes_on_first_letter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            stage1 = self._stage1_with(self._candidate("用户对花生过敏", "我对花生过敏", confidence=0.8))

            self._write(manager, stage1, batch_id="batch-1", date="2026-03-31", texts=["记住，我对花生过敏，以后别推荐含花生的点心。"])

            self.assertEqual(_active_contents(load_entries(settings.documents.user_profile)), ["用户对花生过敏"])

    def test_long_term_wording_in_evidence_alone_does_not_promote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            stage1 = self._stage1_with(self._candidate("用户长期在准备考试", "我长期在准备考试"))

            self._write(manager, stage1, batch_id="batch-1", date="2026-03-31", texts=["我长期在准备考试。"])

            self.assertEqual(load_entries(settings.documents.user_profile), [])

    def test_repeat_observation_must_come_from_distinct_mail_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            stage1 = self._stage1_with(self._candidate("用户容易疲劳", "我容易疲劳"))

            self._write(manager, stage1, batch_id="batch-1", date="2026-03-31", texts=["我容易疲劳。"])
            self._write(manager, stage1, batch_id="batch-2", date="2026-03-31", texts=["我容易疲劳。"])
            self.assertEqual(load_entries(settings.documents.user_profile), [])

            self._write(manager, stage1, batch_id="batch-3", date="2026-04-02", texts=["我容易疲劳。"])
            entries = load_entries(settings.documents.user_profile)
            self.assertEqual(_active_contents(entries), ["用户容易疲劳"])
            self.assertEqual(entries[0]["source_dates"], ["2026-04-02"])

    def test_per_letter_limit_keeps_the_two_most_confident_user_profile_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            stage1 = self._stage1_with(
                self._candidate("用户是左撇子", "我是左撇子", confidence=0.85),
                self._candidate("用户对花生过敏", "我对花生过敏", confidence=0.95),
                self._candidate("用户住在上海", "我住在上海", confidence=0.9),
            )

            self._write(
                manager,
                stage1,
                batch_id="batch-1",
                date="2026-03-31",
                texts=["记住，我对花生过敏。记住，我住在上海。记住，我是左撇子。"],
            )

            self.assertEqual(_active_contents(load_entries(settings.documents.user_profile)), ["用户对花生过敏", "用户住在上海"])

    def test_transient_wording_in_content_blocks_user_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            transient = self._stage1_with(self._candidate("用户今天很累", "今天好累"))
            stable = self._stage1_with(self._candidate("用户容易疲劳", "我容易疲劳"))

            self._write(manager, transient, batch_id="batch-1", date="2026-03-31", texts=["今天好累。"])
            self._write(manager, transient, batch_id="batch-2", date="2026-04-01", texts=["今天好累。"])
            self._write(manager, stable, batch_id="batch-3", date="2026-03-31", texts=["我容易疲劳。"])
            self._write(manager, stable, batch_id="batch-4", date="2026-04-01", texts=["我容易疲劳。"])

            self.assertEqual(_active_contents(load_entries(settings.documents.user_profile)), ["用户容易疲劳"])

    def test_english_remember_request_promotes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            stage1 = self._stage1_with(self._candidate("The user is allergic to peanuts", "I'm allergic to peanuts"))

            self._write(manager, stage1, batch_id="batch-1", date="2026-03-31", texts=["Please remember that I'm allergic to peanuts."])

            self.assertEqual(_active_contents(load_entries(settings.documents.user_profile)), ["The user is allergic to peanuts"])

    def test_verb_negation_is_detected_as_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)

            self._write(manager, self._stage1_with(self._candidate("用户喜欢猫", "我喜欢猫")), batch_id="batch-1", date="2026-03-31", texts=["记住，我喜欢猫。"])
            self._write(
                manager,
                self._stage1_with(self._candidate("用户不喜欢猫", "我不喜欢猫", source_date="2026-04-01")),
                batch_id="batch-2",
                date="2026-04-01",
                texts=["记住，我不喜欢猫。"],
            )

            self.assertEqual(_active_contents(load_entries(settings.documents.user_profile)), ["用户喜欢猫"])
            conflicts = load_entries(settings.documents.conflict_notes)
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0]["new_content"], "用户不喜欢猫")
            self.assertEqual(conflicts[0]["occurrence_dates"], ["2026-04-01"])

    def test_conflict_is_checked_before_substring_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)

            self._write(
                manager,
                self._stage1_with(self._candidate("用户每周三次去健身房", "我每周三次去健身房")),
                batch_id="batch-1",
                date="2026-03-31",
                texts=["记住，我每周三次去健身房。"],
            )
            self._write(
                manager,
                self._stage1_with(self._candidate("用户不再每周三次去健身房", "我不再去健身房了", source_date="2026-04-01")),
                batch_id="batch-2",
                date="2026-04-01",
                texts=["记住，我不再去健身房了。"],
            )

            entries = load_entries(settings.documents.user_profile)
            self.assertEqual(_active_contents(entries), ["用户每周三次去健身房"])
            self.assertEqual(entries[0]["evidence"], ["我每周三次去健身房"])
            self.assertEqual(len(load_entries(settings.documents.conflict_notes)), 1)

    def test_user_correction_overrides_immediately_and_keeps_superseded_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)

            self._write(manager, self._stage1_with(self._candidate("用户喜欢猫", "我喜欢猫")), batch_id="batch-1", date="2026-03-31", texts=["记住，我喜欢猫。"])
            self._write(
                manager,
                self._stage1_with(self._candidate("用户不喜欢猫", "其实我不喜欢猫", source_date="2026-04-01")),
                batch_id="batch-2",
                date="2026-04-01",
                texts=["我说错了，其实我不喜欢猫。"],
            )

            entries = load_entries(settings.documents.user_profile)
            self.assertEqual(_active_contents(entries), ["用户不喜欢猫"])
            superseded = [item for item in entries if item["status"] == "superseded"]
            self.assertEqual(len(superseded), 1)
            self.assertEqual(superseded[0]["content"], "用户喜欢猫")
            active = next(item for item in entries if item["status"] == "active")
            self.assertEqual(superseded[0]["superseded_by"], active["key"])
            self.assertEqual(load_entries(settings.documents.conflict_notes), [])
            prompt_entries = json.loads(manager.active_entries_text(settings.documents.user_profile))
            self.assertEqual([item["content"] for item in prompt_entries], ["用户不喜欢猫"])

    def test_deleted_memory_is_not_written_back_by_stage1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            stage1 = self._stage1_with(self._candidate("用户对花生过敏", "我对花生过敏"))
            now = datetime(2026, 4, 1, 12, 0, tzinfo=ZoneInfo("UTC"))

            self._write(manager, stage1, batch_id="batch-1", date="2026-03-31", texts=["记住，我对花生过敏。"])
            key = load_entries(settings.documents.user_profile)[0]["key"]
            self.assertTrue(manager.delete_entry("user_profile", key, now=now))
            self.assertEqual(load_entries(settings.documents.user_profile), [])
            self.assertEqual(len(manager.list_suppressed()), 1)

            self._write(manager, stage1, batch_id="batch-2", date="2026-04-02", texts=["记住，我对花生过敏。"])
            self.assertEqual(load_entries(settings.documents.user_profile), [])

            self.assertTrue(manager.unsuppress(manager.list_suppressed()[0]["key"]))
            self._write(manager, stage1, batch_id="batch-3", date="2026-04-03", texts=["记住，我对花生过敏。"])
            self.assertEqual(_active_contents(load_entries(settings.documents.user_profile)), ["用户对花生过敏"])

    def test_relationship_memory_requires_explicit_request_or_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            nickname = self._stage1_with(
                self._candidate("用户希望被称呼为小鱼", "以后就叫我小鱼吧", memory_type="relationship_memory", confidence=0.8)
            )
            plan = self._stage1_with(
                self._candidate("用户和角色约好一起看海灯节", "我们一起看海灯节吧", memory_type="relationship_memory", confidence=0.9)
            )

            self._write(manager, nickname, batch_id="batch-1", date="2026-03-31", texts=["以后就叫我小鱼吧。"])
            self._write(manager, plan, batch_id="batch-2", date="2026-03-31", texts=["我们一起看海灯节吧。"])

            self.assertEqual(_active_contents(load_entries(settings.documents.relationship_memory)), ["用户希望被称呼为小鱼"])

    def test_manual_add_update_delete_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            now = datetime(2026, 4, 1, 12, 0, tzinfo=ZoneInfo("UTC"))

            added = manager.add_manual_entry("user_profile", content="用户养了一只叫团子的猫", why_it_matters="写信时可以问候", now=now)
            self.assertEqual(manager.list_entries("user_profile")[0]["content"], "用户养了一只叫团子的猫")
            self.assertEqual(added["evidence"], ["manual entry"])

            updated = manager.update_entry("user_profile", added["key"], content="用户养了一只叫团子的橘猫", now=now)
            self.assertEqual(updated["content"], "用户养了一只叫团子的橘猫")
            self.assertIsNone(manager.update_entry("user_profile", "missing-key", content="x", now=now))

            self.assertTrue(manager.delete_entry("user_profile", added["key"], now=now))
            self.assertEqual(manager.list_entries("user_profile"), [])
            self.assertEqual(manager.list_suppressed()[0]["content"], "用户养了一只叫团子的橘猫")
            with self.assertRaises(ValueError):
                manager.add_manual_entry("unknown_file", content="x", now=now)

    def test_resolve_conflict_take_new_supersedes_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            manager = MemoryManager(settings)
            now = datetime(2026, 4, 2, 12, 0, tzinfo=ZoneInfo("UTC"))

            self._write(manager, self._stage1_with(self._candidate("用户喜欢猫", "我喜欢猫")), batch_id="batch-1", date="2026-03-31", texts=["记住，我喜欢猫。"])
            self._write(
                manager,
                self._stage1_with(self._candidate("用户不喜欢猫", "我不喜欢猫", source_date="2026-04-01")),
                batch_id="batch-2",
                date="2026-04-01",
                texts=["记住，我不喜欢猫。"],
            )
            key = manager.list_conflicts()[0]["key"]

            self.assertTrue(manager.resolve_conflict(key, choice="take_new", now=now))

            self.assertEqual(_active_contents(load_entries(settings.documents.user_profile)), ["用户不喜欢猫"])
            self.assertEqual(manager.list_conflicts(), [])
            self.assertFalse(manager.resolve_conflict(key, choice="keep_existing", now=now))


if __name__ == "__main__":
    unittest.main()
