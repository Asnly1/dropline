from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from dropline.models import (
    CastPoolProfileDraft,
    CastRelationsProfileDraft,
    CharacterProfileDraft,
    FocusRelationshipDraft,
    NormalizedInboundMessage,
    PersonaProfileDraft,
    RelationshipProfileDraft,
    TranslationLayerProfileDraft,
    TranslationMappingGroupDraft,
    TranslationProcessingModeDraft,
    TranslationReplyPatternDraft,
    WorldStateProfileDraft,
)
from dropline.profile_manager import list_character_profile_metas
from dropline.profiles import (
    delete_inbound,
    delete_outbound,
    delete_session,
    generate_character_profile,
    generate_relationship_profile,
    list_profiles,
    select_profile,
    show_active_profile,
    show_inbox,
    show_outbox,
)
from dropline.storage import StateStore
from tests.helpers import create_test_repo, load_test_settings


class _FakeProfileClient:
    def __init__(self) -> None:
        self.last_relationship_character_prompt_docs: dict[str, str] | None = None

    def generate_character_profile(
        self,
        *,
        work_title: str | None,
        character_name: str,
    ) -> CharacterProfileDraft:
        return CharacterProfileDraft(
            persona=PersonaProfileDraft(
                identity_and_role=[f"{character_name}的身份定位", "她的通信角色不是客服，也不是陪聊工具。"],
                core_values=["重视行动", "重视责任", "重视判断与分寸。"],
                personality_traits=["反应快", "自尊强", "对信任的人会更松一点。"],
                language_style=["句子整洁", "不油滑", "夸人时夸具体行为。"],
                repeatable_life_details=["会拆步骤", "会留意具体事务现场。"],
                ooc_boundaries=["不能写成恋爱手游式模板", "不能频繁卖萌。"],
            ),
            world_state=WorldStateProfileDraft(
                worldview_type=[(work_title or "现实公众人物语境") + "通信化版本"],
                time_anchor=["当前阶段", "以稳定可持续写信为主。"],
                overall_atmosphere=["整体基调应稳定、具体。"],
                character_work_domain=["围绕她真正会处理的事务展开。"],
                sustainable_storylines=["长期事务线 A", "长期事务线 B"],
                appearance_boundaries=["不要改写重大主线", "不要抢主角位。"],
                letter_scale=["每封信只聚焦一件主事。"],
                image_principles=["优先事务中的瞬间。"],
            ),
            cast_relations=CastRelationsProfileDraft(
                focus_relationships=[
                    FocusRelationshipDraft(
                        name="主要关系对象",
                        relationship_tone=["关系底色 1", "关系底色 2"],
                        writing_points=["写法要点 1", "写法要点 2"],
                        forbidden_patterns=["禁止 1", "禁止 2"],
                    )
                ]
            ),
            cast_pool=CastPoolProfileDraft(
                usage_principles=["不是越多越好，而是越有功能越好。"],
                tier_1_high_frequency=["高频角色 A", "高频角色 B"],
                tier_2_task_support=["协力角色 A"],
                tier_3_daily_life=["生活角色 A"],
                tier_4_heavyweight_low_frequency=["低频重量角色 A"],
                tier_5_background_flavor=["背景角色 A"],
                frequency_rules=["第一层：高频，但每封最多一人显性出场。"],
            ),
            translation_layer=TranslationLayerProfileDraft(
                purpose=["保留事实与情绪，不做机械古风翻译。"],
                general_principles=["忠于原意", "低调映射", "专有名词可保留。"],
                processing_modes=[
                    TranslationProcessingModeDraft(
                        name="软映射（默认）",
                        guidance=["处理高频普通现代生活词。"],
                        examples=["作业 -> 功课", "复习 -> 温习"],
                    )
                ],
                semantic_mappings=[
                    TranslationMappingGroupDraft(
                        category="学业类",
                        mappings=["作业 -> 功课", "考试 -> 测验"],
                    )
                ],
                emotion_fidelity_rules=["轻情绪轻回，重情绪才写重。"],
                replying_patterns=[
                    TranslationReplyPatternDraft(
                        scenario="她会怎样回复学业压力",
                        preferred=["帮你拆顺序", "承认辛苦是真实的。"],
                        avoid=["空泛鸡汤"],
                    )
                ],
                forbidden_patterns=["禁止把所有句子改成古风套话。"],
            ),
        )

    def generate_relationship_profile(
        self,
        *,
        character_name: str,
        character_prompt_docs: dict[str, str],
        relationship_name: str,
        relationship_description: str,
    ) -> RelationshipProfileDraft:
        self.last_relationship_character_prompt_docs = character_prompt_docs
        return RelationshipProfileDraft(
            setup_statement=["这是系统专用补设。", f"{character_name} / {relationship_name} 不改写公开主线。"],
            relationship_definition=[relationship_description, "关系深于普通朋友，但仍保留克制。"],
            shared_past=["有共同旧事。", "书信是稳定联系形式。"],
            relationship_tone=["熟、信、久、克制。"],
            attitude_toward_user=["会认真读用户来信。", "关心偏实际。"],
            how_it_affects_letters=["更少客套，更快进入正题。"],
            intimacy_threshold=["默认熟悉，但不高频恋爱表达。"],
            explicit_boundaries=["不默认婚约同居。", "不放弃原本职责。"],
            warming_up_path=["先从更坦诚地说近况开始。"],
        )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _set_config_language(root: Path, language: str) -> None:
    config_path = root / "config.yaml"
    config_text = config_path.read_text(encoding="utf-8")
    config_path.write_text(config_text.replace('language: "简体中文"', f'language: "{language}"'), encoding="utf-8")


class ProfileCLITests(unittest.TestCase):
    def test_generate_character_profile_creates_prompt_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            fake_client = _FakeProfileClient()
            output = io.StringIO()
            with patch("dropline.profiles.build_profile_generation_client", return_value=fake_client):
                with redirect_stdout(output):
                    with self.assertLogs("dropline.profiles", level="INFO") as captured_logs:
                        exit_code = generate_character_profile(
                            str(root / "config.yaml"),
                            work_title="原神",
                            character_name="甘雨",
                        )

            self.assertEqual(exit_code, 0)
            prompt_root = root / "profiles" / "characters" / "甘雨" / "prompt"
            self.assertTrue((prompt_root / "persona.md").exists())
            self.assertTrue((prompt_root / "world_state.md").exists())
            persona = (prompt_root / "persona.md").read_text(encoding="utf-8")
            cast_pool = (prompt_root / "cast_pool.md").read_text(encoding="utf-8")
            meta = json.loads((root / "profiles" / "characters" / "甘雨" / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["character_name"], "甘雨")
            self.assertIn("## 1. 身份与定位", persona)
            self.assertIn("## 2. 核心价值观", persona)
            self.assertIn("## 第一层：高频关系轴", cast_pool)
            rendered_logs = "\n".join(captured_logs.output)
            self.assertIn("Preparing character profile generation.", rendered_logs)
            self.assertIn("Requesting character profile draft from LLM.", rendered_logs)
            self.assertIn("Character profile generation completed.", rendered_logs)
            self.assertIn("Generated character profile: 甘雨", output.getvalue())

    def test_generate_character_profile_uses_english_markdown_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            _set_config_language(root, "English")
            fake_client = _FakeProfileClient()

            with patch("dropline.profiles.build_profile_generation_client", return_value=fake_client):
                exit_code = generate_character_profile(
                    str(root / "config.yaml"),
                    work_title="K-On!",
                    character_name="Mio",
                )

            self.assertEqual(exit_code, 0)
            prompt_root = root / "profiles" / "characters" / "mio" / "prompt"
            persona = (prompt_root / "persona.md").read_text(encoding="utf-8")
            cast_pool = (prompt_root / "cast_pool.md").read_text(encoding="utf-8")
            world_state = (prompt_root / "world_state.md").read_text(encoding="utf-8")
            self.assertIn("mode: Fictional work adaptation", persona)
            self.assertIn("## 1. Identity and Role", persona)
            self.assertIn("## 4. Language Style", persona)
            self.assertIn("## Tier 1: High-Frequency Relationship Axis", cast_pool)
            self.assertIn("## 1. Worldview Type", world_state)
            self.assertNotIn("## 1. 身份与定位", persona)
            self.assertNotIn("## 第一层：高频关系轴", cast_pool)

    def test_generate_character_profile_without_work_title_uses_real_person_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            fake_client = _FakeProfileClient()
            with patch("dropline.profiles.build_profile_generation_client", return_value=fake_client):
                exit_code = generate_character_profile(
                    str(root / "config.yaml"),
                    work_title=None,
                    character_name="肖战",
                )

            self.assertEqual(exit_code, 0)
            prompt_root = root / "profiles" / "characters" / "肖战" / "prompt"
            self.assertTrue((prompt_root / "persona.md").exists())
            meta = json.loads((root / "profiles" / "characters" / "肖战" / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["character_name"], "肖战")
            self.assertEqual(meta["work_title"], "")
            world_state = (prompt_root / "world_state.md").read_text(encoding="utf-8")
            self.assertIn("现实公众人物通信化", world_state)
            self.assertIn("## 1. 世界观类型", world_state)

    def test_generate_relationship_profile_creates_prompt_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            fake_client = _FakeProfileClient()
            output = io.StringIO()
            with patch("dropline.profiles.build_profile_generation_client", return_value=fake_client):
                with redirect_stdout(output):
                    with self.assertLogs("dropline.profiles", level="INFO") as captured_logs:
                        exit_code = generate_relationship_profile(
                            str(root / "config.yaml"),
                            character_slug_value="刻晴",
                            relationship_name="同窗",
                            relationship_description="同窗旧识，通信直接。",
                        )

            self.assertEqual(exit_code, 0)
            prompt_root = root / "profiles" / "characters" / "keqing" / "relationships" / "同窗" / "prompt"
            self.assertTrue((prompt_root / "relationship.md").exists())
            meta = json.loads(
                (root / "profiles" / "characters" / "keqing" / "relationships" / "同窗" / "meta.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(meta["name"], "同窗")
            self.assertEqual(meta["character_slug"], "keqing")
            body = (prompt_root / "relationship.md").read_text(encoding="utf-8")
            self.assertIn("## 1. 设定声明", body)
            self.assertIn("## 7. 亲密阈值（非常关键）", body)
            self.assertIsNotNone(fake_client.last_relationship_character_prompt_docs)
            self.assertEqual(
                set(fake_client.last_relationship_character_prompt_docs),
                {"persona", "world_state", "cast_relations", "cast_pool", "translation_layer"},
            )
            rendered_logs = "\n".join(captured_logs.output)
            self.assertIn("Preparing relationship profile generation.", rendered_logs)
            self.assertIn("Character prompt documents ready. document_count=5", rendered_logs)
            self.assertIn("Requesting relationship profile draft from LLM.", rendered_logs)
            self.assertIn("Relationship profile generation completed.", rendered_logs)
            self.assertIn("Generated relationship profile: 刻晴 / 同窗", output.getvalue())

    def test_generate_relationship_profile_uses_english_markdown_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            _set_config_language(root, "English")
            fake_client = _FakeProfileClient()

            with patch("dropline.profiles.build_profile_generation_client", return_value=fake_client):
                exit_code = generate_relationship_profile(
                    str(root / "config.yaml"),
                    character_slug_value="刻晴",
                    relationship_name="classmate",
                    relationship_description="Old classmates who write directly.",
                )

            self.assertEqual(exit_code, 0)
            prompt_root = root / "profiles" / "characters" / "keqing" / "relationships" / "classmate" / "prompt"
            body = (prompt_root / "relationship.md").read_text(encoding="utf-8")
            self.assertIn("canon_status: Non-canon supplement", body)
            self.assertIn("## 2. Relationship Definition", body)
            self.assertIn("## 7. Intimacy Threshold (Critical)", body)
            self.assertNotIn("## 2. 关系定义", body)
            self.assertNotIn("## 7. 亲密阈值（非常关键）", body)

    def test_list_profiles_hides_empty_work_title_separator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            character_root = root / "profiles" / "characters" / "xiao-zhan"
            (character_root / "prompt").mkdir(parents=True, exist_ok=True)
            for filename in ("persona.md", "world_state.md", "cast_relations.md", "cast_pool.md", "translation_layer.md"):
                (character_root / "prompt" / filename).write_text(f"# {filename}\n", encoding="utf-8")
            _write_json(
                character_root / "meta.json",
                {
                    "slug": "xiao-zhan",
                    "character_name": "肖战",
                    "work_title": "",
                    "created_at": "2026-01-01T00:00:00",
                },
            )

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = list_profiles(str(root / "config.yaml"))

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("肖战", rendered)
            self.assertNotIn("xiao-zhan:", rendered)
            self.assertNotIn("肖战 / ", rendered)
            self.assertNotIn("sessions:", rendered)

    def test_list_character_profile_metas_skips_corrupted_meta_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            broken_root = root / "profiles" / "characters" / "broken"
            _write_json(
                broken_root / "meta.json",
                {
                    "slug": "",
                    "character_name": "损坏角色",
                    "work_title": "",
                    "created_at": "2026-01-01T00:00:00",
                },
            )

            with self.assertLogs("dropline.profile_manager", level="WARNING") as captured_logs:
                metas = list_character_profile_metas(root)

            self.assertEqual([meta.slug for meta in metas], ["keqing"])
            self.assertTrue(any("broken" in message for message in captured_logs.output))

    def test_list_profiles_command_skips_corrupted_meta_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            broken_root = root / "profiles" / "characters" / "broken"
            _write_json(
                broken_root / "meta.json",
                {
                    "slug": "",
                    "character_name": "损坏角色",
                    "work_title": "",
                    "created_at": "2026-01-01T00:00:00",
                },
            )

            output = io.StringIO()
            with redirect_stdout(output):
                with self.assertLogs("dropline.profile_manager", level="WARNING"):
                    exit_code = list_profiles(str(root / "config.yaml"))

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("刻晴", rendered)
            self.assertNotIn("损坏角色", rendered)

    def test_list_profiles_handles_missing_active_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            shutil.rmtree(root / "profiles" / "characters" / "keqing" / "sessions" / "childhood-friends")

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = list_profiles(str(root / "config.yaml"))

            self.assertEqual(exit_code, 0)
            self.assertIn("Active: 刻晴 / 原神 + 青梅竹马 (session missing)", output.getvalue())

    def test_show_active_profile_handles_missing_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            shutil.rmtree(root / "profiles" / "characters" / "keqing" / "sessions" / "childhood-friends")

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = show_active_profile(str(root / "config.yaml"))

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("Character: 刻晴 / 原神", rendered)
            self.assertIn("Relationship: 青梅竹马", rendered)
            self.assertIn("Session: missing", rendered)

    def test_show_active_profile_includes_session_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            store.upsert_inbound_message(
                NormalizedInboundMessage(
                    message_id="<msg-1@example.com>",
                    imap_uid=1,
                    from_email="user@example.com",
                    subject="近况",
                    received_at="2026-04-01T12:00:00+00:00",
                    body_text="今天有点累。",
                    body_html_present=False,
                    attachments=[],
                ),
                raw_path=root / "msg-1.eml",
            )
            store.record_outbound_message(
                message_id="<outbound@example.com>",
                sent_at="2026-04-01T13:00:00+00:00",
                to_email="user@example.com",
                subject="今夜先缓一缓",
                raw_path=root / "outbox.eml",
                image_path=None,
                run_id="run-1",
                batch_id="batch-1",
            )
            store.start_run("run-1", "2026-04-01T13:00:00+00:00")
            store.finish_run("run-1", "success", "2026-04-01T13:01:00+00:00")

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = show_active_profile(str(root / "config.yaml"))

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("Pending inbound: 1", rendered)
            self.assertIn("Last run: success / pending / 2026-04-01T13:00:00+00:00", rendered)
            self.assertIn("Last outbound: 2026-04-01T13:00:00+00:00 / 今夜先缓一缓", rendered)

    def test_show_active_profile_uses_explicit_repo_root_from_external_config(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as config_dir:
            root = Path(repo_dir).resolve()
            create_test_repo(root)
            external_config = Path(config_dir).resolve() / "config.local.yaml"
            payload = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
            payload["project"]["repo_root"] = str(root)
            external_config.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = show_active_profile(str(external_config))

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("Character: 刻晴 / 原神", rendered)
            self.assertIn(f"Session root: {root / 'profiles' / 'characters' / 'keqing' / 'sessions' / 'childhood-friends'}", rendered)

    def test_session_diagnostic_commands_render_scoped_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            store.upsert_inbound_message(
                NormalizedInboundMessage(
                    message_id="<msg-2@example.com>",
                    imap_uid=2,
                    from_email="user@example.com",
                    subject="消息摘要",
                    received_at="2026-04-01T12:05:00+00:00",
                    body_text="今天顺便来报个平安。",
                    body_html_present=False,
                    attachments=[],
                ),
                raw_path=root / "msg-2.eml",
            )
            store.record_outbound_message(
                message_id="<outbound-2@example.com>",
                sent_at="2026-04-01T13:05:00+00:00",
                to_email="user@example.com",
                subject="回信标题",
                raw_path=root / "outbox-2.eml",
                image_path=None,
                run_id="run-2",
                batch_id="batch-2",
            )
            store.start_run("run-2", "2026-04-01T13:05:00+00:00")
            store.finish_run("run-2", "failed", "2026-04-01T13:06:00+00:00", error_stage="image")
            store.record_asset(
                asset_id="asset-2",
                run_id="run-2",
                image_path=root / "scene-2.png",
                scene_card={"visual_style": "二次元插画", "location": "璃月港", "action": "整理文书"},
                prompt_text="scene prompt",
                created_at="2026-04-01T13:05:30+00:00",
            )

            inbox_output = io.StringIO()
            with redirect_stdout(inbox_output):
                show_inbox(str(root / "config.yaml"))
            self.assertIn("Unconsumed inbound: 1", inbox_output.getvalue())
            self.assertIn("subject=消息摘要", inbox_output.getvalue())

            outbox_output = io.StringIO()
            with redirect_stdout(outbox_output):
                show_outbox(str(root / "config.yaml"))
            self.assertIn("message_id=<outbound-2@example.com>", outbox_output.getvalue())
            self.assertIn("sent_at=2026-04-01T13:05:00+00:00", outbox_output.getvalue())
            self.assertNotIn("send_at=", outbox_output.getvalue())
            self.assertIn("subject=回信标题", outbox_output.getvalue())
            self.assertNotIn("Session:", outbox_output.getvalue())

    def test_delete_inbound_and_outbound_commands_remove_scoped_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()

            inbound_raw = settings.documents.session_archive_dir / "inbound-delete.eml"
            inbound_raw.parent.mkdir(parents=True, exist_ok=True)
            inbound_raw.write_text("inbound", encoding="utf-8")
            store.upsert_inbound_message(
                NormalizedInboundMessage(
                    message_id="<delete-inbound@example.com>",
                    imap_uid=77,
                    from_email="user@example.com",
                    subject="待删 inbound",
                    received_at="2026-04-01T12:07:00+00:00",
                    body_text="hello",
                    body_html_present=False,
                    attachments=[],
                ),
                raw_path=inbound_raw,
            )

            outbound_raw = settings.documents.session_archive_dir / "outbound-delete.eml"
            outbound_raw.write_text("outbound", encoding="utf-8")
            store.record_outbound_message(
                message_id="<delete-outbound@example.com>",
                sent_at="2026-04-01T13:07:00+00:00",
                to_email="user@example.com",
                subject="待删 outbound",
                raw_path=outbound_raw,
                image_path=None,
                run_id="run-delete",
                batch_id="batch-delete",
            )

            inbound_output = io.StringIO()
            with redirect_stdout(inbound_output):
                inbound_exit = delete_inbound(
                    str(root / "config.yaml"),
                    character_slug_value="keqing",
                    relationship_slug_value="childhood-friends",
                    imap_uid=77,
                    yes=True,
                )
            self.assertEqual(inbound_exit, 0)
            self.assertIn("Deleted inbound message(s): 1", inbound_output.getvalue())
            self.assertFalse(inbound_raw.exists())
            self.assertEqual(store.list_unconsumed_inbound(), [])

            outbound_output = io.StringIO()
            with redirect_stdout(outbound_output):
                outbound_exit = delete_outbound(
                    str(root / "config.yaml"),
                    character_slug_value="keqing",
                    relationship_slug_value="childhood-friends",
                    message_id="<delete-outbound@example.com>",
                    yes=True,
                )
            self.assertEqual(outbound_exit, 0)
            self.assertIn("Deleted outbound message(s): 1", outbound_output.getvalue())
            self.assertFalse(outbound_raw.exists())
            self.assertEqual(store.list_recent_outbound_messages(), [])

    def test_delete_session_clears_active_profile_and_session_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            store.set_last_consumed_imap_uid(8)
            store.upsert_inbound_message(
                NormalizedInboundMessage(
                    message_id="<delete-session@example.com>",
                    imap_uid=8,
                    from_email="user@example.com",
                    subject="待删 session",
                    received_at="2026-04-01T12:08:00+00:00",
                    body_text="hello",
                    body_html_present=False,
                    attachments=[],
                ),
                raw_path=settings.documents.session_archive_dir / "delete-session.eml",
            )

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = delete_session(
                    str(root / "config.yaml"),
                    character_slug_value="keqing",
                    relationship_slug_value="childhood-friends",
                    yes=True,
                )

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("Deleted session: 刻晴 / 原神 + 青梅竹马", rendered)
            self.assertIn("Active profile cleared.", rendered)
            self.assertFalse((root / "profiles" / "active.json").exists())
            self.assertFalse(
                (root / "profiles" / "characters" / "keqing" / "sessions" / "childhood-friends").exists()
            )

    def test_delete_session_rejects_active_profile_slug_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(tmp_dir)
            outside_character_root = Path(outside_dir) / "evil-character"
            create_test_repo(root)

            outside_session_root = outside_character_root / "sessions" / "escape"
            (outside_character_root / "relationships" / "escape").mkdir(parents=True, exist_ok=True)
            outside_session_root.mkdir(parents=True, exist_ok=True)
            victim_file = outside_session_root / "victim.txt"
            victim_file.write_text("do not delete", encoding="utf-8")
            _write_json(
                outside_character_root / "meta.json",
                {
                    "slug": str(outside_character_root),
                    "character_name": "恶意角色",
                    "work_title": "测试",
                    "created_at": "2026-01-01T00:00:00",
                },
            )
            _write_json(
                outside_character_root / "relationships" / "escape" / "meta.json",
                {
                    "slug": "escape",
                    "character_slug": str(outside_character_root),
                    "name": "越界关系",
                    "description": "测试越界删除。",
                    "created_at": "2026-01-01T00:00:00",
                },
            )
            _write_json(
                outside_session_root / "meta.json",
                {
                    "slug": "escape",
                    "character_slug": str(outside_character_root),
                    "relationship_slug": "escape",
                    "character_name": "恶意角色",
                    "relationship_name": "越界关系",
                    "created_at": "2026-01-01T00:00:00",
                },
            )
            _write_json(
                root / "profiles" / "active.json",
                {
                    "character_slug": str(outside_character_root),
                    "relationship_slug": "escape",
                },
            )

            with self.assertRaisesRegex(ValueError, "Invalid profile slug"):
                delete_session(
                    str(root / "config.yaml"),
                    character_slug_value="",
                    relationship_slug_value="",
                    yes=True,
                )

            self.assertTrue(victim_file.exists())
            self.assertTrue(outside_session_root.exists())

    def test_select_profile_accepts_names_and_bootstraps_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            character_root = root / "profiles" / "characters" / "ganyu"
            relationship_root = character_root / "relationships" / "study-buddies"
            (character_root / "prompt").mkdir(parents=True, exist_ok=True)
            (relationship_root / "prompt").mkdir(parents=True, exist_ok=True)
            for filename in ("persona.md", "world_state.md", "cast_relations.md", "cast_pool.md", "translation_layer.md"):
                (character_root / "prompt" / filename).write_text(f"# {filename}\n", encoding="utf-8")
            (relationship_root / "prompt" / "relationship.md").write_text("# relationship\n", encoding="utf-8")
            _write_json(
                character_root / "meta.json",
                {
                    "slug": "ganyu",
                    "character_name": "甘雨",
                    "work_title": "原神",
                    "created_at": "2026-01-01T00:00:00",
                },
            )
            _write_json(
                relationship_root / "meta.json",
                {
                    "slug": "study-buddies",
                    "character_slug": "ganyu",
                    "name": "同窗",
                    "description": "同窗旧识，通信直接。",
                    "created_at": "2026-01-01T00:00:00",
                },
            )

            exit_code = select_profile(
                str(root / "config.yaml"),
                character_slug_value="甘雨",
                relationship_slug_value="同窗",
            )

            self.assertEqual(exit_code, 0)
            active = json.loads((root / "profiles" / "active.json").read_text(encoding="utf-8"))
            self.assertEqual(active["character_slug"], "ganyu")
            self.assertEqual(active["relationship_slug"], "study-buddies")
            session_root = root / "profiles" / "characters" / "ganyu" / "sessions" / "study-buddies"
            self.assertTrue((session_root / "meta.json").exists())
            self.assertTrue((session_root / "memory" / "active_arc.json").exists())
            self.assertTrue((session_root / "episodes" / "character").exists())

    def test_select_profile_warns_when_previous_session_has_pending_inbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            store.upsert_inbound_message(
                NormalizedInboundMessage(
                    message_id="<pending@example.com>",
                    imap_uid=9,
                    from_email="user@example.com",
                    subject="待处理",
                    received_at="2026-04-01T12:09:00+00:00",
                    body_text="还没消费。",
                    body_html_present=False,
                    attachments=[],
                ),
                raw_path=root / "pending.eml",
            )

            character_root = root / "profiles" / "characters" / "ganyu"
            relationship_root = character_root / "relationships" / "study-buddies"
            (character_root / "prompt").mkdir(parents=True, exist_ok=True)
            (relationship_root / "prompt").mkdir(parents=True, exist_ok=True)
            for filename in ("persona.md", "world_state.md", "cast_relations.md", "cast_pool.md", "translation_layer.md"):
                (character_root / "prompt" / filename).write_text(f"# {filename}\n", encoding="utf-8")
            (relationship_root / "prompt" / "relationship.md").write_text("# relationship\n", encoding="utf-8")
            _write_json(
                character_root / "meta.json",
                {
                    "slug": "ganyu",
                    "character_name": "甘雨",
                    "work_title": "原神",
                    "created_at": "2026-01-01T00:00:00",
                },
            )
            _write_json(
                relationship_root / "meta.json",
                {
                    "slug": "study-buddies",
                    "character_slug": "ganyu",
                    "name": "同窗",
                    "description": "同窗旧识，通信直接。",
                    "created_at": "2026-01-01T00:00:00",
                },
            )

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = select_profile(
                    str(root / "config.yaml"),
                    character_slug_value="甘雨",
                    relationship_slug_value="同窗",
                )

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("Warning: current session 刻晴 / 原神 + 青梅竹马 has 1 unconsumed inbound message(s).", rendered)
            self.assertIn("Selected active profile: 甘雨 / 原神 + 同窗", rendered)

    def test_select_profile_rejects_relationship_from_another_character(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            character_root = root / "profiles" / "characters" / "ganyu"
            relationship_root = character_root / "relationships" / "study-buddies"
            (character_root / "prompt").mkdir(parents=True, exist_ok=True)
            (relationship_root / "prompt").mkdir(parents=True, exist_ok=True)
            for filename in ("persona.md", "world_state.md", "cast_relations.md", "cast_pool.md", "translation_layer.md"):
                (character_root / "prompt" / filename).write_text(f"# {filename}\n", encoding="utf-8")
            (relationship_root / "prompt" / "relationship.md").write_text("# relationship\n", encoding="utf-8")
            _write_json(
                character_root / "meta.json",
                {
                    "slug": "ganyu",
                    "character_name": "甘雨",
                    "work_title": "原神",
                    "created_at": "2026-01-01T00:00:00",
                },
            )
            _write_json(
                relationship_root / "meta.json",
                {
                    "slug": "study-buddies",
                    "character_slug": "ganyu",
                    "name": "同窗",
                    "description": "同窗旧识，通信直接。",
                    "created_at": "2026-01-01T00:00:00",
                },
            )

            with self.assertRaisesRegex(ValueError, "Relationship profile not found for character '刻晴 / 原神'"):
                select_profile(
                    str(root / "config.yaml"),
                    character_slug_value="keqing",
                    relationship_slug_value="study-buddies",
                )


if __name__ == "__main__":
    unittest.main()
