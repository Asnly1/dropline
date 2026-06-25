from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dropline.documents import load_entries, load_json_file, write_json_file
from dropline.memory import MemoryManager
from dropline.models import ActiveArcUpdateSuggestion, CandidateMemory, CharacterEpisode, Stage1Result, Stage2Result, UserDigest

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


if __name__ == "__main__":
    unittest.main()
