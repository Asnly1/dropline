from __future__ import annotations

import base64
import json
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml

from dropline import llm
from dropline.config import load_settings
from dropline.llm import (
    AnthropicBackedClient,
    DeepSeekBackedClient,
    DoubaoBackedClient,
    GLMBackedClient,
    GeminiBackedClient,
    GrokBackedClient,
    OpenAIBackedClient,
    QwenBackedClient,
    build_ai_client,
    render_scene_card_prompt,
)
from dropline.llm_contracts import (
    CHARACTER_PROFILE_RESPONSE_SCHEMA,
    RELATIONSHIP_PROFILE_RESPONSE_SCHEMA,
    SCENE_CARD_RESPONSE_SCHEMA,
    STAGE1_RESPONSE_SCHEMA,
    build_stage2_response_schema,
)
from dropline.models import CandidateMemory, ModelValidationError, RelationshipProfileDraft, SceneCard, Stage1Result, Stage2Result

from tests.helpers import create_test_repo


class _FakeDownloadResponse:
    def __init__(self, *, headers: dict[str, str], body: bytes) -> None:
        self.headers = headers
        self._body = body
        self._offset = 0
        self.read_sizes: list[int] = []

    def __enter__(self) -> _FakeDownloadResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size < 0:
            size = len(self._body) - self._offset
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class _ReadShouldNotBeCalledResponse(_FakeDownloadResponse):
    def read(self, size: int = -1) -> bytes:
        raise AssertionError("body should not be read")


def _stage1_payload_json() -> str:
    return json.dumps(
        {
            "user_digest": {
                "main_event": "今天大部分时间在处理课业",
                "state": "疲惫，有被课业压住的感觉",
                "possible_needs": ["被理解", "不要太重的说教"],
            },
            "candidate_memories": [
                {
                    "content": "用户近期在准备下个月的重要考试",
                    "type": "active_arc",
                    "confidence": 0.86,
                    "durability": "short",
                    "evidence": "我下个月要参加很重要的考试，这段时间都在复习",
                    "why_it_matters": "",
                    "source_date": "",
                }
            ],
            "emotional_priority": "comfort",
            "open_loops": ["用户在准备考试", "刻晴在处理海灯节筹备"],
        },
        ensure_ascii=False,
    )


def _stage2_payload_json(*, include_scene_card: bool = True) -> str:
    payload = {
        "subject": "今晚先到这里",
        "body": "我会继续陪你把这段时间走过去。",
        "character_episode": {
            "summary": "刻晴在夜里收起文书前，想起了用户这段时间的压力。",
            "continuity_value": "她把这份挂念记得更具体了一点。",
        },
        "active_arc_update_suggestion": {
            "action": "keep",
            "content": "继续保留考试备考这条近期主线。",
        },
    }
    if include_scene_card:
        payload["scene_card"] = {
            "visual_style": "二次元插画",
            "location": "璃月港临水长阶",
            "action": "夜里短暂停步，整理尚未收起的文书",
            "key_object": "压着纸页的玉簪与折起的公文",
            "time": "入夜后",
            "mood": "克制而安静的挂念",
            "camera_distance": "medium",
        }
    return json.dumps(payload, ensure_ascii=False)


def _english_stage1_payload_json() -> str:
    return json.dumps(
        {
            "user_digest": {
                "main_event": "Spent most of the day working through coursework.",
                "state": "Tired and feeling buried by assignments.",
                "possible_needs": ["To feel understood", "A gentle response instead of a lecture"],
            },
            "candidate_memories": [
                {
                    "content": "The user is preparing for an important exam next month.",
                    "type": "active_arc",
                    "confidence": 0.86,
                    "durability": "short",
                    "evidence": "They said they have been studying for a major exam next month.",
                    "why_it_matters": "",
                    "source_date": "",
                }
            ],
            "emotional_priority": "comfort",
            "open_loops": ["The user is preparing for an exam", "Keqing is handling festival preparation"],
        },
        ensure_ascii=False,
    )


def _english_stage2_payload_json(*, include_scene_card: bool = True) -> str:
    payload = {
        "subject": "Let's stop here for tonight",
        "body": "I will stay with you as you get through this stretch.",
        "character_episode": {
            "summary": "Before putting away her paperwork that night, Keqing thought about the user's pressure.",
            "continuity_value": "She remembered that concern a little more clearly.",
        },
        "active_arc_update_suggestion": {
            "action": "keep",
            "content": "Keep the ongoing exam-preparation thread for now.",
        },
    }
    if include_scene_card:
        payload["scene_card"] = {
            "visual_style": "anime illustration",
            "location": "the harbor steps by the water in Liyue",
            "action": "pausing at night to gather unfinished documents",
            "key_object": "a jade hairpin pinning down folded paperwork",
            "time": "late evening",
            "mood": "restrained and quiet concern",
            "camera_distance": "medium shot",
        }
    return json.dumps(payload, ensure_ascii=False)


def _character_profile_payload_json() -> str:
    return json.dumps(
        {
            "persona": {
                "identity_and_role": ["刻晴，璃月七星之一。"],
                "core_values": ["重视行动与责任。"],
                "personality_traits": ["反应快，自尊强。"],
                "language_style": ["句子整洁，不油滑。"],
                "repeatable_life_details": ["会亲自去现场。"],
                "ooc_boundaries": ["不能写成恋爱手游式模板。"],
            },
            "world_state": {
                "worldview_type": ["原作世界延展型。"],
                "time_anchor": ["当前版本璃月稳定期。"],
                "overall_atmosphere": ["繁华而忙碌。"],
                "character_work_domain": ["围绕土地、建设、规划与民生。"],
                "sustainable_storylines": ["节庆协调。"],
                "appearance_boundaries": ["不改写重大主线。"],
                "letter_scale": ["每封信聚焦一件主事。"],
                "image_principles": ["附图优先事务中的瞬间。"],
            },
            "cast_relations": {
                "focus_relationships": [
                    {
                        "name": "凝光",
                        "relationship_tone": ["会争、会合作、也逐渐互相承认。"],
                        "writing_points": ["对话应锋利、短促、聪明。"],
                        "forbidden_patterns": ["不写成互相撒娇的姐妹情。"],
                    }
                ]
            },
            "cast_pool": {
                "usage_principles": ["不是角色越多越好。"],
                "tier_1_high_frequency": ["凝光：预算、优先级、资源配置。"],
                "tier_2_task_support": ["烟绯：法规解释、合规。"],
                "tier_3_daily_life": ["香菱：吃食、热闹、烟火气。"],
                "tier_4_heavyweight_low_frequency": ["魈：旧职责、守护、人与仙的边界。"],
                "tier_5_background_flavor": ["商会人士、总务司人员、千岩军。"],
                "frequency_rules": ["第一层高频，但每封最多 1 人显性出场。"],
            },
            "translation_layer": {
                "purpose": ["保留事实与情绪。"],
                "general_principles": ["忠于原意。"],
                "processing_modes": [
                    {
                        "name": "软映射（默认）",
                        "guidance": ["适用于高频、普通、易理解的现代生活词。"],
                        "examples": ["作业 -> 功课"],
                    }
                ],
                "semantic_mappings": [
                    {
                        "category": "学业类",
                        "mappings": ["作业 -> 功课"],
                    }
                ],
                "emotion_fidelity_rules": ["用户情绪轻，就轻回。"],
                "replying_patterns": [
                    {
                        "scenario": "她会怎样回复学业压力",
                        "preferred": ["帮你拆顺序。"],
                        "avoid": ["空泛鸡汤。"],
                    }
                ],
                "forbidden_patterns": ["禁止把所有句子改成古风套话。"],
            },
        },
        ensure_ascii=False,
    )


def _relationship_profile_payload_json() -> str:
    return json.dumps(
        {
            "setup_statement": ["这是系统专用补设。"],
            "relationship_definition": ["关系深于普通朋友，但仍克制。"],
            "shared_past": ["两人有共同旧事。"],
            "relationship_tone": ["熟、信、久、克制。"],
            "attitude_toward_user": ["会认真读用户来信。"],
            "how_it_affects_letters": ["更少客套，更快进入正题。"],
            "intimacy_threshold": ["默认熟悉，但不高频恋爱表达。"],
            "explicit_boundaries": ["不默认婚约同居。"],
            "warming_up_path": ["先从更坦诚地说近况开始。"],
        },
        ensure_ascii=False,
    )


SCENE_CARD = SceneCard(
    visual_style="二次元插画",
    location="璃月港临水长阶",
    action="夜里短暂停步，整理尚未收起的文书",
    key_object="压着纸页的玉簪与折起的公文",
    time="入夜后",
    mood="克制而安静的挂念",
    camera_distance="medium",
)

SCENE_CARD_ENGLISH = SceneCard(
    visual_style="anime illustration",
    location="the harbor steps by the water in Liyue",
    action="pausing at night to gather unfinished documents",
    key_object="a jade hairpin pinning down folded paperwork",
    time="late evening",
    mood="restrained and quiet concern",
    camera_distance="medium shot",
)


class LLMTests(unittest.TestCase):
    def test_download_image_bytes_accepts_image_response_under_limit(self) -> None:
        response = _FakeDownloadResponse(
            headers={"Content-Type": "image/png; charset=binary", "Content-Length": "8"},
            body=b"fake-png",
        )

        with patch("dropline.llm.urlopen", return_value=response), patch(
            "dropline.llm.MAX_IMAGE_BYTES",
            16,
            create=True,
        ), patch("dropline.llm.IMAGE_DOWNLOAD_CHUNK_BYTES", 4, create=True):
            image_bytes = llm._download_image_bytes("https://example.com/image.png", provider_label="Test")

        self.assertEqual(image_bytes, b"fake-png")
        self.assertEqual(response.read_sizes, [4, 4, 4])

    def test_download_image_bytes_rejects_non_image_content_type(self) -> None:
        response = _FakeDownloadResponse(headers={"Content-Type": "text/html"}, body=b"<html></html>")

        with patch("dropline.llm.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "non-image Content-Type"):
                llm._download_image_bytes("https://example.com/image.png", provider_label="Test")

    def test_download_image_bytes_rejects_missing_content_type(self) -> None:
        response = _FakeDownloadResponse(headers={}, body=b"fake-png")

        with patch("dropline.llm.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "missing Content-Type"):
                llm._download_image_bytes("https://example.com/image.png", provider_label="Test")

    def test_download_image_bytes_rejects_content_length_over_limit_before_reading(self) -> None:
        response = _ReadShouldNotBeCalledResponse(
            headers={"Content-Type": "image/png", "Content-Length": "17"},
            body=b"",
        )

        with patch("dropline.llm.urlopen", return_value=response), patch(
            "dropline.llm.MAX_IMAGE_BYTES",
            16,
            create=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "exceeds maximum size"):
                llm._download_image_bytes("https://example.com/image.png", provider_label="Test")

    def test_download_image_bytes_rejects_stream_that_exceeds_limit_without_content_length(self) -> None:
        response = _FakeDownloadResponse(headers={"Content-Type": "image/png"}, body=b"123456789")

        with patch("dropline.llm.urlopen", return_value=response), patch(
            "dropline.llm.MAX_IMAGE_BYTES",
            8,
            create=True,
        ), patch("dropline.llm.IMAGE_DOWNLOAD_CHUNK_BYTES", 4, create=True):
            with self.assertRaisesRegex(RuntimeError, "exceeds maximum size"):
                llm._download_image_bytes("https://example.com/image.png", provider_label="Test")

    def test_stage_schema_matches_core_model_fields(self) -> None:
        candidate_schema = STAGE1_RESPONSE_SCHEMA["properties"]["candidate_memories"]["items"]
        self.assertEqual(set(candidate_schema["required"]), {field.name for field in fields(CandidateMemory)})
        self.assertEqual(set(SCENE_CARD_RESPONSE_SCHEMA["required"]), {field.name for field in fields(SceneCard)})
        self.assertEqual(
            build_stage2_response_schema(require_scene_card=True)["properties"]["active_arc_update_suggestion"][
                "properties"
            ]["action"]["enum"],
            ["none", "keep", "update", "archive"],
        )

    def test_stage1_rejects_invalid_candidate_memory_values(self) -> None:
        cases = (
            ("type", "unknown"),
            ("durability", "forever"),
            ("confidence", 1.5),
            ("confidence", -0.1),
        )
        for field_name, value in cases:
            with self.subTest(field_name=field_name, value=value):
                payload = json.loads(_stage1_payload_json())
                payload["candidate_memories"][0][field_name] = value

                with self.assertRaises(ModelValidationError):
                    Stage1Result.from_dict(payload)

    def test_stage_parsers_reject_malformed_nested_objects(self) -> None:
        malformed_stage1 = json.loads(_stage1_payload_json())
        malformed_stage1["user_digest"] = "bad"
        with self.assertRaises(ModelValidationError):
            Stage1Result.from_dict(malformed_stage1)

        malformed_memory = json.loads(_stage1_payload_json())
        malformed_memory["candidate_memories"][0] = "bad"
        with self.assertRaises(ModelValidationError):
            Stage1Result.from_dict(malformed_memory)

        malformed_stage2 = json.loads(_stage2_payload_json())
        malformed_stage2["character_episode"] = "bad"
        with self.assertRaises(ModelValidationError):
            Stage2Result.from_dict(malformed_stage2)

        malformed_suggestion = json.loads(_stage2_payload_json())
        malformed_suggestion["active_arc_update_suggestion"] = "bad"
        with self.assertRaises(ModelValidationError):
            Stage2Result.from_dict(malformed_suggestion)

    def test_stage2_rejects_replace_active_arc_action(self) -> None:
        payload = json.loads(_stage2_payload_json())
        payload["active_arc_update_suggestion"]["action"] = "replace"

        with self.assertRaises(ModelValidationError):
            Stage2Result.from_dict(payload)

    def test_stage2_body_normalizes_literal_newline_sequences(self) -> None:
        payload = json.loads(_stage2_payload_json())
        payload["body"] = "第一段\\r\\n第二行\\n\\n第三段\\r最后"

        result = Stage2Result.from_dict(payload)

        self.assertEqual(result.body, "第一段\n第二行\n\n第三段\n最后")

    def test_stage2_schema_respects_image_toggle(self) -> None:
        with_scene = build_stage2_response_schema(require_scene_card=True)
        without_scene = build_stage2_response_schema(require_scene_card=False)
        self.assertIn("scene_card", with_scene["required"])
        self.assertNotIn("scene_card", without_scene["required"])
        self.assertIn("scene_card", without_scene["properties"])

    def test_profile_schema_matches_relationship_draft_fields(self) -> None:
        self.assertEqual(
            set(RELATIONSHIP_PROFILE_RESPONSE_SCHEMA["required"]),
            {field.name for field in fields(RelationshipProfileDraft)},
        )
        self.assertEqual(
            set(CHARACTER_PROFILE_RESPONSE_SCHEMA["required"]),
            {"persona", "world_state", "cast_relations", "cast_pool", "translation_layer"},
        )

    def test_chinese_field_violation_uses_symmetric_character_ratio(self) -> None:
        self.assertTrue(
            llm._chinese_field_violates_requirement(
                "I will continue supporting you through this 加油"
            )
        )
        self.assertFalse(llm._chinese_field_violates_requirement("我今天用 Photoshop 修了图"))
        self.assertFalse(llm._chinese_field_violates_requirement("见字如晤，今夜风很静，一切都好。"))
        self.assertFalse(llm._chinese_field_violates_requirement(""))

    def _load_settings(
        self,
        root: Path,
        *,
        stage1_provider: str = "openai",
        stage1_model: str = "gpt-5.4",
        stage2_provider: str = "openai",
        stage2_model: str = "gpt-5.4",
        image_provider: str = "openai",
        image_model: str = "gpt-image-1.5",
        image_enabled: bool = True,
        language: str = "简体中文",
    ):
        config_path = root / "config.yaml"
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        payload["llm"]["language"] = language
        payload["llm"]["stage1"]["provider"] = stage1_provider
        payload["llm"]["stage2"]["provider"] = stage2_provider
        payload["llm"]["image"]["provider"] = image_provider
        payload["llm"]["image"]["enabled"] = image_enabled
        payload[stage1_provider]["text_model"] = stage1_model
        payload[stage2_provider]["text_model"] = stage2_model
        if "image_model" in payload[image_provider]:
            payload[image_provider]["image_model"] = image_model
        config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return load_settings(config_path)

    def _assert_language_requirement(self, instructions: str, language: str) -> None:
        self.assertIn(language, instructions)
        if language == "English":
            self.assertIn("Output language is controlled only by llm.language", instructions)
            self.assertIn("do not keep source-language fragments", instructions)
            self.assertIn(
                "Do not translate JSON Schema field names, enum values, or protocol constants",
                instructions,
            )
            return
        self.assertIn("输出语言只由 llm.language 决定", instructions)
        self.assertIn("不要保留源语言片段", instructions)
        self.assertIn("不要翻译 JSON Schema 规定必须保留原样的字段名、枚举值、协议常量。", instructions)

    def _assert_openai_compatible_text_client(
        self,
        *,
        client_cls,
        provider_name: str,
        model_name: str,
        expected_base_url: str,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(
                root,
                stage1_provider=provider_name,
                stage1_model=model_name,
                stage2_provider=provider_name,
                stage2_model=model_name,
            )
            fake_client = MagicMock()
            fake_client.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=_stage1_payload_json()))]
            )

            with patch("openai.OpenAI", return_value=fake_client) as patched_openai:
                client = client_cls(settings)
                result = client.generate_stage1({"inbound_messages": []})

            self.assertEqual(result.emotional_priority, "comfort")
            self.assertEqual(patched_openai.call_args.kwargs["api_key"], settings.providers[provider_name].api_key)
            self.assertEqual(patched_openai.call_args.kwargs["base_url"], expected_base_url)
            kwargs = fake_client.chat.completions.create.call_args.kwargs
            self.assertEqual(kwargs["model"], model_name)
            self.assertEqual(kwargs["messages"][0]["role"], "system")
            self.assertIn("JSON Schema", kwargs["messages"][0]["content"])
            self._assert_language_requirement(kwargs["messages"][0]["content"], settings.llm.language)

    def test_openai_stage1_uses_responses_json_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(root)
            fake_client = MagicMock()
            fake_client.responses.create.return_value = SimpleNamespace(output_text=_stage1_payload_json(), output=[])

            with patch("openai.OpenAI", return_value=fake_client) as patched_openai:
                client = OpenAIBackedClient(settings)
                result = client.generate_stage1({"inbound_messages": []})

            self.assertEqual(result.emotional_priority, "comfort")
            self.assertEqual(patched_openai.call_args.kwargs["api_key"], settings.providers["openai"].api_key)
            kwargs = fake_client.responses.create.call_args.kwargs
            self.assertEqual(kwargs["model"], "gpt-5.4")
            self.assertEqual(kwargs["text"]["format"]["type"], "json_schema")
            self.assertEqual(kwargs["text"]["format"]["name"], "stage1_result")
            self._assert_language_requirement(kwargs["instructions"], settings.llm.language)

    def test_openai_stage1_uses_english_instructions_when_target_language_is_english(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(root, language="English")
            fake_client = MagicMock()
            fake_client.responses.create.return_value = SimpleNamespace(output_text=_english_stage1_payload_json(), output=[])

            with patch("openai.OpenAI", return_value=fake_client):
                client = OpenAIBackedClient(settings)
                client.generate_stage1({"inbound_messages": []})

            instructions = fake_client.responses.create.call_args.kwargs["instructions"]
            self.assertIn("You are the Stage 1 analyzer", instructions)
            self._assert_language_requirement(instructions, settings.llm.language)
            self.assertNotIn("输出语言规则", instructions)

    def test_openai_stage2_uses_stage2_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(root)
            fake_client = MagicMock()
            fake_client.responses.create.return_value = SimpleNamespace(output_text=_stage2_payload_json(), output=[])

            with patch("openai.OpenAI", return_value=fake_client):
                client = OpenAIBackedClient(settings)
                result = client.generate_stage2({"recent_messages": []})

            self.assertEqual(result.subject, "今晚先到这里")
            kwargs = fake_client.responses.create.call_args.kwargs
            self.assertEqual(kwargs["model"], "gpt-5.4")
            self.assertEqual(kwargs["text"]["format"]["name"], "stage2_result")
            self.assertIn("scene_card", kwargs["text"]["format"]["schema"]["required"])
            self.assertIn("recent_scene_cards", kwargs["instructions"])
            self.assertIn("不要连续依赖夜晚案前写信", kwargs["instructions"])

    def test_openai_stage2_uses_english_instructions_when_target_language_is_english(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(root, language="English")
            fake_client = MagicMock()
            fake_client.responses.create.return_value = SimpleNamespace(output_text=_english_stage2_payload_json(), output=[])

            with patch("openai.OpenAI", return_value=fake_client):
                client = OpenAIBackedClient(settings)
                client.generate_stage2({"recent_messages": []})

            instructions = fake_client.responses.create.call_args.kwargs["instructions"]
            self.assertIn("You are the Stage 2 writer", instructions)
            self.assertIn("recent_scene_cards", instructions)
            self.assertIn("Night scenes are allowed", instructions)
            self._assert_language_requirement(instructions, settings.llm.language)
            self.assertNotIn("不要连续依赖夜晚案前写信", instructions)

    def test_openai_stage2_repairs_non_english_output_when_target_language_is_english(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(root, language="English")
            fake_client = MagicMock()
            fake_client.responses.create.side_effect = [
                SimpleNamespace(output_text=_stage2_payload_json(), output=[]),
                SimpleNamespace(output_text=_english_stage2_payload_json(), output=[]),
            ]

            with patch("openai.OpenAI", return_value=fake_client):
                client = OpenAIBackedClient(settings)
                result = client.generate_stage2({"recent_messages": []})

            self.assertEqual(result.subject, "Let's stop here for tonight")
            self.assertEqual(result.active_arc_update_suggestion.action, "keep")
            self.assertEqual(result.active_arc_update_suggestion.content, "Keep the ongoing exam-preparation thread for now.")
            self.assertIsNotNone(result.scene_card)
            assert result.scene_card is not None
            self.assertEqual(result.scene_card.visual_style, "anime illustration")
            self.assertEqual(result.scene_card.location, "the harbor steps by the water in Liyue")
            self.assertEqual(result.scene_card.action, "pausing at night to gather unfinished documents")
            self.assertEqual(result.scene_card.key_object, "a jade hairpin pinning down folded paperwork")
            self.assertEqual(result.scene_card.time, "late evening")
            self.assertEqual(result.scene_card.mood, "restrained and quiet concern")
            self.assertEqual(result.scene_card.camera_distance, "medium shot")
            self.assertEqual(fake_client.responses.create.call_count, 2)
            self.assertIn(
                "structured JSON language repairer",
                fake_client.responses.create.call_args_list[1].kwargs["instructions"],
            )

    def test_openai_stage2_raises_when_english_repair_still_contains_non_english_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(root, language="English")
            fake_client = MagicMock()
            fake_client.responses.create.side_effect = [
                SimpleNamespace(output_text=_stage2_payload_json(), output=[]),
                SimpleNamespace(output_text=_stage2_payload_json(), output=[]),
            ]

            with patch("openai.OpenAI", return_value=fake_client):
                client = OpenAIBackedClient(settings)
                with self.assertRaisesRegex(RuntimeError, "could not be repaired to target language 'English'"):
                    client.generate_stage2({"recent_messages": []})

    def test_openai_stage2_repairs_english_output_when_target_language_is_chinese(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(root, language="简体中文")
            fake_client = MagicMock()
            fake_client.responses.create.side_effect = [
                SimpleNamespace(output_text=_english_stage2_payload_json(), output=[]),
                SimpleNamespace(output_text=_stage2_payload_json(), output=[]),
            ]

            with patch("openai.OpenAI", return_value=fake_client):
                client = OpenAIBackedClient(settings)
                result = client.generate_stage2({"recent_messages": []})

            self.assertEqual(result.subject, "今晚先到这里")
            self.assertEqual(fake_client.responses.create.call_count, 2)
            self.assertIn(
                "结构化 JSON 语言修复器",
                fake_client.responses.create.call_args_list[1].kwargs["instructions"],
            )

    def test_openai_stage2_raises_when_chinese_repair_still_contains_english_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(root, language="简体中文")
            fake_client = MagicMock()
            fake_client.responses.create.side_effect = [
                SimpleNamespace(output_text=_english_stage2_payload_json(), output=[]),
                SimpleNamespace(output_text=_english_stage2_payload_json(), output=[]),
            ]

            with patch("openai.OpenAI", return_value=fake_client):
                client = OpenAIBackedClient(settings)
                with self.assertRaisesRegex(RuntimeError, "could not be repaired to target language '简体中文'"):
                    client.generate_stage2({"recent_messages": []})

    def test_openai_stage2_omits_scene_card_requirement_when_image_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(root, image_enabled=False)
            fake_client = MagicMock()
            fake_client.responses.create.return_value = SimpleNamespace(
                output_text=_stage2_payload_json(include_scene_card=False),
                output=[],
            )

            with patch("openai.OpenAI", return_value=fake_client):
                client = OpenAIBackedClient(settings)
                result = client.generate_stage2({"recent_messages": []})

            self.assertEqual(result.subject, "今晚先到这里")
            self.assertIsNone(result.scene_card)
            kwargs = fake_client.responses.create.call_args.kwargs
            self.assertEqual(kwargs["model"], "gpt-5.4")
            self.assertNotIn("scene_card", kwargs["text"]["format"]["schema"]["required"])

    def test_openai_character_profile_uses_character_profile_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(root)
            fake_client = MagicMock()
            fake_client.responses.create.return_value = SimpleNamespace(
                output_text=_character_profile_payload_json(),
                output=[],
            )

            with patch("openai.OpenAI", return_value=fake_client):
                client = OpenAIBackedClient(settings)
                draft = client.generate_character_profile(work_title="原神", character_name="刻晴")

            self.assertIn("刻晴", draft.persona.identity_and_role[0])
            kwargs = fake_client.responses.create.call_args.kwargs
            self.assertEqual(kwargs["model"], "gpt-5.4")
            self.assertEqual(kwargs["text"]["format"]["name"], "character_profile")
            self._assert_language_requirement(kwargs["instructions"], settings.llm.language)

    def test_openai_character_profile_without_work_title_uses_null_work_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(root)
            fake_client = MagicMock()
            fake_client.responses.create.return_value = SimpleNamespace(
                output_text=_character_profile_payload_json(),
                output=[],
            )

            with patch("openai.OpenAI", return_value=fake_client):
                client = OpenAIBackedClient(settings)
                draft = client.generate_character_profile(work_title=None, character_name="肖战")

            self.assertIn("刻晴", draft.persona.identity_and_role[0])
            kwargs = fake_client.responses.create.call_args.kwargs
            payload = json.loads(kwargs["input"])
            self.assertIsNone(payload["work_title"])
            self.assertEqual(payload["character_name"], "肖战")

    def test_openai_relationship_profile_uses_relationship_profile_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(root)
            fake_client = MagicMock()
            fake_client.responses.create.return_value = SimpleNamespace(
                output_text=_relationship_profile_payload_json(),
                output=[],
            )

            with patch("openai.OpenAI", return_value=fake_client):
                client = OpenAIBackedClient(settings)
                draft = client.generate_relationship_profile(
                    character_name="刻晴",
                    character_prompt_docs={"persona": "# persona"},
                    relationship_name="青梅竹马",
                    relationship_description="久别重逢。",
                )

            self.assertIn("熟、信、久、克制。", draft.relationship_tone)
            kwargs = fake_client.responses.create.call_args.kwargs
            self.assertEqual(kwargs["model"], "gpt-5.4")
            self.assertEqual(kwargs["text"]["format"]["name"], "relationship_profile")

    def test_openai_image_uses_dedicated_images_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(root)
            fake_client = MagicMock()
            fake_client.images.generate.return_value = SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(b"fake-png").decode("ascii"))]
            )

            with patch("openai.OpenAI", return_value=fake_client):
                client = OpenAIBackedClient(settings)
                image = client.generate_image(SCENE_CARD)

            self.assertEqual(image.image_bytes, b"fake-png")
            kwargs = fake_client.images.generate.call_args.kwargs
            self.assertEqual(kwargs["model"], "gpt-image-1.5")
            self.assertEqual(kwargs["size"], "1536x1024")
            self.assertEqual(kwargs["output_format"], "png")
            self.assertIn("画面风格：二次元插画", kwargs["prompt"])

    def test_openai_image_prompt_uses_target_language_for_english_scene_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(root, language="English")
            fake_client = MagicMock()
            fake_client.images.generate.return_value = SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(b"fake-png").decode("ascii"))]
            )

            with patch("openai.OpenAI", return_value=fake_client):
                client = OpenAIBackedClient(settings)
                client.generate_image(SCENE_CARD_ENGLISH)

            prompt = fake_client.images.generate.call_args.kwargs["prompt"]
            self.assertIn("Visual style: anime illustration.", prompt)
            self.assertIn("Camera distance: medium shot.", prompt)
            self.assertNotIn("画面风格", prompt)

    def test_gemini_stage1_uses_json_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(
                root,
                stage1_provider="gemini",
                stage1_model="gemini-3.1-pro-preview",
                stage2_provider="gemini",
                stage2_model="gemini-3.1-pro-preview",
                image_provider="gemini",
                image_model="gemini-3-pro-image-preview",
            )
            fake_client = MagicMock()
            fake_client.models.generate_content.return_value = SimpleNamespace(text=_stage1_payload_json())
            fake_genai = SimpleNamespace(Client=MagicMock(return_value=fake_client))

            with patch("dropline.llm._import_google_genai", return_value=fake_genai):
                client = GeminiBackedClient(settings)
                result = client.generate_stage1({"inbound_messages": []})

            self.assertEqual(result.emotional_priority, "comfort")
            self.assertEqual(fake_genai.Client.call_args.kwargs["api_key"], settings.providers["gemini"].api_key)
            kwargs = fake_client.models.generate_content.call_args.kwargs
            self.assertEqual(kwargs["model"], "gemini-3.1-pro-preview")
            self.assertEqual(kwargs["config"]["response_mime_type"], "application/json")
            self.assertEqual(kwargs["config"]["response_json_schema"]["type"], "object")
            self._assert_language_requirement(kwargs["config"]["system_instruction"], settings.llm.language)

    def test_gemini_image_uses_image_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(
                root,
                stage1_provider="gemini",
                stage1_model="gemini-3.1-pro-preview",
                stage2_provider="gemini",
                stage2_model="gemini-3.1-pro-preview",
                image_provider="gemini",
                image_model="gemini-3-pro-image-preview",
            )
            fake_client = MagicMock()
            fake_client.models.generate_content.return_value = SimpleNamespace(
                parts=[SimpleNamespace(text=None, inline_data=SimpleNamespace(data=b"fake-gemini-png"))]
            )
            fake_genai = SimpleNamespace(Client=MagicMock(return_value=fake_client))

            with patch("dropline.llm._import_google_genai", return_value=fake_genai):
                client = GeminiBackedClient(settings)
                image = client.generate_image(SCENE_CARD)

            self.assertEqual(image.image_bytes, b"fake-gemini-png")
            kwargs = fake_client.models.generate_content.call_args.kwargs
            self.assertEqual(kwargs["model"], "gemini-3-pro-image-preview")
            self.assertEqual(kwargs["config"]["response_modalities"], ["IMAGE"])

    def test_gemini_image_uses_english_system_instruction_when_target_language_is_english(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(
                root,
                stage1_provider="gemini",
                stage1_model="gemini-3.1-pro-preview",
                stage2_provider="gemini",
                stage2_model="gemini-3.1-pro-preview",
                image_provider="gemini",
                image_model="gemini-3-pro-image-preview",
                language="English",
            )
            fake_client = MagicMock()
            fake_client.models.generate_content.return_value = SimpleNamespace(
                parts=[SimpleNamespace(text=None, inline_data=SimpleNamespace(data=b"fake-gemini-png"))]
            )
            fake_genai = SimpleNamespace(Client=MagicMock(return_value=fake_client))

            with patch("dropline.llm._import_google_genai", return_value=fake_genai):
                client = GeminiBackedClient(settings)
                client.generate_image(SCENE_CARD_ENGLISH)

            kwargs = fake_client.models.generate_content.call_args.kwargs
            self.assertEqual(
                kwargs["config"]["system_instruction"],
                "Generate exactly one image that matches the prompt. Do not output any extra explanation.",
            )

    def test_gemini_stage1_retries_transient_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(
                root,
                stage1_provider="gemini",
                stage1_model="gemini-3.1-pro-preview",
                stage2_provider="gemini",
                stage2_model="gemini-3.1-pro-preview",
                image_provider="gemini",
                image_model="gemini-3-pro-image-preview",
            )
            fake_client = MagicMock()
            fake_client.models.generate_content.side_effect = [
                ConnectionError("server disconnected without sending a response"),
                SimpleNamespace(text=_stage1_payload_json()),
            ]
            fake_genai = SimpleNamespace(Client=MagicMock(return_value=fake_client))

            with patch("dropline.llm._import_google_genai", return_value=fake_genai), patch(
                "dropline.utils.time.sleep",
                return_value=None,
            ):
                client = GeminiBackedClient(settings)
                result = client.generate_stage1({"inbound_messages": []})

            self.assertEqual(result.emotional_priority, "comfort")
            self.assertEqual(fake_client.models.generate_content.call_count, 2)

    def test_anthropic_stage1_uses_structured_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(
                root,
                stage1_provider="anthropic",
                stage1_model="claude-opus-4-7",
                stage2_provider="anthropic",
                stage2_model="claude-opus-4-7",
            )
            fake_client = MagicMock()
            fake_client.messages.create.return_value = SimpleNamespace(
                content=[SimpleNamespace(type="text", text=_stage1_payload_json())]
            )

            anthropic_cls = MagicMock(return_value=fake_client)
            with patch("dropline.llm._import_anthropic", return_value=anthropic_cls):
                client = AnthropicBackedClient(settings)
                result = client.generate_stage1({"inbound_messages": []})

            self.assertEqual(result.emotional_priority, "comfort")
            self.assertEqual(anthropic_cls.call_args.kwargs["api_key"], settings.providers["anthropic"].api_key)
            kwargs = fake_client.messages.create.call_args.kwargs
            self.assertEqual(kwargs["model"], "claude-opus-4-7")
            self.assertEqual(kwargs["output_config"]["format"]["type"], "json_schema")
            self.assertEqual(kwargs["output_config"]["format"]["schema"]["type"], "object")
            self._assert_language_requirement(kwargs["system"], settings.llm.language)

    def test_grok_image_uses_xai_images_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(
                root,
                stage1_provider="grok",
                stage1_model="grok-4.20-0309-reasoning",
                stage2_provider="grok",
                stage2_model="grok-4.20-0309-reasoning",
                image_provider="grok",
                image_model="grok-imagine-image-pro",
            )
            fake_client = MagicMock()
            fake_client.images.generate.return_value = SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(b"fake-grok-png").decode("ascii"))]
            )

            with patch("openai.OpenAI", return_value=fake_client) as patched_openai:
                client = GrokBackedClient(settings)
                image = client.generate_image(SCENE_CARD)

            self.assertEqual(image.image_bytes, b"fake-grok-png")
            self.assertEqual(patched_openai.call_args.kwargs["api_key"], settings.providers["grok"].api_key)
            kwargs = fake_client.images.generate.call_args.kwargs
            self.assertEqual(kwargs["model"], "grok-imagine-image-pro")
            self.assertEqual(kwargs["response_format"], "b64_json")

    def test_grok_stage1_uses_responses_json_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(
                root,
                stage1_provider="grok",
                stage1_model="grok-4.20-0309-reasoning",
                stage2_provider="grok",
                stage2_model="grok-4.20-0309-reasoning",
                image_provider="grok",
                image_model="grok-imagine-image-pro",
            )
            fake_client = MagicMock()
            fake_client.responses.create.return_value = SimpleNamespace(output_text=_stage1_payload_json(), output=[])

            with patch("openai.OpenAI", return_value=fake_client):
                client = GrokBackedClient(settings)
                result = client.generate_stage1({"inbound_messages": []})

            self.assertEqual(result.emotional_priority, "comfort")
            kwargs = fake_client.responses.create.call_args.kwargs
            self.assertEqual(kwargs["model"], "grok-4.20-0309-reasoning")
            self.assertEqual(kwargs["text"]["format"]["type"], "json_schema")
            self.assertEqual(kwargs["text"]["format"]["name"], "stage1_result")

    def test_qwen_stage1_uses_openai_compatible_api(self) -> None:
        self._assert_openai_compatible_text_client(
            client_cls=QwenBackedClient,
            provider_name="qwen",
            model_name="qwen3-max-2026-01-23",
            expected_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

    def test_qwen_image_uses_dashscope_multimodal_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(
                root,
                stage1_provider="qwen",
                stage1_model="qwen3-max-2026-01-23",
                stage2_provider="qwen",
                stage2_model="qwen3-max-2026-01-23",
                image_provider="qwen",
                image_model="qwen-image-2.0-pro",
            )
            fake_dashscope = SimpleNamespace(
                base_http_api_url="",
                MultiModalConversation=SimpleNamespace(
                    call=MagicMock(
                        return_value=SimpleNamespace(
                            status_code=200,
                            output=SimpleNamespace(
                                choices=[
                                    SimpleNamespace(
                                        message=SimpleNamespace(
                                            content=[{"image": "https://example.com/qwen.png"}]
                                        )
                                    )
                                ]
                            ),
                        )
                    )
                ),
            )

            with patch("dropline.llm._import_dashscope", return_value=fake_dashscope), patch(
                "dropline.llm._download_image_bytes",
                return_value=b"fake-qwen-png",
            ):
                client = QwenBackedClient(settings)
                image = client.generate_image(SCENE_CARD)

            self.assertEqual(image.image_bytes, b"fake-qwen-png")
            self.assertEqual(fake_dashscope.base_http_api_url, "https://dashscope.aliyuncs.com/api/v1")
            kwargs = fake_dashscope.MultiModalConversation.call.call_args.kwargs
            self.assertEqual(kwargs["model"], "qwen-image-2.0-pro")
            self.assertEqual(kwargs["result_format"], "message")
            self.assertEqual(kwargs["size"], "2048*2048")
            self.assertFalse(kwargs["watermark"])
            self.assertTrue(kwargs["prompt_extend"])
            self.assertIn("画面风格：二次元插画", kwargs["messages"][0]["content"][0]["text"])

    def test_deepseek_stage1_uses_openai_compatible_api(self) -> None:
        self._assert_openai_compatible_text_client(
            client_cls=DeepSeekBackedClient,
            provider_name="deepseek",
            model_name="deepseek-v4-pro",
            expected_base_url="https://api.deepseek.com",
        )

    def test_deepseek_stage1_requests_json_output_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(
                root,
                stage1_provider="deepseek",
                stage1_model="deepseek-v4-pro",
                stage2_provider="deepseek",
                stage2_model="deepseek-v4-pro",
            )
            fake_client = MagicMock()
            fake_client.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=_stage1_payload_json()))]
            )

            with patch("openai.OpenAI", return_value=fake_client):
                client = DeepSeekBackedClient(settings)
                result = client.generate_stage1({"inbound_messages": []})

            self.assertEqual(result.emotional_priority, "comfort")
            kwargs = fake_client.chat.completions.create.call_args.kwargs
            self.assertEqual(kwargs["response_format"], {"type": "json_object"})

    def test_deepseek_stage1_accepts_markdown_wrapped_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(
                root,
                stage1_provider="deepseek",
                stage1_model="deepseek-v4-pro",
                stage2_provider="deepseek",
                stage2_model="deepseek-v4-pro",
            )
            fake_client = MagicMock()
            wrapped_json = f"这里是结果：\n```json\n{_stage1_payload_json()}\n```"
            fake_client.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=wrapped_json))]
            )

            with patch("openai.OpenAI", return_value=fake_client):
                client = DeepSeekBackedClient(settings)
                result = client.generate_stage1({"inbound_messages": []})

            self.assertEqual(result.emotional_priority, "comfort")

    def test_glm_stage1_uses_openai_compatible_api(self) -> None:
        self._assert_openai_compatible_text_client(
            client_cls=GLMBackedClient,
            provider_name="glm",
            model_name="GLM-5.1",
            expected_base_url="https://open.bigmodel.cn/api/paas/v4",
        )

    def test_doubao_stage1_uses_openai_compatible_api(self) -> None:
        self._assert_openai_compatible_text_client(
            client_cls=DoubaoBackedClient,
            provider_name="doubao",
            model_name="doubao-seed-2-0-pro-260215",
            expected_base_url="https://ark.cn-beijing.volces.com/api/v3",
        )

    def test_doubao_image_uses_ark_images_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(
                root,
                stage1_provider="doubao",
                stage1_model="doubao-seed-2-0-pro-260215",
                stage2_provider="doubao",
                stage2_model="doubao-seed-2-0-pro-260215",
                image_provider="doubao",
                image_model="doubao-seedream-5-0-260128",
            )
            fake_client = MagicMock()
            fake_client.images.generate.return_value = SimpleNamespace(
                data=[SimpleNamespace(url="https://example.com/doubao.png")]
            )

            with patch("openai.OpenAI", return_value=fake_client), patch(
                "dropline.llm._download_image_bytes",
                return_value=b"fake-doubao-png",
            ):
                client = DoubaoBackedClient(settings)
                image = client.generate_image(SCENE_CARD)

            self.assertEqual(image.image_bytes, b"fake-doubao-png")
            kwargs = fake_client.images.generate.call_args.kwargs
            self.assertEqual(kwargs["model"], "doubao-seedream-5-0-260128")
            self.assertEqual(kwargs["size"], "2K")
            self.assertEqual(kwargs["output_format"], "png")
            self.assertEqual(kwargs["response_format"], "url")
            self.assertEqual(kwargs["extra_body"], {"watermark": False})
            self.assertIn("画面风格：二次元插画", kwargs["prompt"])

    def test_render_scene_card_prompt_uses_english_template(self) -> None:
        prompt = render_scene_card_prompt("Keqing", SCENE_CARD_ENGLISH, language="English")
        self.assertIn("Character: Keqing.", prompt)
        self.assertIn("Visual style: anime illustration.", prompt)
        self.assertIn("Camera distance: medium shot.", prompt)
        self.assertNotIn("画面风格", prompt)

    def test_render_scene_card_prompt_includes_recent_similarity_guardrail(self) -> None:
        chinese_prompt = render_scene_card_prompt("刻晴", SCENE_CARD)
        english_prompt = render_scene_card_prompt("Keqing", SCENE_CARD_ENGLISH, language="English")

        self.assertIn("避免与最近生成的图像", chinese_prompt)
        self.assertIn("Avoid making this image highly similar", english_prompt)

    def test_build_ai_client_routes_stage_and_image_providers_independently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(
                root,
                stage1_provider="anthropic",
                stage1_model="claude-opus-4-7",
                stage2_provider="anthropic",
                stage2_model="claude-opus-4-7",
                image_provider="grok",
                image_model="grok-imagine-image-pro",
            )
            anthropic_client = MagicMock()
            anthropic_client.messages.create.return_value = SimpleNamespace(
                content=[SimpleNamespace(type="text", text=_stage1_payload_json())]
            )
            grok_client = MagicMock()
            grok_client.images.generate.return_value = SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(b"routed-image").decode("ascii"))]
            )

            with patch("dropline.llm._import_anthropic", return_value=MagicMock(return_value=anthropic_client)):
                with patch("openai.OpenAI", return_value=grok_client):
                    client = build_ai_client(settings)
                    stage1 = client.generate_stage1({"inbound_messages": []})
                    image = client.generate_image(SCENE_CARD)

            self.assertEqual(stage1.emotional_priority, "comfort")
            self.assertEqual(image.image_bytes, b"routed-image")
            self.assertEqual(anthropic_client.messages.create.call_count, 1)
            self.assertEqual(grok_client.images.generate.call_count, 1)

    def test_build_ai_client_routes_qwen_and_deepseek_text_with_grok_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(
                root,
                stage1_provider="qwen",
                stage1_model="qwen3-max-2026-01-23",
                stage2_provider="deepseek",
                stage2_model="deepseek-v4-pro",
                image_provider="grok",
                image_model="grok-imagine-image-pro",
            )
            qwen_client = MagicMock()
            qwen_client.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=_stage1_payload_json()))]
            )
            deepseek_client = MagicMock()
            deepseek_client.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=_stage2_payload_json()))]
            )
            grok_client = MagicMock()
            grok_client.images.generate.return_value = SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(b"routed-new-image").decode("ascii"))]
            )

            with patch("openai.OpenAI", side_effect=[qwen_client, deepseek_client, grok_client]):
                client = build_ai_client(settings)
                stage1 = client.generate_stage1({"inbound_messages": []})
                stage2 = client.generate_stage2({"inbound_messages": []})
                image = client.generate_image(SCENE_CARD)

            self.assertEqual(stage1.emotional_priority, "comfort")
            self.assertEqual(stage2.subject, "今晚先到这里")
            self.assertEqual(image.image_bytes, b"routed-new-image")
            self.assertEqual(qwen_client.chat.completions.create.call_count, 1)
            self.assertEqual(deepseek_client.chat.completions.create.call_count, 1)
            self.assertEqual(grok_client.images.generate.call_count, 1)

    def test_build_ai_client_routes_qwen_text_and_doubao_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = self._load_settings(
                root,
                stage1_provider="qwen",
                stage1_model="qwen3-max-2026-01-23",
                stage2_provider="qwen",
                stage2_model="qwen3-max-2026-01-23",
                image_provider="doubao",
                image_model="doubao-seedream-5-0-260128",
            )
            qwen_client = MagicMock()
            qwen_client.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=_stage1_payload_json()))]
            )
            doubao_client = MagicMock()
            doubao_client.images.generate.return_value = SimpleNamespace(
                data=[SimpleNamespace(url="https://example.com/routed-doubao.png")]
            )

            with patch("openai.OpenAI", side_effect=[qwen_client, doubao_client]), patch(
                "dropline.llm._download_image_bytes",
                return_value=b"routed-doubao-image",
            ):
                client = build_ai_client(settings)
                stage1 = client.generate_stage1({"inbound_messages": []})
                image = client.generate_image(SCENE_CARD)

            self.assertEqual(stage1.emotional_priority, "comfort")
            self.assertEqual(image.image_bytes, b"routed-doubao-image")
            self.assertEqual(qwen_client.chat.completions.create.call_count, 1)
            self.assertEqual(doubao_client.images.generate.call_count, 1)


class RestoreVerbatimFieldsTests(unittest.TestCase):
    def test_evidence_and_source_date_are_copied_back_after_repair(self) -> None:
        from dropline.llm import _restore_verbatim_fields

        original = {"candidate_memories": [{"content": "用户喜欢猫", "evidence": "我喜欢猫", "source_date": "2026-03-31"}]}
        repaired = {"candidate_memories": [{"content": "The user likes cats", "evidence": "I like cats", "source_date": "2026/03/31"}]}

        result = _restore_verbatim_fields(original, repaired)

        self.assertEqual(result["candidate_memories"][0]["content"], "The user likes cats")
        self.assertEqual(result["candidate_memories"][0]["evidence"], "我喜欢猫")
        self.assertEqual(result["candidate_memories"][0]["source_date"], "2026-03-31")

    def test_mismatched_candidate_counts_leave_the_repaired_output_alone(self) -> None:
        from dropline.llm import _restore_verbatim_fields

        original = {"candidate_memories": [{"evidence": "a"}, {"evidence": "b"}]}
        repaired = {"candidate_memories": [{"evidence": "x"}]}

        self.assertEqual(_restore_verbatim_fields(original, repaired)["candidate_memories"][0]["evidence"], "x")


if __name__ == "__main__":
    unittest.main()
