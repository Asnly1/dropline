from __future__ import annotations

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
                batch_id="batch-1",
                now=now,
                source_message_ids=["<msg-1@example.com>"],
                source_dates=["2026-03-31"],
            )
            manager.write_user_side(
                stage1_result=stage1,
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
                batch_id="batch-1",
                now=datetime(2026, 3, 31, 22, 0, tzinfo=ZoneInfo("America/Los_Angeles")),
                source_message_ids=["<msg-1@example.com>"],
                source_dates=["2026-03-31"],
            )
            entries_after_first = load_entries(settings.documents.active_arc)
            self.assertEqual(len(entries_after_first), 0)

            manager.write_user_side(
                stage1_result=second,
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
                batch_id="batch-old",
                now=now,
                source_message_ids=["<msg-old@example.com>"],
                source_dates=["2026-03-31"],
            )
            manager.write_user_side(
                stage1_result=new_observation,
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
                batch_id="batch-old",
                now=now,
                source_message_ids=["<msg-old@example.com>"],
                source_dates=["2026-03-31"],
            )
            manager.write_user_side(
                stage1_result=negated,
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
                batch_id="batch-old",
                now=now,
                source_message_ids=["<msg-old@example.com>"],
                source_dates=["2026-03-31"],
            )

            # Fires the conflict once on 2026-04-01.
            manager.write_user_side(
                stage1_result=self._negated_night_run_stage1("2026-04-01"),
                batch_id="batch-day1",
                now=now,
                source_message_ids=["<msg-day1@example.com>"],
                source_dates=["2026-04-01"],
            )
            # Repeats on the SAME date -> must not count as a second distinct occurrence.
            manager.write_user_side(
                stage1_result=self._negated_night_run_stage1("2026-04-01"),
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
                batch_id="batch-old",
                now=now,
                source_message_ids=["<msg-old@example.com>"],
                source_dates=["2026-03-31"],
            )

            manager.write_user_side(
                stage1_result=self._negated_night_run_stage1("2026-04-01"),
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
                batch_id="batch-day2",
                now=now,
                source_message_ids=["<msg-day2@example.com>"],
                source_dates=["2026-04-02"],
            )

            profile_after_override = load_entries(settings.documents.user_profile)
            conflict_entries_after_override = load_entries(settings.documents.conflict_notes)
            self.assertEqual([item["content"] for item in profile_after_override], ["用户不再喜欢夜跑"])
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
                batch_id="batch-day2",
                now=now,
                source_message_ids=["<msg-day2@example.com>"],
                source_dates=["2026-04-02"],
            )

            profile_entries = load_entries(settings.documents.user_profile)
            conflict_entries = load_entries(settings.documents.conflict_notes)
            self.assertEqual([item["content"] for item in profile_entries], ["用户不再喜欢夜跑"])
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


if __name__ == "__main__":
    unittest.main()
