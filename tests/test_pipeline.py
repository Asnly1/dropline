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
from dropline.mail import compose_email
from dropline.memory import MemoryManager
from dropline.models import GeneratedImage, ModelValidationError, NormalizedInboundMessage, RawInboundMessage, Stage1Result, Stage2Result
from dropline.pipeline import PipelineDependencies, PipelineService, RunContext
from dropline.storage import LAST_ALERT_AT_KEY, RUNS_TODAY_KEY, StateStore

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
    def __init__(self, messages: list[RawInboundMessage], *, uidvalidity: int = 0) -> None:
        self.messages = messages
        self.uidvalidity = uidvalidity
        self.calls: list[dict] = []

    def fetch_since(
        self,
        since_uid: int,
        limit: int,
        allowlist_senders: tuple[str, ...] = (),
        received_since=None,
    ) -> tuple[list[RawInboundMessage], int, int]:
        self.calls.append(
            {
                "since_uid": since_uid,
                "limit": limit,
                "allowlist_senders": allowlist_senders,
                "received_since": received_since,
            }
        )
        # Deliberately no "> since_uid" pre-filter: the fake behaves like a server that hands back
        # everything it has, so the pipeline must prove it never re-processes an already-stored letter.
        selected = self.messages[:limit] if limit > 0 else list(self.messages)
        highest_seen_uid = max((item.imap_uid for item in self.messages), default=0)
        return selected, highest_seen_uid, self.uidvalidity


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


class FailingUserMemoryManager(MemoryManager):
    def write_user_side(self, **kwargs: object) -> None:
        raise RuntimeError("user memory write failed")


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
    body: str | None = None,
) -> RawInboundMessage:
    raw = f"""From: Sender <{from_email}>
To: keqing@example.com
Subject: test-{uid}
Date: {date_header}
Message-ID: <{message_id}>
Content-Type: text/plain; charset="utf-8"

{body if body is not None else f"测试邮件 {uid}"}
""".encode("utf-8")
    return RawInboundMessage(imap_uid=uid, raw_bytes=raw)


def build_raw_email_with_attachments(*, uid: int, message_id: str) -> RawInboundMessage:
    message = EmailMessage()
    message["From"] = "Sender <user@example.com>"
    message["To"] = "keqing@example.com"
    message["Subject"] = f"files-{uid}"
    message["Date"] = "Tue, 31 Mar 2026 21:42:10 +0800"
    message["Message-ID"] = f"<{message_id}>"
    message.set_content(f"带附件的测试邮件 {uid}")
    message.add_alternative(f"<p>带附件的测试邮件 {uid}</p>", subtype="html")
    message.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="notes.pdf")
    message.add_attachment(b"PNG!", maintype="image", subtype="png", filename="photo.png", disposition="inline")
    return RawInboundMessage(imap_uid=uid, raw_bytes=message.as_bytes())


def _count_inbound_rows(store: StateStore) -> int:
    with store._connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM inbound_messages").fetchone()[0])


def _set_config_language(root: Path, language: str) -> None:
    config_path = root / "config.yaml"
    config_text = config_path.read_text(encoding="utf-8")
    config_path.write_text(config_text.replace('language: "简体中文"', f'language: "{language}"'), encoding="utf-8")


def _replace_config_text(root: Path, old: str, new: str) -> None:
    config_path = root / "config.yaml"
    config_text = config_path.read_text(encoding="utf-8")
    if old not in config_text:
        raise AssertionError(f"config snippet not found: {old!r}")
    config_path.write_text(config_text.replace(old, new, 1), encoding="utf-8")


def _set_image_on_failure(root: Path, value: str) -> None:
    _replace_config_text(
        root,
        '    provider: "openai"\n\nopenai:',
        f'    provider: "openai"\n    on_failure: "{value}"\n\nopenai:',
    )


def _set_max_batch_attempts(root: Path, value: int) -> None:
    _replace_config_text(root, "  check_every_minutes: 10\n", f"  check_every_minutes: 10\n  max_batch_attempts: {value}\n")


def _disable_alerts(root: Path) -> None:
    _replace_config_text(
        root,
        '  primary_user_recipient: "user@example.com"\n',
        '  primary_user_recipient: "user@example.com"\n  alerts:\n    enabled: false\n',
    )


def _attachment_maintypes(message: EmailMessage) -> list[str]:
    return [part.get_content_maintype() for part in message.walk() if part.get_content_disposition() == "attachment"]


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

    def test_uidvalidity_change_resets_seen_cursor_and_rescans_without_replying_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, outbound_client = self._build_pipeline(root)
            pipeline.dependencies.inbound_client = FakeInboundClient(
                [build_raw_email(uid=5, from_email="user@example.com", message_id="first@example.com")],
                uidvalidity=100,
            )

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                first_outcome = pipeline.run_once()

            self.assertTrue(first_outcome.success)
            self.assertEqual(first_outcome.mode, "reply")
            self.assertEqual(store.get_last_seen_imap_uid(), 5)
            self.assertEqual(store.get_imap_uidvalidity(), 100)

            # The mailbox was rebuilt: the answered letter now carries UID 1 and a new letter UID 2.
            rebuilt_client = FakeInboundClient(
                [
                    build_raw_email(uid=1, from_email="user@example.com", message_id="first@example.com"),
                    build_raw_email(uid=2, from_email="user@example.com", message_id="second@example.com"),
                ],
                uidvalidity=200,
            )
            pipeline.dependencies.inbound_client = rebuilt_client
            second_now = self.fixed_now + timedelta(days=1)
            with patch("dropline.pipeline.now_in_timezone", return_value=second_now), self.assertLogs(
                "dropline.pipeline", level="ERROR"
            ) as logs:
                second_outcome = pipeline.run_once()

            self.assertTrue(second_outcome.success)
            self.assertEqual(second_outcome.mode, "reply")
            self.assertTrue(any("UIDVALIDITY changed from 100 to 200" in line for line in logs.output))
            self.assertEqual([call["since_uid"] for call in rebuilt_client.calls], [5, 0])
            self.assertIsNone(rebuilt_client.calls[0]["received_since"])
            self.assertEqual(rebuilt_client.calls[1]["received_since"], (self.fixed_now - timedelta(days=1)).date())
            self.assertEqual(store.get_imap_uidvalidity(), 200)
            self.assertEqual(store.get_last_seen_imap_uid(), 2)
            inbound_payload = pipeline.dependencies.ai_client.stage1_payloads[-1]["inbound_messages"]
            self.assertEqual([item["message_id"] for item in inbound_payload], ["<second@example.com>"])
            self.assertEqual([item["uidvalidity"] for item in inbound_payload], [200])
            self.assertEqual(len(outbound_client.sent_messages), 2)
            self.assertEqual(store.list_unconsumed_inbound(), [])
            self.assertEqual(_count_inbound_rows(store), 2)

    def test_unknown_uidvalidity_keeps_cursor_and_recorded_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, _ = self._build_pipeline(root)
            pipeline.dependencies.inbound_client = FakeInboundClient(
                [build_raw_email(uid=5, from_email="user@example.com", message_id="first@example.com")],
                uidvalidity=100,
            )

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                self.assertTrue(pipeline.run_once().success)

            silent_client = FakeInboundClient([], uidvalidity=0)
            pipeline.dependencies.inbound_client = silent_client
            with patch(
                "dropline.pipeline.now_in_timezone", return_value=self.fixed_now + timedelta(minutes=5)
            ), self.assertNoLogs("dropline.pipeline", level="ERROR"):
                outcome = pipeline.run_once()

            self.assertTrue(outcome.success)
            self.assertEqual([call["since_uid"] for call in silent_client.calls], [5])
            self.assertEqual(store.get_imap_uidvalidity(), 100)
            self.assertEqual(store.get_last_seen_imap_uid(), 5)

    def test_server_returning_already_processed_uid_does_not_reprocess_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, outbound_client = self._build_pipeline(root)
            inbound_client = pipeline.dependencies.inbound_client

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                first_outcome = pipeline.run_once()

            self.assertTrue(first_outcome.success)
            self.assertEqual(first_outcome.mode, "reply")
            self.assertEqual(store.get_last_seen_imap_uid(), 1)

            # The fake keeps handing back UID 1 (like a server answering "UID 2:*" with the highest UID).
            second_now = self.fixed_now + timedelta(minutes=5)
            with patch("dropline.pipeline.now_in_timezone", return_value=second_now), self.assertLogs(
                "dropline.pipeline", level="INFO"
            ) as logs:
                second_outcome = pipeline.run_once()

            self.assertTrue(second_outcome.success)
            self.assertEqual(second_outcome.mode, "initiative")
            self.assertEqual(inbound_client.calls[1]["since_uid"], 1)
            self.assertTrue(any("Message-ID is already stored" in line for line in logs.output))
            self.assertEqual(pipeline.dependencies.ai_client.stage1_payloads[1]["inbound_messages"], [])
            self.assertEqual(_count_inbound_rows(store), 1)
            self.assertEqual(store.list_unconsumed_inbound(), [])
            second_run_dir = pipeline.settings.documents.session_archive_dir / "2026-03-31" / second_outcome.run_id
            self.assertFalse((second_run_dir / "inbox_raw").exists())
            self.assertEqual(len(outbound_client.sent_messages), 2)

    def test_message_moved_back_to_inbox_with_new_uid_is_not_replied_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, outbound_client = self._build_pipeline(root)

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                self.assertTrue(pipeline.run_once().success)

            pipeline.dependencies.inbound_client = FakeInboundClient([RawInboundMessage(imap_uid=9, raw_bytes=RAW_EMAIL)])
            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now + timedelta(minutes=5)):
                outcome = pipeline.run_once()

            self.assertTrue(outcome.success)
            self.assertEqual(outcome.mode, "initiative")
            self.assertEqual(store.get_last_seen_imap_uid(), 9)
            self.assertEqual(store.get_last_consumed_imap_uid(), 1)
            self.assertEqual(_count_inbound_rows(store), 1)
            self.assertEqual(len(outbound_client.sent_messages), 2)

    def test_character_paragraphs_quoted_without_markers_are_stripped_from_inbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, _ = self._build_pipeline(root)

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                self.assertTrue(pipeline.run_once().success)

            quoted_reply = build_raw_email(
                uid=2,
                from_email="user@example.com",
                message_id="quoted@example.com",
                body="我看完了，谢谢你。\n\n" + LONG_BODY + "\n\n下周再写。",
            )
            pipeline.dependencies.inbound_client = FakeInboundClient([quoted_reply])
            with patch(
                "dropline.pipeline.now_in_timezone", return_value=self.fixed_now + timedelta(hours=1)
            ), self.assertLogs("dropline.pipeline", level="INFO") as logs:
                outcome = pipeline.run_once()

            self.assertTrue(outcome.success)
            self.assertEqual(outcome.mode, "reply")
            self.assertTrue(any("quoted verbatim" in line for line in logs.output))
            inbound_payload = pipeline.dependencies.ai_client.stage1_payloads[-1]["inbound_messages"]
            self.assertEqual([item["body_text"] for item in inbound_payload], ["我看完了，谢谢你。\n\n下周再写。"])
            with store._connect() as conn:
                stored_body = conn.execute("SELECT body_text FROM inbound_messages WHERE imap_uid = 2").fetchone()[0]
            self.assertEqual(stored_body, "我看完了，谢谢你。\n\n下周再写。")

    def test_quote_stripping_falls_back_to_archived_outbound_eml_when_stage2_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, _ = self._build_pipeline(root)
            letter = "角色写的第一段，用来验证能从归档邮件里恢复正文。\n\n第二段也足够长，可以当作已知文本来比对。"
            archived = compose_email(
                pipeline.settings,
                subject="旧信",
                body_text=letter,
                image_path=None,
                reply_to_message_id=None,
                mode="initiative",
            )
            raw_path = pipeline.settings.documents.session_archive_dir / "old-letter.eml"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(archived.as_bytes())
            store.record_outbound_message(
                message_id="<old-letter@example.com>",
                sent_at=(self.fixed_now - timedelta(hours=1)).isoformat(),
                to_email="user@example.com",
                subject="旧信",
                raw_path=raw_path,
                image_path=None,
                run_id="run-without-stage2",
                batch_id="batch-old-letter",
            )
            pipeline.dependencies.inbound_client = FakeInboundClient(
                [
                    build_raw_email(
                        uid=2,
                        from_email="user@example.com",
                        message_id="quoted-eml@example.com",
                        body="收到。\n\n角色写的第一段，用来验证能从归档邮件里恢复正文。\n\n我这边一切都好。",
                    )
                ]
            )

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertTrue(outcome.success)
            inbound_payload = pipeline.dependencies.ai_client.stage1_payloads[-1]["inbound_messages"]
            self.assertEqual([item["body_text"] for item in inbound_payload], ["收到。\n\n我这边一切都好。"])

    def test_retry_restores_attachment_metadata_and_html_flag_from_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, _ = self._build_pipeline(root, stage2_should_fail=True)
            pipeline.dependencies.inbound_client = FakeInboundClient(
                [build_raw_email_with_attachments(uid=3, message_id="files@example.com")]
            )

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                first_outcome = pipeline.run_once()

            self.assertFalse(first_outcome.success)
            self.assertEqual(first_outcome.error_stage, "stage2")
            first_payload = pipeline.dependencies.ai_client.stage1_payloads[0]["inbound_messages"][0]
            self.assertTrue(first_payload["body_html_present"])
            self.assertEqual(
                first_payload["attachments"],
                [
                    {"filename": "notes.pdf", "content_type": "application/pdf", "size": 8, "inline": False},
                    {"filename": "photo.png", "content_type": "image/png", "size": 4, "inline": True},
                ],
            )

            retry_pipeline, _, _ = self._build_pipeline(root)
            retry_pipeline.dependencies.state_store = store
            retry_pipeline.dependencies.inbound_client = FakeInboundClient([])
            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now + timedelta(minutes=5)):
                retry_outcome = retry_pipeline.run_once()

            self.assertTrue(retry_outcome.success)
            self.assertEqual(retry_outcome.mode, "reply")
            # The retry reuses the archived stage1.json, so Stage 1 is not called again; the message
            # restored from the store (attachment metadata, HTML flag) must still reach Stage 2 unchanged.
            self.assertEqual(retry_pipeline.dependencies.ai_client.stage1_payloads, [])
            second_payload = retry_pipeline.dependencies.ai_client.stage2_payloads[0]["inbound_messages"][0]
            self.assertEqual(second_payload, first_payload)

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

    def test_stage2_failure_records_attempt_and_failure_bookkeeping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, outbound_client = self._build_pipeline(root, stage2_should_fail=True)

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertFalse(outcome.success)
            self.assertEqual(outcome.error_stage, "stage2")
            message = store.list_unconsumed_inbound()[0]
            self.assertEqual(message.attempt_count, 1)
            self.assertEqual(message.last_error, "stage2: stage2 network failure")
            self.assertEqual(message.last_attempt_at, self.fixed_now.isoformat())
            self.assertIsNone(message.quarantined_at)
            self.assertEqual(store.get_consecutive_failures(), 1)
            self.assertEqual(store.get_last_failed_at(), self.fixed_now.isoformat())
            stored_run = store.get_run(outcome.run_id)
            self.assertEqual(stored_run.status, "failed")
            self.assertEqual(stored_run.error_message, "stage2 network failure")
            self.assertEqual(outbound_client.sent_messages, [])
            self.assertIsNone(store.get_kv(RUNS_TODAY_KEY))

    def test_repeated_failures_quarantine_inbound_alert_and_fall_back_to_initiative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, outbound_client = self._build_pipeline(root, stage2_should_fail=True)
            ai_client = pipeline.dependencies.ai_client

            for day in range(3):
                with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now + timedelta(days=day)):
                    outcome = pipeline.run_once()
                self.assertFalse(outcome.success)
                self.assertEqual(outcome.mode, "reply")

            self.assertEqual(store.list_unconsumed_inbound(), [])
            quarantined = store.list_quarantined_inbound()
            self.assertEqual([item.imap_uid for item in quarantined], [1])
            self.assertEqual(quarantined[0].attempt_count, 3)
            self.assertEqual(quarantined[0].quarantined_at, (self.fixed_now + timedelta(days=2)).isoformat())
            self.assertEqual(store.get_consecutive_failures(), 3)
            self.assertEqual(ai_client.stage1_calls, 1)
            self.assertEqual(ai_client.stage2_calls, 3)
            self.assertEqual(
                [item["Subject"] for item in outbound_client.sent_messages],
                ["[dropline] 已连续失败 2 次（阶段 stage2）", "[dropline] 来信已被隔离：uid 1（阶段 stage2）"],
            )
            for alert in outbound_client.sent_messages:
                self.assertEqual(alert["From"], "dropline <character@example.com>")
                self.assertEqual(alert["To"], "user@example.com")
                self.assertIn("系统通知，不是角色来信", alert.get_content())
            self.assertIn("已隔离的来信 UID：1", outbound_client.sent_messages[1].get_content())
            self.assertEqual(store.get_kv(LAST_ALERT_AT_KEY), (self.fixed_now + timedelta(days=2)).isoformat())

            ai_client.stage2_should_fail = False
            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now + timedelta(days=3)):
                recovered = pipeline.run_once()

            self.assertTrue(recovered.success)
            self.assertEqual(recovered.mode, "initiative")
            self.assertEqual(recovered.batch_id, "initiative-2026-04-03")
            self.assertEqual(len(outbound_client.sent_messages), 3)
            self.assertNotIn("[dropline]", outbound_client.sent_messages[2]["Subject"])
            self.assertEqual(store.get_consecutive_failures(), 0)
            self.assertIsNone(store.get_last_failed_at())
            self.assertEqual([item.imap_uid for item in store.list_quarantined_inbound()], [1])

    def test_validation_failures_quarantine_after_two_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, outbound_client = self._build_pipeline(root, scene_card_location="")

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                first = pipeline.run_once()
            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now + timedelta(hours=1)):
                second = pipeline.run_once()

            self.assertEqual(first.error_stage, "validation")
            self.assertEqual(second.error_stage, "validation")
            self.assertEqual(store.list_unconsumed_inbound(), [])
            quarantined = store.list_quarantined_inbound()
            self.assertEqual([item.imap_uid for item in quarantined], [1])
            self.assertEqual(quarantined[0].attempt_count, 2)
            self.assertTrue(quarantined[0].last_error.startswith("validation: "))
            self.assertEqual(pipeline.dependencies.ai_client.stage1_calls, 1)
            self.assertEqual(
                [item["Subject"] for item in outbound_client.sent_messages],
                ["[dropline] 来信已被隔离：uid 1（阶段 validation）"],
            )

    def test_max_batch_attempts_config_controls_quarantine_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            _set_max_batch_attempts(root, 1)
            pipeline, store, _outbound_client = self._build_pipeline(root, stage2_should_fail=True)
            self.assertEqual(pipeline.settings.schedule.max_batch_attempts, 1)

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertFalse(outcome.success)
            self.assertEqual([item.imap_uid for item in store.list_quarantined_inbound()], [1])
            self.assertEqual(store.list_unconsumed_inbound(), [])

    def test_smtp_failure_does_not_count_against_inbound_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, _outbound_client = self._build_pipeline(root)
            pipeline.dependencies.outbound_client = FakeOutboundClient(should_fail=True)

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertFalse(outcome.success)
            self.assertEqual(outcome.error_stage, "smtp")
            message = store.list_unconsumed_inbound()[0]
            self.assertEqual(message.attempt_count, 0)
            self.assertIsNone(message.last_error)
            self.assertEqual(store.get_consecutive_failures(), 1)

    def test_alerts_disabled_suppresses_failure_emails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            _disable_alerts(root)
            pipeline, store, outbound_client = self._build_pipeline(root, stage2_should_fail=True)
            self.assertFalse(pipeline.settings.mail.alerts.enabled)

            for day in range(3):
                with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now + timedelta(days=day)):
                    pipeline.run_once()

            self.assertEqual([item.imap_uid for item in store.list_quarantined_inbound()], [1])
            self.assertEqual(outbound_client.sent_messages, [])
            self.assertIsNone(store.get_kv(LAST_ALERT_AT_KEY))

    def test_retry_reuses_archived_stage2_after_image_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            failing_pipeline, store, outbound_client = self._build_pipeline(root, image_should_fail=True)

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                first_outcome = failing_pipeline.run_once()

            self.assertFalse(first_outcome.success)
            self.assertEqual(first_outcome.error_stage, "image")
            failed_run = store.get_run(first_outcome.run_id)
            self.assertTrue(failed_run.stage1_path.endswith("stage1.json"))
            self.assertTrue(failed_run.stage2_path.endswith("stage2.json"))

            retry_pipeline, _, _ = self._build_pipeline(root, stage1_should_fail=True, stage2_should_fail=True)
            retry_pipeline.dependencies.state_store = store
            retry_pipeline.dependencies.outbound_client = outbound_client
            retry_ai_client = retry_pipeline.dependencies.ai_client

            with patch(
                "dropline.pipeline.now_in_timezone",
                return_value=self.fixed_now + timedelta(minutes=5),
            ), self.assertLogs("dropline.pipeline", level="INFO") as captured:
                retry_outcome = retry_pipeline.run_once()

            self.assertTrue(retry_outcome.success)
            self.assertEqual(retry_outcome.batch_id, first_outcome.batch_id)
            self.assertTrue(retry_outcome.image_generated)
            self.assertEqual(retry_ai_client.stage1_calls, 0)
            self.assertEqual(retry_ai_client.stage2_calls, 0)
            self.assertEqual(retry_ai_client.image_calls, 1)
            self.assertEqual(len(outbound_client.sent_messages), 1)
            self.assertEqual(_attachment_maintypes(outbound_client.sent_messages[0]), ["image"])
            retry_run = store.get_run(retry_outcome.run_id)
            self.assertEqual(retry_run.stage1_path, failed_run.stage1_path)
            self.assertEqual(retry_run.stage2_path, failed_run.stage2_path)
            self.assertEqual(store.list_unconsumed_inbound(include_quarantined=True), [])
            self.assertEqual(store.get_consecutive_failures(), 0)
            user_episode_entries = load_json_file(
                retry_pipeline.settings.documents.user_episode_dir / "2026-03-31.json",
                default=[],
            )
            character_episode_entries = load_json_file(
                retry_pipeline.settings.documents.character_episode_dir / "2026-03-31.json",
                default=[],
            )
            self.assertEqual(len(user_episode_entries), 1)
            self.assertEqual(len(character_episode_entries), 1)
            output = "\n".join(captured.output)
            self.assertIn("Reusing archived Stage 1 and Stage 2 output from failed run", output)
            self.assertIn(f"reused_run_id={first_outcome.run_id}", output)

    def test_retry_reuses_archived_stage1_and_reruns_stage2_without_duplicate_user_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            failing_pipeline, store, outbound_client = self._build_pipeline(root, stage2_should_fail=True)

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                first_outcome = failing_pipeline.run_once()

            self.assertEqual(first_outcome.error_stage, "stage2")
            self.assertIsNone(store.get_run(first_outcome.run_id).stage2_path)

            retry_pipeline, _, _ = self._build_pipeline(root, stage1_should_fail=True)
            retry_pipeline.dependencies.state_store = store
            retry_pipeline.dependencies.outbound_client = outbound_client
            retry_ai_client = retry_pipeline.dependencies.ai_client

            with patch(
                "dropline.pipeline.now_in_timezone",
                return_value=self.fixed_now + timedelta(minutes=5),
            ), self.assertLogs("dropline.pipeline", level="INFO") as captured:
                retry_outcome = retry_pipeline.run_once()

            self.assertTrue(retry_outcome.success)
            self.assertEqual(retry_ai_client.stage1_calls, 0)
            self.assertEqual(retry_ai_client.stage2_calls, 1)
            self.assertEqual(len(outbound_client.sent_messages), 1)
            user_episode_entries = load_json_file(
                retry_pipeline.settings.documents.user_episode_dir / "2026-03-31.json",
                default=[],
            )
            self.assertEqual(len(user_episode_entries), 1)
            retry_run = store.get_run(retry_outcome.run_id)
            self.assertEqual(retry_run.stage1_path, store.get_run(first_outcome.run_id).stage1_path)
            self.assertTrue(retry_run.stage2_path.endswith(f"{retry_outcome.run_id}/stage2.json"))
            self.assertIn("Reusing archived Stage 1 output from failed run", "\n".join(captured.output))

    def test_retry_does_not_reuse_stage1_when_user_memory_write_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            failing_pipeline, store, outbound_client = self._build_pipeline(root)
            failing_pipeline.dependencies.memory_manager = FailingUserMemoryManager(failing_pipeline.settings)

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                first_outcome = failing_pipeline.run_once()

            self.assertFalse(first_outcome.success)
            self.assertEqual(first_outcome.error_stage, "stage1")
            self.assertTrue(store.get_run(first_outcome.run_id).stage1_path.endswith("stage1.json"))

            retry_pipeline, _, _ = self._build_pipeline(root)
            retry_pipeline.dependencies.state_store = store
            retry_pipeline.dependencies.outbound_client = outbound_client

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now + timedelta(minutes=5)):
                retry_outcome = retry_pipeline.run_once()

            self.assertTrue(retry_outcome.success)
            self.assertEqual(retry_pipeline.dependencies.ai_client.stage1_calls, 1)
            self.assertEqual(retry_pipeline.dependencies.ai_client.stage2_calls, 1)
            user_episode_entries = load_json_file(
                retry_pipeline.settings.documents.user_episode_dir / "2026-03-31.json",
                default=[],
            )
            self.assertEqual(len(user_episode_entries), 1)

    def test_retry_ignores_unparsable_archived_stage2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            failing_pipeline, store, outbound_client = self._build_pipeline(root, image_should_fail=True)

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                first_outcome = failing_pipeline.run_once()

            Path(store.get_run(first_outcome.run_id).stage2_path).write_text("{not json", encoding="utf-8")
            retry_pipeline, _, _ = self._build_pipeline(root, stage1_should_fail=True)
            retry_pipeline.dependencies.state_store = store
            retry_pipeline.dependencies.outbound_client = outbound_client

            with patch(
                "dropline.pipeline.now_in_timezone",
                return_value=self.fixed_now + timedelta(minutes=5),
            ), self.assertLogs("dropline.pipeline", level="WARNING") as captured:
                retry_outcome = retry_pipeline.run_once()

            self.assertTrue(retry_outcome.success)
            self.assertEqual(retry_pipeline.dependencies.ai_client.stage1_calls, 0)
            self.assertEqual(retry_pipeline.dependencies.ai_client.stage2_calls, 1)
            self.assertIn("could not be read", "\n".join(captured.output))

    def test_image_failure_with_text_only_fallback_sends_plain_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            _set_image_on_failure(root, "text_only")
            pipeline, store, outbound_client = self._build_pipeline(root, image_should_fail=True)
            self.assertEqual(pipeline.settings.llm.image.on_failure, "text_only")

            with patch(
                "dropline.pipeline.now_in_timezone",
                return_value=self.fixed_now,
            ), self.assertLogs("dropline.pipeline", level="INFO") as captured:
                outcome = pipeline.run_once()

            self.assertTrue(outcome.success)
            self.assertFalse(outcome.image_generated)
            self.assertEqual(len(outbound_client.sent_messages), 1)
            self.assertEqual(_attachment_maintypes(outbound_client.sent_messages[0]), [])
            self.assertEqual(store.list_unconsumed_inbound(), [])
            self.assertEqual(store.list_recent_assets(), [])
            self.assertEqual(store.get_consecutive_failures(), 0)
            run_dir = pipeline.settings.documents.session_archive_dir / "2026-03-31" / outcome.run_id
            self.assertFalse((run_dir / "scene.png").exists())
            image_failure = load_json_file(run_dir / "image_failure.json", default={})
            self.assertEqual(image_failure["on_failure"], "text_only")
            self.assertEqual(image_failure["error"], "image failure")
            output = "\n".join(captured.output)
            self.assertIn("WARNING:dropline.pipeline:", output)
            self.assertIn("continuing with a text-only email", output)
            self.assertIn("Image generation degraded to text-only email. image_degraded=True", output)
            self.assertIn("Pipeline run succeeded.", output)
            self.assertIn("image_generated=False, image_degraded=True", output)

    def test_image_failure_with_default_abort_still_fails_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            _set_image_on_failure(root, "abort")
            pipeline, store, outbound_client = self._build_pipeline(root, image_should_fail=True)

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertFalse(outcome.success)
            self.assertEqual(outcome.error_stage, "image")
            self.assertEqual(outbound_client.sent_messages, [])
            self.assertEqual(store.list_unconsumed_inbound()[0].attempt_count, 1)

    def test_successful_run_resets_consecutive_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            pipeline, store, _outbound_client = self._build_pipeline(root)
            store.record_run_failure(failed_at="2026-03-30T22:00:00-07:00")
            store.record_run_failure(failed_at="2026-03-31T20:00:00-07:00")

            with patch("dropline.pipeline.now_in_timezone", return_value=self.fixed_now):
                outcome = pipeline.run_once()

            self.assertTrue(outcome.success)
            self.assertEqual(store.get_consecutive_failures(), 0)
            self.assertIsNone(store.get_last_failed_at())
            self.assertIsNone(store.get_kv(RUNS_TODAY_KEY))


if __name__ == "__main__":
    unittest.main()
