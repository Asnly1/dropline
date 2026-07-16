from __future__ import annotations

import base64
import tempfile
import unittest
from datetime import timedelta
from email.message import EmailMessage
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dropline.documents import load_entries, load_json_file
from dropline.memory import MemoryManager
from dropline.models import GeneratedImage, ModelValidationError, NormalizedInboundMessage, RawInboundMessage, Stage1Result, Stage2Result
from dropline.pipeline import PipelineDependencies, PipelineService, RunContext
from dropline.storage import StateStore

from tests.helpers import create_test_repo, load_test_settings


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9lJawAAAAASUVORK5CYII="
)
LONG_BODY = (
    "见字如晤。今晚的风并不急，我把你写来的那几句话在灯下又看了一遍。"
    "你提到的疲惫并不是一句轻巧的安慰就能化开的东西，所以我不打算用太空泛的话来敷衍。"
    "人会因为连续的用力而觉得发沉，这很常见，也并不羞于承认。真正要紧的，是别把一时的吃力误认成永久的无能。"
    "若今日的事情已经压得你心里发闷，就先拆成最小的一步去做，先处理最靠前、最具体、最能落手的那一点。"
    "其余尚且搁着，不代表逃避，只是给自己留出重新整顿气息的余地。"
    "我会记得你这阵子的节奏，也会在你下一封信里继续追问那些今天尚未说完的话。"
    "夜深之前，先让自己缓一缓，再回去面对那些还没完成的事。"
    "若你实在觉得一切都堆在眼前，不妨先离开案前片刻，喝一点热的，等呼吸稳下来再回来。"
    "你不必为了显得镇定，就把真正困住你的部分都压下去；能够坦白说出疲惫，本身就已经是一种清醒。"
    "我今夜会把这份近况安稳记下，等你下回再来信时，我们便从今天没有说完的地方继续。"
)


class FakeInboundClient:
    def __init__(self, messages: list[RawInboundMessage]) -> None:
        self.messages = messages
        self.calls: list[dict] = []

    def fetch_since(
        self,
        since_uid: int,
        limit: int,
        allowlist_senders: tuple[str, ...] = (),
        received_since=None,
    ) -> tuple[list[RawInboundMessage], int]:
        self.calls.append(
            {
                "since_uid": since_uid,
                "limit": limit,
                "allowlist_senders": allowlist_senders,
                "received_since": received_since,
            }
        )
        filtered = [item for item in self.messages if item.imap_uid > since_uid]
        selected = filtered[:limit] if limit > 0 else filtered
        highest_seen_uid = max((item.imap_uid for item in filtered), default=0)
        return selected, highest_seen_uid


class FakeOutboundClient:
    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.sent_messages: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> str:
        if self.should_fail:
            raise RuntimeError("SMTP send failed")
        self.sent_messages.append(message)
        return message["Message-ID"] or "<fake-message-id@example.com>"


class FakeAIClient:
    def __init__(
        self,
        *,
        stage1: Stage1Result,
        stage2: Stage2Result,
        stage1_should_fail: bool = False,
        stage2_should_fail: bool = False,
        image_should_fail: bool = False,
    ) -> None:
        self.stage1 = stage1
        self.stage2 = stage2
        self.stage1_should_fail = stage1_should_fail
        self.stage2_should_fail = stage2_should_fail
        self.image_should_fail = image_should_fail
        self.stage1_calls = 0
        self.stage2_calls = 0
        self.image_calls = 0
        self.stage1_payloads: list[dict] = []
        self.stage2_payloads: list[dict] = []

    def generate_stage1(self, payload: dict) -> Stage1Result:
        self.stage1_calls += 1
        self.stage1_payloads.append(payload)
        if self.stage1_should_fail:
            raise RuntimeError("stage1 network failure")
        return self.stage1

    def generate_stage2(self, payload: dict) -> Stage2Result:
        self.stage2_calls += 1
        self.stage2_payloads.append(payload)
        if self.stage2_should_fail:
            raise RuntimeError("stage2 network failure")
        return self.stage2

    def generate_image(self, scene_card) -> GeneratedImage:
        self.image_calls += 1
        if self.image_should_fail:
            raise RuntimeError("image failure")
        return GeneratedImage(prompt_text="scene prompt", image_bytes=PNG_BYTES)


class FailingCharacterMemoryManager(MemoryManager):
    def write_character_side(self, *, stage2_result: Stage2Result, batch_id: str, now: datetime) -> None:
        raise RuntimeError("character memory write failed")


class FailingOutboundRecordStore(StateStore):
    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.fail_pending_records = False
        self.fail_sent_marks = False

    def record_pending_outbound_message(self, **kwargs) -> None:
        if self.fail_pending_records:
            raise RuntimeError("pending outbound record failed")
        return super().record_pending_outbound_message(**kwargs)

    def mark_outbound_message_sent(self, **kwargs) -> None:
        if self.fail_sent_marks:
            raise RuntimeError("outbound sent mark failed")
        return super().mark_outbound_message_sent(**kwargs)


class FailingInitializeStateStore(StateStore):
    def initialize(self) -> None:
        raise RuntimeError("initialize failed")


RAW_EMAIL = """From: User <user@example.com>
To: keqing@example.com
Subject: =?utf-8?b?5LuK5aSp5pyJ54K557Sv?=
Date: Tue, 31 Mar 2026 21:42:10 +0800
Message-ID: <msg-1@example.com>
Content-Type: text/plain; charset="utf-8"

今天写了一天作业，有点累。
""".encode("utf-8")


def build_raw_email(
    *,
    uid: int,
    from_email: str,
    message_id: str,
    date_header: str = "Tue, 31 Mar 2026 21:42:10 +0800",
) -> RawInboundMessage:
    raw = f"""From: Sender <{from_email}>
To: keqing@example.com
Subject: test-{uid}
Date: {date_header}
Message-ID: <{message_id}>
Content-Type: text/plain; charset="utf-8"

测试邮件 {uid}
""".encode("utf-8")
    return RawInboundMessage(imap_uid=uid, raw_bytes=raw)


def _set_config_language(root: Path, language: str) -> None:
    config_path = root / "config.yaml"
    config_text = config_path.read_text(encoding="utf-8")
    config_path.write_text(config_text.replace('language: "简体中文"', f'language: "{language}"'), encoding="utf-8")


class PipelineTests(unittest.TestCase):
    fixed_now = datetime(2026, 3, 31, 22, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

    def _prime_non_first_run(self, root: Path, store: StateStore) -> None:
        store.initialize()
        bootstrap_raw = root / "archive" / "bootstrap.eml"
        bootstrap_raw.parent.mkdir(parents=True, exist_ok=True)
        bootstrap_raw.write_bytes(b"bootstrap")
        store.record_outbound_message(
            message_id="<bootstrap@example.com>",
            sent_at=(self.fixed_now - timedelta(days=1)).isoformat(),
            to_email="user@example.com",
            subject="bootstrap",
            raw_path=bootstrap_raw,
            image_path=None,
            run_id="bootstrap-run",
            batch_id="initiative-2026-03-30",
        )

    def _build_pipeline(
        self,
        root: Path,
        *,
        stage2_body: str = LONG_BODY,
        scene_card_location: str = "璃月港临水长阶",
        include_scene_card: bool = True,
        stage1_should_fail: bool = False,
        stage2_should_fail: bool = False,
        image_should_fail: bool = False,
        image_enabled: bool = True,
    ) -> tuple[PipelineService, StateStore, FakeOutboundClient]:
        if not image_enabled:
            config_path = root / "config.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace("    enabled: true\n", "    enabled: false\n", 1)
            config_path.write_text(config_text, encoding="utf-8")
        settings = load_test_settings(root)
        store = StateStore(settings)
        memory_manager = MemoryManager(settings)
        self._prime_non_first_run(root, store)
        stage1 = Stage1Result.from_dict(
            {
                "user_digest": {
                    "main_event": "今天写了一天作业，有点累。",
                    "state": "疲惫",
                    "possible_needs": ["陪伴"],
                },
                "candidate_memories": [
                    {
                        "content": "今天写了一天作业，有点累。",
                        "type": "user_episode",
                        "confidence": 0.9,
                        "durability": "short",
                        "evidence": "今天写了一天作业，有点累。",
                        "why_it_matters": "用于短期连续性。",
                        "source_date": "2026-03-31",
                    }
                ],
                "emotional_priority": "comfort",
                "open_loops": [],
            }
        )
        stage2_payload = {
            "subject": "今夜先缓一缓",
            "body": stage2_body,
            "character_episode": {
                "summary": "夜里仍记挂着你的近况。",
                "continuity_value": "记得你这阵子的疲惫。",
            },
            "active_arc_update_suggestion": {
                "action": "none",
                "content": "",
            },
        }
        if include_scene_card:
            stage2_payload["scene_card"] = {
                "visual_style": "二次元插画",
                "location": scene_card_location,
                "action": "夜里整理文书",
                "key_object": "折起的公文",
                "time": "入夜后",
                "mood": "克制而安静的挂念",
                "camera_distance": "medium",
            }
        stage2 = Stage2Result.from_dict(stage2_payload)
        outbound_client = FakeOutboundClient()
        pipeline = PipelineService(
            settings,
            dependencies=PipelineDependencies(
                inbound_client=FakeInboundClient([RawInboundMessage(imap_uid=1, raw_bytes=RAW_EMAIL)]),
                outbound_client=outbound_client,
                ai_client=FakeAIClient(
                    stage1=stage1,
                    stage2=stage2,
                    stage1_should_fail=stage1_should_fail,
                    stage2_should_fail=stage2_should_fail,
                    image_should_fail=image_should_fail,
                ),
                state_store=store,
                memory_manager=memory_manager,
            ),
        )
        return pipeline, store, outbound_client

    def test_reply_run_success_persists_state_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, outbound_client = self._build_pipeline(root)
            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertTrue(outcome.success)
            self.assertEqual(outcome.mode, "reply")
            self.assertEqual(store.get_last_consumed_imap_uid(), 1)
            self.assertEqual(len(store.list_unconsumed_inbound()), 0)
            self.assertEqual(len(outbound_client.sent_messages), 1)

            user_episode_entries = load_json_file(
                pipeline.settings.documents.user_episode_dir / "2026-03-31.json",
                default=[],
            )
            character_episode_entries = load_json_file(
                pipeline.settings.documents.character_episode_dir / "2026-03-31.json",
                default=[],
            )
            self.assertEqual(len(user_episode_entries), 1)
            self.assertEqual(len(character_episode_entries), 1)
            self.assertEqual(
                user_episode_entries[0]["episode"],
                {
                    "main_event": "今天写了一天作业，有点累。",
                    "state": "疲惫",
                    "possible_needs": ["陪伴"],
                },
            )
            self.assertEqual(
                character_episode_entries[0]["episode"],
                {
                    "summary": "夜里仍记挂着你的近况。",
                    "continuity_value": "记得你这阵子的疲惫。",
                },
            )
            run_dir = pipeline.settings.documents.session_archive_dir / "2026-03-31" / outcome.run_id
            self.assertTrue((run_dir / "inbox_raw" / "uid-1.eml").exists())
            normalized_inbound_messages = load_json_file(run_dir / "normalized_inbound_messages.json", default=[])
            self.assertEqual(len(normalized_inbound_messages), 1)
            self.assertEqual(normalized_inbound_messages[0]["message_id"], "<msg-1@example.com>")
            self.assertEqual(normalized_inbound_messages[0]["from"], "user@example.com")
            self.assertEqual(normalized_inbound_messages[0]["body_text"], "今天写了一天作业，有点累。")
            self.assertTrue((run_dir / "outbox_raw.eml").exists())
            self.assertTrue((run_dir / "stage1.json").exists())
            self.assertTrue((run_dir / "stage2.json").exists())
            self.assertTrue((run_dir / "scene.png").exists())

    def test_english_stage1_payload_uses_english_memory_promotion_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            _set_config_language(root, "English")
            pipeline, _, _ = self._build_pipeline(root)

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertTrue(outcome.success)
            ai_client = pipeline.dependencies.ai_client
            memory_rules = ai_client.stage1_payloads[0]["docs"]["memory_promotion_rules"]
            self.assertIn("Remember less, not more.", memory_rules)
            self.assertNotIn("少记，不乱记。", memory_rules)

    def test_stage2_payload_includes_recent_scene_cards_for_image_diversity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, _ = self._build_pipeline(root)
            older_card = {
                "visual_style": "二次元插画",
                "location": "玉京台案桌",
                "action": "夜里写信",
                "key_object": "信纸",
                "time": "入夜后",
                "mood": "安静",
                "camera_distance": "medium",
            }
            newer_card = {
                "visual_style": "二次元插画",
                "location": "璃月港码头",
                "action": "查看货运记录",
                "key_object": "货运清单",
                "time": "傍晚",
                "mood": "专注",
                "camera_distance": "wide",
            }
            store.record_asset(
                asset_id="asset-old",
                run_id="run-old",
                image_path=root / "old.png",
                scene_card=older_card,
                prompt_text="old prompt",
                created_at="2026-03-29T20:00:00-07:00",
            )
            store.record_asset(
                asset_id="asset-new",
                run_id="run-new",
                image_path=root / "new.png",
                scene_card=newer_card,
                prompt_text="new prompt",
                created_at="2026-03-30T20:00:00-07:00",
            )

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertTrue(outcome.success)
            ai_client = pipeline.dependencies.ai_client
            self.assertEqual(
                ai_client.stage2_payloads[0]["recent_scene_cards"],
                [
                    {"created_at": "2026-03-30T20:00:00-07:00", "scene_card": newer_card},
                    {"created_at": "2026-03-29T20:00:00-07:00", "scene_card": older_card},
                ],
            )

    def test_stage2_validation_failure_keeps_inbound_unconsumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, _ = self._build_pipeline(root, scene_card_location="")
            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertFalse(outcome.success)
            self.assertEqual(outcome.error_stage, "validation")
            self.assertEqual(len(store.list_unconsumed_inbound()), 1)
            self.assertEqual(store.get_last_consumed_imap_uid(), 0)
            run_dir = pipeline.settings.documents.session_archive_dir / "2026-03-31" / outcome.run_id
            self.assertTrue((run_dir / "normalized_inbound_messages.json").exists())

    def test_image_generation_failure_fails_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, outbound_client = self._build_pipeline(root, image_should_fail=True)
            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertFalse(outcome.success)
            self.assertEqual(outcome.error_stage, "image")
            self.assertFalse(outcome.image_generated)
            self.assertEqual(len(outbound_client.sent_messages), 0)
            self.assertEqual(len(store.list_unconsumed_inbound()), 1)

    def test_disabled_image_generation_sends_text_only_email_without_scene_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, outbound_client = self._build_pipeline(
                root,
                image_enabled=False,
                include_scene_card=False,
                image_should_fail=True,
            )
            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertTrue(outcome.success)
            self.assertFalse(outcome.image_generated)
            self.assertEqual(len(outbound_client.sent_messages), 1)
            self.assertEqual(len(store.list_unconsumed_inbound()), 0)
            run_dir = pipeline.settings.documents.session_archive_dir / "2026-03-31" / outcome.run_id
            self.assertFalse((run_dir / "scene.png").exists())
            stage2_payload = load_json_file(run_dir / "stage2.json", default={})
            self.assertIsNone(stage2_payload["scene_card"])

    def test_disabled_image_generation_accepts_legacy_scene_card_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, outbound_client = self._build_pipeline(root, image_enabled=False, image_should_fail=True)
            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertTrue(outcome.success)
            self.assertFalse(outcome.image_generated)
            self.assertEqual(len(outbound_client.sent_messages), 1)
            self.assertEqual(len(store.list_unconsumed_inbound()), 0)
            run_dir = pipeline.settings.documents.session_archive_dir / "2026-03-31" / outcome.run_id
            self.assertFalse((run_dir / "scene.png").exists())

    def test_image_generation_enabled_requires_scene_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, outbound_client = self._build_pipeline(root, include_scene_card=False)
            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertFalse(outcome.success)
            self.assertEqual(outcome.error_stage, "validation")
            self.assertFalse(outcome.image_generated)
            self.assertEqual(len(outbound_client.sent_messages), 0)
            self.assertEqual(len(store.list_unconsumed_inbound()), 1)

    def test_stage2_generation_failure_is_classified_as_stage2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, outbound_client = self._build_pipeline(root, stage2_should_fail=True)
            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertFalse(outcome.success)
            self.assertEqual(outcome.error_stage, "stage2")
            self.assertEqual(len(outbound_client.sent_messages), 0)
            self.assertEqual(len(store.list_unconsumed_inbound()), 1)

    def test_reply_run_does_not_advance_cursor_past_consumed_inbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            memory_manager = MemoryManager(settings)
            self._prime_non_first_run(root, store)
            inbound_messages = [build_raw_email(uid=21, from_email="user@example.com", message_id="msg-21@example.com")]
            inbound_messages.extend(
                build_raw_email(uid=uid, from_email=f"bulk-{uid}@example.net", message_id=f"msg-{uid}@example.net")
                for uid in range(22, 42)
            )
            stage1 = Stage1Result.from_dict(
                {
                    "user_digest": {
                        "main_event": "测试邮件 21",
                        "state": "平静",
                        "possible_needs": ["回应"],
                    },
                    "candidate_memories": [],
                    "emotional_priority": "companionship",
                    "open_loops": [],
                }
            )
            stage2 = Stage2Result.from_dict(
                {
                    "subject": "今夜先缓一缓",
                    "body": LONG_BODY,
                    "character_episode": {
                        "summary": "夜里仍记挂着你的近况。",
                        "continuity_value": "记得你这阵子的疲惫。",
                    },
                    "active_arc_update_suggestion": {
                        "action": "none",
                        "content": "",
                    },
                    "scene_card": {
                        "visual_style": "二次元插画",
                        "location": "璃月港临水长阶",
                        "action": "夜里整理文书",
                        "key_object": "折起的公文",
                        "time": "入夜后",
                        "mood": "克制而安静的挂念",
                        "camera_distance": "medium",
                    },
                }
            )
            pipeline = PipelineService(
                settings,
                dependencies=PipelineDependencies(
                    inbound_client=FakeInboundClient(inbound_messages),
                    outbound_client=FakeOutboundClient(),
                    ai_client=FakeAIClient(stage1=stage1, stage2=stage2),
                    state_store=store,
                    memory_manager=memory_manager,
                ),
            )

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertTrue(outcome.success)
            self.assertEqual(outcome.mode, "reply")
            self.assertEqual(store.get_last_consumed_imap_uid(), 21)

    def test_empty_inbound_scan_does_not_advance_imap_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, _ = self._build_pipeline(root)
            pipeline.dependencies.inbound_client = FakeInboundClient(
                [build_raw_email(uid=22, from_email="bulk@example.net", message_id="msg-22@example.net")]
            )

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertTrue(outcome.success)
            self.assertEqual(outcome.mode, "initiative")
            self.assertEqual(store.get_last_consumed_imap_uid(), 0)
            self.assertEqual(len(store.list_unconsumed_inbound()), 0)

    def test_first_run_skips_inbox_and_forces_initiative_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            memory_manager = MemoryManager(settings)
            inbound_client = FakeInboundClient([RawInboundMessage(imap_uid=1, raw_bytes=RAW_EMAIL)])
            stage1 = Stage1Result.from_dict(
                {
                    "user_digest": {
                        "main_event": "今天没有收到新的来信。",
                        "state": "适合主动维系联系",
                        "possible_needs": ["陪伴"],
                    },
                    "candidate_memories": [],
                    "emotional_priority": "companionship",
                    "open_loops": [],
                }
            )
            stage2 = Stage2Result.from_dict(
                {
                    "subject": "今夜先缓一缓",
                    "body": LONG_BODY,
                    "character_episode": {
                        "summary": "夜里仍记挂着你的近况。",
                        "continuity_value": "记得你这阵子的疲惫。",
                    },
                    "active_arc_update_suggestion": {
                        "action": "none",
                        "content": "",
                    },
                    "scene_card": {
                        "visual_style": "二次元插画",
                        "location": "璃月港临水长阶",
                        "action": "夜里整理文书",
                        "key_object": "折起的公文",
                        "time": "入夜后",
                        "mood": "克制而安静的挂念",
                        "camera_distance": "medium",
                    },
                }
            )
            pipeline = PipelineService(
                settings,
                dependencies=PipelineDependencies(
                    inbound_client=inbound_client,
                    outbound_client=FakeOutboundClient(),
                    ai_client=FakeAIClient(stage1=stage1, stage2=stage2),
                    state_store=store,
                    memory_manager=memory_manager,
                ),
            )

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertTrue(outcome.success)
            self.assertEqual(outcome.mode, "initiative")
            self.assertEqual(inbound_client.calls, [])
            self.assertEqual(store.get_last_consumed_imap_uid(), 0)

    def test_non_first_run_uses_first_outbound_date_as_mail_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            self._prime_non_first_run(root, store)
            memory_manager = MemoryManager(settings)
            inbound_client = FakeInboundClient([RawInboundMessage(imap_uid=2, raw_bytes=RAW_EMAIL)])
            stage1 = Stage1Result.from_dict(
                {
                    "user_digest": {
                        "main_event": "今天写了一天作业，有点累。",
                        "state": "疲惫",
                        "possible_needs": ["陪伴"],
                    },
                    "candidate_memories": [],
                    "emotional_priority": "comfort",
                    "open_loops": [],
                }
            )
            stage2 = Stage2Result.from_dict(
                {
                    "subject": "今夜先缓一缓",
                    "body": LONG_BODY,
                    "character_episode": {
                        "summary": "夜里仍记挂着你的近况。",
                        "continuity_value": "记得你这阵子的疲惫。",
                    },
                    "active_arc_update_suggestion": {
                        "action": "none",
                        "content": "",
                    },
                    "scene_card": {
                        "visual_style": "二次元插画",
                        "location": "璃月港临水长阶",
                        "action": "夜里整理文书",
                        "key_object": "折起的公文",
                        "time": "入夜后",
                        "mood": "克制而安静的挂念",
                        "camera_distance": "medium",
                    },
                }
            )
            pipeline = PipelineService(
                settings,
                dependencies=PipelineDependencies(
                    inbound_client=inbound_client,
                    outbound_client=FakeOutboundClient(),
                    ai_client=FakeAIClient(stage1=stage1, stage2=stage2),
                    state_store=store,
                    memory_manager=memory_manager,
                ),
            )

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertTrue(outcome.success)
            self.assertEqual(inbound_client.calls[0]["since_uid"], 0)
            self.assertEqual(inbound_client.calls[0]["received_since"], (self.fixed_now - timedelta(days=1)).date())

    def test_non_allowlist_scan_advances_seen_cursor_without_consuming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, _outbound_client = self._build_pipeline(root)
            inbound_client = FakeInboundClient(
                [build_raw_email(uid=4, from_email="spam@example.com", message_id="spam-4@example.com")]
            )
            pipeline.dependencies.inbound_client = inbound_client

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertTrue(outcome.success)
            self.assertEqual(inbound_client.calls[0]["since_uid"], 0)
            self.assertEqual(store.get_last_seen_imap_uid(), 4)
            self.assertEqual(store.get_last_consumed_imap_uid(), 0)
            self.assertEqual(store.list_unconsumed_inbound(), [])

            next_client = FakeInboundClient([])
            pipeline.dependencies.inbound_client = next_client
            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now + timedelta(minutes=5)):
                pipeline.run_once()

            self.assertEqual(next_client.calls[0]["since_uid"], 4)

    def test_allowlist_message_in_middle_of_seen_range_is_processed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, outbound_client = self._build_pipeline(root)
            pipeline.dependencies.inbound_client = FakeInboundClient(
                [
                    build_raw_email(uid=120, from_email="user@example.com", message_id="user-120@example.com"),
                    build_raw_email(uid=200, from_email="spam@example.com", message_id="spam-200@example.com"),
                ]
            )

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertTrue(outcome.success)
            self.assertEqual(store.get_last_seen_imap_uid(), 200)
            self.assertEqual(store.get_last_consumed_imap_uid(), 120)
            self.assertEqual(store.list_unconsumed_inbound(), [])
            self.assertEqual(len(outbound_client.sent_messages), 1)

    def test_first_run_cutoff_skips_backlog_and_advances_seen_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, _outbound_client = self._build_pipeline(root)
            pipeline.dependencies.inbound_client = FakeInboundClient(
                [
                    build_raw_email(
                        uid=7,
                        from_email="user@example.com",
                        message_id="old-user@example.com",
                        date_header="Mon, 30 Mar 2026 20:00:00 -0700",
                    )
                ]
            )

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertTrue(outcome.success)
            self.assertEqual(store.get_last_seen_imap_uid(), 7)
            self.assertEqual(store.get_last_consumed_imap_uid(), 0)
            self.assertEqual(store.list_unconsumed_inbound(), [])

    def test_unconsumed_local_inbound_is_processed_after_seen_cursor_advances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            config_path = root / "config.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_path.write_text(
                config_text.replace(
                    '  allowlist_senders:\n    - "user@example.com"',
                    '  allowlist_senders:\n    - "user@example.com"\n    - "friend@example.com"',
                ),
                encoding="utf-8",
            )
            pipeline, store, outbound_client = self._build_pipeline(root)
            first_client = FakeInboundClient(
                [
                    build_raw_email(uid=1, from_email="user@example.com", message_id="msg-user@example.com"),
                    build_raw_email(uid=2, from_email="friend@example.com", message_id="msg-friend@example.com"),
                ]
            )
            pipeline.dependencies.inbound_client = first_client

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                first_outcome = pipeline.run_once()

            self.assertTrue(first_outcome.success)
            self.assertEqual(store.get_last_seen_imap_uid(), 2)
            self.assertEqual(store.get_last_consumed_imap_uid(), 1)
            self.assertEqual([item.imap_uid for item in store.list_unconsumed_inbound()], [2])

            second_client = FakeInboundClient([])
            pipeline.dependencies.inbound_client = second_client
            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now + timedelta(minutes=5)):
                second_outcome = pipeline.run_once()

            self.assertTrue(second_outcome.success)
            self.assertEqual(second_client.calls[0]["since_uid"], 2)
            self.assertEqual(len(outbound_client.sent_messages), 2)
            self.assertEqual(store.get_last_seen_imap_uid(), 2)
            self.assertEqual(store.get_last_consumed_imap_uid(), 2)
            self.assertEqual(store.list_unconsumed_inbound(), [])

    def test_missing_stage1_required_document_fails_before_ai_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            (root / "docs" / "memory_promotion_rules.md").unlink()
            pipeline, _store, _outbound_client = self._build_pipeline(root)

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertFalse(outcome.success)
            self.assertEqual(outcome.error_stage, "stage1")
            self.assertIn("Required text document is missing", outcome.message)
            self.assertIn("memory_promotion_rules.md", outcome.message)
            self.assertEqual(pipeline.dependencies.ai_client.stage1_calls, 0)

    def test_missing_stage2_required_document_fails_before_stage2_ai_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            (root / "profiles" / "characters" / "keqing" / "prompt" / "persona.md").unlink()
            pipeline, _store, _outbound_client = self._build_pipeline(root)

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertFalse(outcome.success)
            self.assertEqual(outcome.error_stage, "stage2")
            self.assertIn("Required text document is missing", outcome.message)
            self.assertIn("persona.md", outcome.message)
            self.assertEqual(pipeline.dependencies.ai_client.stage1_calls, 1)
            self.assertEqual(pipeline.dependencies.ai_client.stage2_calls, 0)

    def test_recorded_outbound_prevents_duplicate_send_after_later_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            failing_pipeline, store, outbound_client = self._build_pipeline(root)
            failing_pipeline.dependencies.ai_client.stage2.active_arc_update_suggestion.action = "update"
            failing_pipeline.dependencies.ai_client.stage2.active_arc_update_suggestion.content = "用户仍在准备考试"
            failing_pipeline.dependencies.memory_manager = FailingCharacterMemoryManager(failing_pipeline.settings)

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                first_outcome = failing_pipeline.run_once()

            self.assertFalse(first_outcome.success)
            self.assertEqual(first_outcome.error_stage, "pipeline")
            self.assertEqual(len(outbound_client.sent_messages), 1)
            self.assertEqual(len(store.list_unconsumed_inbound()), 1)

            retry_pipeline, _, _ = self._build_pipeline(root, stage1_should_fail=True, stage2_should_fail=True)
            retry_ai_client = retry_pipeline.dependencies.ai_client
            retry_pipeline.dependencies.state_store = store
            retry_pipeline.dependencies.outbound_client = outbound_client

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                retry_outcome = retry_pipeline.run_once()

            self.assertTrue(retry_outcome.success)
            self.assertEqual(retry_outcome.outbound_message_id, first_outcome.outbound_message_id)
            self.assertEqual(len(outbound_client.sent_messages), 1)
            self.assertEqual(retry_ai_client.stage1_calls, 0)
            self.assertEqual(len(store.list_unconsumed_inbound()), 0)
            self.assertEqual(store.get_last_consumed_imap_uid(), 1)
            character_episode_entries = load_json_file(
                retry_pipeline.settings.documents.character_episode_dir / "2026-03-31.json",
                default=[],
            )
            active_arc_entries = load_entries(retry_pipeline.settings.documents.active_arc)
            self.assertEqual(len(character_episode_entries), 1)
            self.assertEqual(active_arc_entries[0]["content"], "用户仍在准备考试")

    def test_reply_run_processes_only_earliest_sender_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            config_path = root / "config.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_path.write_text(
                config_text.replace(
                    '  allowlist_senders:\n    - "user@example.com"',
                    '  allowlist_senders:\n    - "user@example.com"\n    - "friend@example.com"',
                ),
                encoding="utf-8",
            )
            pipeline, store, outbound_client = self._build_pipeline(root)
            pipeline.dependencies.inbound_client = FakeInboundClient(
                [
                    build_raw_email(uid=1, from_email="user@example.com", message_id="msg-user@example.com"),
                    build_raw_email(uid=2, from_email="friend@example.com", message_id="msg-friend@example.com"),
                ]
            )

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertTrue(outcome.success)
            self.assertEqual(len(outbound_client.sent_messages), 1)
            self.assertEqual(pipeline.dependencies.ai_client.stage1_calls, 1)
            inbound_payload = pipeline.dependencies.ai_client.stage1_payloads[0]["inbound_messages"]
            self.assertEqual([item["from"] for item in inbound_payload], ["user@example.com"])
            self.assertEqual([item.imap_uid for item in store.list_unconsumed_inbound()], [2])
            self.assertEqual(store.get_last_consumed_imap_uid(), 1)

    def test_inbound_sender_batch_does_not_rewind_cursor_for_later_uid_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            config_path = root / "config.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_path.write_text(
                config_text.replace(
                    '  allowlist_senders:\n    - "user@example.com"',
                    '  allowlist_senders:\n    - "user@example.com"\n    - "friend@example.com"',
                ),
                encoding="utf-8",
            )
            pipeline, store, outbound_client = self._build_pipeline(root)
            pipeline.dependencies.inbound_client = FakeInboundClient(
                [
                    build_raw_email(uid=1, from_email="user@example.com", message_id="msg-user-1@example.com"),
                    build_raw_email(uid=2, from_email="friend@example.com", message_id="msg-friend@example.com"),
                    build_raw_email(uid=3, from_email="user@example.com", message_id="msg-user-3@example.com"),
                ]
            )

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                first_outcome = pipeline.run_once()

            self.assertTrue(first_outcome.success)
            self.assertEqual([item.imap_uid for item in store.list_unconsumed_inbound()], [2])
            self.assertEqual(store.get_last_consumed_imap_uid(), 3)

            pipeline.dependencies.inbound_client = FakeInboundClient([])
            next_now = self.fixed_now + timedelta(minutes=5)
            with patch("dropline.pipeline.now_in_timezone", return_value=next_now):
                second_outcome = pipeline.run_once()

            self.assertTrue(second_outcome.success)
            self.assertEqual(len(outbound_client.sent_messages), 2)
            self.assertEqual([item.imap_uid for item in store.list_unconsumed_inbound()], [])
            self.assertEqual(store.get_last_consumed_imap_uid(), 3)

    def test_validate_inbound_batch_rejects_multiple_senders_defensively(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, _, _ = self._build_pipeline(root)
            context = RunContext(
                now=self.fixed_now,
                run_id="run-defensive-validation",
                mode="reply",
                batch_id="batch-defensive-validation",
                inbound_messages=[
                    NormalizedInboundMessage(
                        message_id="<msg-user@example.com>",
                        imap_uid=1,
                        from_email="user@example.com",
                        subject="user",
                        received_at="2026-03-31T21:42:10+08:00",
                        body_text="user",
                        body_html_present=False,
                        attachments=[],
                    ),
                    NormalizedInboundMessage(
                        message_id="<msg-friend@example.com>",
                        imap_uid=2,
                        from_email="friend@example.com",
                        subject="friend",
                        received_at="2026-03-31T21:43:10+08:00",
                        body_text="friend",
                        body_html_present=False,
                        attachments=[],
                    ),
                ],
            )

            with self.assertRaisesRegex(ModelValidationError, "multiple senders"):
                pipeline._validate_inbound_batch(context)

    def test_second_run_filters_same_day_backlog_before_first_outbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            memory_manager = MemoryManager(settings)
            first_inbound_client = FakeInboundClient(
                [build_raw_email(uid=1, from_email="user@example.com", message_id="old@example.com")]
            )
            stage1 = Stage1Result.from_dict(
                {
                    "user_digest": {
                        "main_event": "今天没有收到新的来信。",
                        "state": "适合主动维系联系",
                        "possible_needs": ["陪伴"],
                    },
                    "candidate_memories": [],
                    "emotional_priority": "companionship",
                    "open_loops": [],
                }
            )
            stage2 = Stage2Result.from_dict(
                {
                    "subject": "今夜先缓一缓",
                    "body": LONG_BODY,
                    "character_episode": {
                        "summary": "夜里仍记挂着你的近况。",
                        "continuity_value": "记得你这阵子的疲惫。",
                    },
                    "active_arc_update_suggestion": {
                        "action": "none",
                        "content": "",
                    },
                    "scene_card": {
                        "visual_style": "二次元插画",
                        "location": "璃月港临水长阶",
                        "action": "夜里整理文书",
                        "key_object": "折起的公文",
                        "time": "入夜后",
                        "mood": "克制而安静的挂念",
                        "camera_distance": "medium",
                    },
                }
            )
            pipeline = PipelineService(
                settings,
                dependencies=PipelineDependencies(
                    inbound_client=first_inbound_client,
                    outbound_client=FakeOutboundClient(),
                    ai_client=FakeAIClient(stage1=stage1, stage2=stage2),
                    state_store=store,
                    memory_manager=memory_manager,
                ),
            )

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                first_outcome = pipeline.run_once()

            self.assertTrue(first_outcome.success)
            self.assertEqual(first_inbound_client.calls, [])

            pipeline.dependencies.inbound_client = FakeInboundClient(
                [
                    build_raw_email(
                        uid=1,
                        from_email="user@example.com",
                        message_id="old@example.com",
                        date_header="Tue, 31 Mar 2026 21:30:00 -0700",
                    ),
                    build_raw_email(
                        uid=2,
                        from_email="user@example.com",
                        message_id="new@example.com",
                        date_header="Tue, 31 Mar 2026 22:30:00 -0700",
                    ),
                ]
            )
            second_now = self.fixed_now + timedelta(hours=1)
            with patch("dropline.pipeline.now_in_timezone", return_value=second_now):
                second_outcome = pipeline.run_once()

            self.assertTrue(second_outcome.success)
            self.assertEqual(pipeline.dependencies.ai_client.stage1_payloads[-1]["inbound_messages"][0]["message_id"], "<new@example.com>")
            self.assertEqual(store.get_last_consumed_imap_uid(), 2)

    def test_pending_outbound_reuses_raw_message_after_sent_mark_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = FailingOutboundRecordStore(settings)
            self._prime_non_first_run(root, store)
            store.fail_sent_marks = True
            outbound_client = FakeOutboundClient()
            pipeline, _, _ = self._build_pipeline(root)
            pipeline.dependencies.state_store = store
            pipeline.dependencies.outbound_client = outbound_client

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                first_outcome = pipeline.run_once()

            self.assertFalse(first_outcome.success)
            self.assertEqual(first_outcome.error_stage, "smtp")
            self.assertEqual(len(outbound_client.sent_messages), 1)
            first_sent_message_id = outbound_client.sent_messages[0]["Message-ID"]
            self.assertEqual(first_outcome.outbound_message_id, first_sent_message_id)
            pending_outbound = store.get_outbound_message_for_batch(first_outcome.batch_id)
            self.assertIsNotNone(pending_outbound)
            self.assertEqual(pending_outbound.delivery_status, "pending")
            self.assertEqual(len(store.list_unconsumed_inbound()), 1)

            store.fail_sent_marks = False
            retry_pipeline, _, _ = self._build_pipeline(root)
            retry_pipeline.dependencies.state_store = store
            retry_pipeline.dependencies.outbound_client = outbound_client
            retry_pipeline.dependencies.ai_client.stage1_should_fail = True
            retry_pipeline.dependencies.ai_client.stage2_should_fail = True

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                retry_outcome = retry_pipeline.run_once()

            self.assertTrue(retry_outcome.success)
            self.assertEqual(len(outbound_client.sent_messages), 2)
            self.assertEqual(outbound_client.sent_messages[1]["Message-ID"], first_sent_message_id)
            self.assertEqual(retry_pipeline.dependencies.ai_client.stage1_calls, 0)
            self.assertEqual(retry_pipeline.dependencies.ai_client.stage2_calls, 0)
            sent_outbound = store.get_outbound_message_for_batch(retry_outcome.batch_id)
            self.assertIsNotNone(sent_outbound)
            self.assertEqual(sent_outbound.delivery_status, "sent")
            self.assertEqual(len(store.list_unconsumed_inbound()), 0)

    def test_pending_outbound_record_failure_does_not_send_or_consume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = FailingOutboundRecordStore(settings)
            self._prime_non_first_run(root, store)
            store.fail_pending_records = True
            outbound_client = FakeOutboundClient()
            pipeline, _, _ = self._build_pipeline(root)
            pipeline.dependencies.state_store = store
            pipeline.dependencies.outbound_client = outbound_client

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertFalse(outcome.success)
            self.assertEqual(outcome.error_stage, "smtp")
            self.assertEqual(len(outbound_client.sent_messages), 0)
            self.assertIsNone(store.get_outbound_message_for_batch(outcome.batch_id))
            self.assertEqual(len(store.list_unconsumed_inbound()), 1)

    def test_prepare_run_failure_returns_failed_outcome_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = FailingInitializeStateStore(settings)
            memory_manager = MemoryManager(settings)
            stage1 = Stage1Result.from_dict(
                {
                    "user_digest": {"main_event": "", "state": "", "possible_needs": []},
                    "candidate_memories": [],
                    "emotional_priority": "comfort",
                    "open_loops": [],
                }
            )
            stage2 = Stage2Result.from_dict(
                {
                    "subject": "今夜先缓一缓",
                    "body": LONG_BODY,
                    "character_episode": {
                        "summary": "夜里仍记挂着你的近况。",
                        "continuity_value": "记得你这阵子的疲惫。",
                    },
                    "active_arc_update_suggestion": {
                        "action": "none",
                        "content": "",
                    },
                    "scene_card": {
                        "visual_style": "二次元插画",
                        "location": "璃月港临水长阶",
                        "action": "夜里整理文书",
                        "key_object": "折起的公文",
                        "time": "入夜后",
                        "mood": "克制而安静的挂念",
                        "camera_distance": "medium",
                    },
                }
            )
            pipeline = PipelineService(
                settings,
                dependencies=PipelineDependencies(
                    inbound_client=FakeInboundClient([]),
                    outbound_client=FakeOutboundClient(),
                    ai_client=FakeAIClient(stage1=stage1, stage2=stage2),
                    state_store=store,
                    memory_manager=memory_manager,
                ),
            )

            outcome = pipeline.run_once()

            self.assertFalse(outcome.success)
            self.assertEqual(outcome.error_stage, "prepare")
            self.assertIn("initialize failed", outcome.message)
            self.assertEqual(pipeline.dependencies.ai_client.stage1_calls, 0)


if __name__ == "__main__":
    unittest.main()
