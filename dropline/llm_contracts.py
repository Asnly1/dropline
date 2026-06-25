from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .config import Settings
from .models import CharacterProfileDraft, RelationshipProfileDraft, Stage1Result, Stage2Result
from .provider_registry import get_provider_text_model


CHINESE_OUTPUT_LANGUAGE = "简体中文"
ENGLISH_OUTPUT_LANGUAGE = "English"


STAGE1_INSTRUCTIONS = (
    "你是虚拟人物邮件系统的 Stage 1 分析器。"
    "只做来信理解和候选记忆提取。"
    "禁止写正文、禁止扩展世界观、禁止把一次性情绪直接晋升为长期人格结论。"
    "必须输出 JSON。"
)

STAGE1_INSTRUCTIONS_ENGLISH = (
    "You are the Stage 1 analyzer for a fictional character email system. "
    "Only understand inbound mail and extract candidate memories. "
    "Do not write the reply body, expand the world state, or promote one-off emotions into long-term personality conclusions. "
    "You must output JSON."
)

STAGE2_INSTRUCTIONS = (
    "你是虚拟人物邮件系统的 Stage 2 写作者。"
    "必须保持角色一致、聚焦一件主事、生成完整回信、character_episode、active_arc_update_suggestion 和 scene_card。"
    "输入里的 user_profile、relationship_memory、active_arc 是长期记忆：user_profile 放用户的稳定事实、稳定偏好与长期目标；relationship_memory 放你与用户之间的称呼、约定、共同梗与未完的话头；active_arc 是最多 1~2 条仍在推进的连续主线，由 active_arc_update_suggestion 决定保持、更新或归档。"
    "图片场景只能由 scene_card 承载，不要把正文细节随意外延。"
    "scene_card 必须包含明确的 visual_style，且要与角色类型一致，例如二次元角色应偏二次元插画，现实人物应偏三次元写实。"
    "如果输入包含 recent_scene_cards，它们代表最近已经生成过的图片场景；新的 scene_card 必须主动避开与这些记录高度相似的地点、时段、动作、关键物件和镜头距离。"
    "允许夜晚场景，但不要连续依赖夜晚案前写信、整理文书或静坐灯下这类重复画面。"
    "优先从该角色自身的日常事务、工作场景与生活环境里取不同瞬间的景，而不是反复使用同一个画面。"
    "必须输出 JSON。"
)

STAGE2_INSTRUCTIONS_ENGLISH = (
    "You are the Stage 2 writer for a fictional character email system. "
    "You must preserve character consistency, focus on one main matter, and produce a complete reply, "
    "character_episode, active_arc_update_suggestion, and scene_card. "
    "In the input, user_profile, relationship_memory, and active_arc are long-term memory: user_profile holds the user's stable facts, stable preferences, and long-term goals; relationship_memory holds the names, agreements, shared jokes, and open threads between you and the user; active_arc holds at most 1-2 ongoing storylines that you maintain via active_arc_update_suggestion (keep, update, or archive). "
    "Image scene details must be carried only by scene_card; do not casually extend body details into the image. "
    "scene_card must include a clear visual_style matched to the character type; for example, anime characters should lean "
    "toward anime illustration, and real people should lean toward live-action realism. "
    "If the input contains recent_scene_cards, they represent recently generated image scenes; the new scene_card must "
    "actively avoid highly similar location, time, action, key object, and camera distance. "
    "Night scenes are allowed, but do not repeatedly rely on night-at-desk letter writing, paperwork sorting, or sitting under a lamp. "
    "Prefer different moments drawn from the character's own duties, work settings, and surroundings, rather than repeating the same frame. "
    "You must output JSON."
)

STAGE2_TEXT_ONLY_INSTRUCTIONS = (
    "你是虚拟人物邮件系统的 Stage 2 写作者。"
    "必须保持角色一致、聚焦一件主事、生成完整回信、character_episode 和 active_arc_update_suggestion。"
    "输入里的 user_profile、relationship_memory、active_arc 是长期记忆：user_profile 放用户的稳定事实、稳定偏好与长期目标；relationship_memory 放你与用户之间的称呼、约定、共同梗与未完的话头；active_arc 是最多 1~2 条仍在推进的连续主线，由 active_arc_update_suggestion 决定保持、更新或归档。"
    "当前图片生成功能已关闭，不要输出 scene_card，不要为图片额外设计画面描述。"
    "必须输出 JSON。"
)

STAGE2_TEXT_ONLY_INSTRUCTIONS_ENGLISH = (
    "You are the Stage 2 writer for a fictional character email system. "
    "You must preserve character consistency, focus on one main matter, and produce a complete reply, "
    "character_episode, and active_arc_update_suggestion. "
    "In the input, user_profile, relationship_memory, and active_arc are long-term memory: user_profile holds the user's stable facts, stable preferences, and long-term goals; relationship_memory holds the names, agreements, shared jokes, and open threads between you and the user; active_arc holds at most 1-2 ongoing storylines that you maintain via active_arc_update_suggestion (keep, update, or archive). "
    "Image generation is currently disabled; do not output scene_card and do not design extra image descriptions. "
    "You must output JSON."
)

CHARACTER_PROFILE_INSTRUCTIONS = (
    "你是虚拟人物邮件系统的角色设定生成器。"
    "输入包含角色名，work_title 可能存在也可能为空。"
    "如果提供了 work_title，就把它当成作品角色来写。"
    "如果 work_title 为空，就把这个角色当成现实中的公众人物来写。"
    "现实人物模式只允许参考公开可见形象、公开表达风格、公开活动与公共语境。"
    "不要编造私密经历、私人关系、未经证实的内在设定或私生活细节。"
    "你要为私人通信场景生成角色提示文档，强调角色性格、语言边界、世界状态、配角关系与现代输入转译。"
    "输出必须是结构化 JSON，各章节字段都要写成可长期复用的 prompt 内容。"
    "不要把任何章节写成一句摘要，不要留空，不要把 cast_pool 写成只有几个人名。"
    "每个字符串数组中的一项都应是一条完整、具体、可执行的提示语句。"
    "不要输出 JSON 以外的任何解释。"
)

CHARACTER_PROFILE_INSTRUCTIONS_ENGLISH = (
    "You are the character profile generator for a fictional character email system. "
    "The input contains a character name, and work_title may be present or empty. "
    "If work_title is provided, treat the character as a fictional work character. "
    "If work_title is empty, treat the character as a real public figure. "
    "In real-person mode, only rely on public image, public communication style, public activity, and public context. "
    "Do not invent private experiences, private relationships, unsupported inner settings, or private-life details. "
    "Generate character prompt documents for a private communication setting, emphasizing personality, language boundaries, "
    "world state, supporting cast relationships, and translation from modern input. "
    "Output structured JSON; every section field should be long-term reusable prompt content. "
    "Do not reduce any section to a one-sentence summary, leave it empty, or make cast_pool only a few names. "
    "Each item in a string array should be a complete, specific, actionable prompt sentence. "
    "Do not output any explanation outside JSON."
)

RELATIONSHIP_PROFILE_INSTRUCTIONS = (
    "你是虚拟人物邮件系统的关系设定生成器。"
    "输入包含角色名、角色 prompt 文档、关系名称与关系描述。"
    "如果角色 prompt 文档体现的是现实公众人物语境，就继续遵守公开形象优先，不要生成高度私密或未经支持的真实生活模拟。"
    "你要基于这个角色本身生成一份专属于该角色的 relationship.md。"
    "输出必须是结构化 JSON，各章节字段都要写成能直接驱动长期写信风格的条目。"
    "不要把 relationship 写成一段简介，不要把整份关系设定压缩成一句话。"
    "不要把它写成可跨角色复用的抽象关系模板。"
    "必须体现这个关系如何作用于该角色的写信方式、边界、亲密尺度与历史语境。"
    "不要输出 JSON 以外的任何解释。"
)

RELATIONSHIP_PROFILE_INSTRUCTIONS_ENGLISH = (
    "You are the relationship profile generator for a fictional character email system. "
    "The input contains the character name, character prompt documents, relationship name, and relationship description. "
    "If the character prompt documents indicate a real public-figure context, continue prioritizing public image and do not generate highly private "
    "or unsupported real-life simulation. "
    "Generate a relationship.md that belongs specifically to this character. "
    "Output structured JSON; every section field should directly drive long-term letter style. "
    "Do not make the relationship a short introduction or compress the whole setting into one sentence. "
    "Do not make it an abstract relationship template reusable across characters. "
    "It must show how this relationship affects the character's letter-writing style, boundaries, intimacy level, and history. "
    "Do not output any explanation outside JSON."
)


def _string_list_schema() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


STAGE1_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["user_digest", "candidate_memories", "emotional_priority", "open_loops"],
    "properties": {
        "user_digest": {
            "type": "object",
            "additionalProperties": False,
            "required": ["main_event", "state", "possible_needs"],
            "properties": {
                "main_event": {"type": "string"},
                "state": {"type": "string"},
                "possible_needs": _string_list_schema(),
            },
        },
        "candidate_memories": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "content",
                    "type",
                    "confidence",
                    "durability",
                    "evidence",
                    "why_it_matters",
                    "source_date",
                ],
                "properties": {
                    "content": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": ["user_profile", "relationship_memory", "active_arc", "user_episode", "discard"],
                    },
                    "confidence": {"type": "number"},
                    "durability": {"type": "string", "enum": ["transient", "short", "stable"]},
                    "evidence": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "source_date": {"type": "string"},
                },
            },
        },
        "emotional_priority": {"type": "string"},
        "open_loops": _string_list_schema(),
    },
}

SCENE_CARD_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["visual_style", "location", "action", "key_object", "time", "mood", "camera_distance"],
    "properties": {
        "visual_style": {"type": "string"},
        "location": {"type": "string"},
        "action": {"type": "string"},
        "key_object": {"type": "string"},
        "time": {"type": "string"},
        "mood": {"type": "string"},
        "camera_distance": {"type": "string"},
    },
}


def build_stage2_response_schema(*, require_scene_card: bool) -> dict[str, Any]:
    required = ["subject", "body", "character_episode", "active_arc_update_suggestion"]
    if require_scene_card:
        required.append("scene_card")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": {
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "character_episode": {
                "type": "object",
                "additionalProperties": False,
                "required": ["summary", "continuity_value"],
                "properties": {"summary": {"type": "string"}, "continuity_value": {"type": "string"}},
            },
            "active_arc_update_suggestion": {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "content"],
                "properties": {
                    "action": {"type": "string", "enum": ["none", "keep", "update", "archive"]},
                    "content": {"type": "string"},
                },
            },
            "scene_card": SCENE_CARD_RESPONSE_SCHEMA,
        },
    }


def _focus_relationship_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "relationship_tone", "writing_points", "forbidden_patterns"],
        "properties": {
            "name": {"type": "string"},
            "relationship_tone": _string_list_schema(),
            "writing_points": _string_list_schema(),
            "forbidden_patterns": _string_list_schema(),
        },
    }


def _translation_processing_mode_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "guidance", "examples"],
        "properties": {"name": {"type": "string"}, "guidance": _string_list_schema(), "examples": _string_list_schema()},
    }


def _translation_mapping_group_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["category", "mappings"],
        "properties": {"category": {"type": "string"}, "mappings": _string_list_schema()},
    }


def _translation_reply_pattern_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["scenario", "preferred", "avoid"],
        "properties": {
            "scenario": {"type": "string"},
            "preferred": _string_list_schema(),
            "avoid": _string_list_schema(),
        },
    }


CHARACTER_PROFILE_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["persona", "world_state", "cast_relations", "cast_pool", "translation_layer"],
    "properties": {
        "persona": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "identity_and_role",
                "core_values",
                "personality_traits",
                "language_style",
                "repeatable_life_details",
                "ooc_boundaries",
            ],
            "properties": {
                "identity_and_role": _string_list_schema(),
                "core_values": _string_list_schema(),
                "personality_traits": _string_list_schema(),
                "language_style": _string_list_schema(),
                "repeatable_life_details": _string_list_schema(),
                "ooc_boundaries": _string_list_schema(),
            },
        },
        "world_state": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "worldview_type",
                "time_anchor",
                "overall_atmosphere",
                "character_work_domain",
                "sustainable_storylines",
                "appearance_boundaries",
                "letter_scale",
                "image_principles",
            ],
            "properties": {
                "worldview_type": _string_list_schema(),
                "time_anchor": _string_list_schema(),
                "overall_atmosphere": _string_list_schema(),
                "character_work_domain": _string_list_schema(),
                "sustainable_storylines": _string_list_schema(),
                "appearance_boundaries": _string_list_schema(),
                "letter_scale": _string_list_schema(),
                "image_principles": _string_list_schema(),
            },
        },
        "cast_relations": {
            "type": "object",
            "additionalProperties": False,
            "required": ["focus_relationships"],
            "properties": {"focus_relationships": {"type": "array", "items": _focus_relationship_schema()}},
        },
        "cast_pool": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "usage_principles",
                "tier_1_high_frequency",
                "tier_2_task_support",
                "tier_3_daily_life",
                "tier_4_heavyweight_low_frequency",
                "tier_5_background_flavor",
                "frequency_rules",
            ],
            "properties": {
                "usage_principles": _string_list_schema(),
                "tier_1_high_frequency": _string_list_schema(),
                "tier_2_task_support": _string_list_schema(),
                "tier_3_daily_life": _string_list_schema(),
                "tier_4_heavyweight_low_frequency": _string_list_schema(),
                "tier_5_background_flavor": _string_list_schema(),
                "frequency_rules": _string_list_schema(),
            },
        },
        "translation_layer": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "purpose",
                "general_principles",
                "processing_modes",
                "semantic_mappings",
                "emotion_fidelity_rules",
                "replying_patterns",
                "forbidden_patterns",
            ],
            "properties": {
                "purpose": _string_list_schema(),
                "general_principles": _string_list_schema(),
                "processing_modes": {"type": "array", "items": _translation_processing_mode_schema()},
                "semantic_mappings": {"type": "array", "items": _translation_mapping_group_schema()},
                "emotion_fidelity_rules": _string_list_schema(),
                "replying_patterns": {"type": "array", "items": _translation_reply_pattern_schema()},
                "forbidden_patterns": _string_list_schema(),
            },
        },
    },
}

RELATIONSHIP_PROFILE_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "setup_statement",
        "relationship_definition",
        "shared_past",
        "relationship_tone",
        "attitude_toward_user",
        "how_it_affects_letters",
        "intimacy_threshold",
        "explicit_boundaries",
        "warming_up_path",
    ],
    "properties": {
        "setup_statement": _string_list_schema(),
        "relationship_definition": _string_list_schema(),
        "shared_past": _string_list_schema(),
        "relationship_tone": _string_list_schema(),
        "attitude_toward_user": _string_list_schema(),
        "how_it_affects_letters": _string_list_schema(),
        "intimacy_threshold": _string_list_schema(),
        "explicit_boundaries": _string_list_schema(),
        "warming_up_path": _string_list_schema(),
    },
}


@dataclass(frozen=True, slots=True)
class StructuredTaskSpec:
    schema_name: str
    instructions_by_language: dict[str, str]
    schema: dict[str, Any]
    model_for: Callable[[Settings], str]
    parse_result: Callable[[dict[str, Any]], Any]

    def instructions_for(self, settings: Settings) -> str:
        return self.instructions_by_language.get(
            settings.llm.language,
            self.instructions_by_language[CHINESE_OUTPUT_LANGUAGE],
        )


def _stage1_model(settings: Settings) -> str:
    return get_provider_text_model(settings, settings.llm.stage1.provider)


def _stage2_model(settings: Settings) -> str:
    return get_provider_text_model(settings, settings.llm.stage2.provider)


STAGE1_TASK = StructuredTaskSpec(
    schema_name="stage1_result",
    instructions_by_language={
        CHINESE_OUTPUT_LANGUAGE: STAGE1_INSTRUCTIONS,
        ENGLISH_OUTPUT_LANGUAGE: STAGE1_INSTRUCTIONS_ENGLISH,
    },
    schema=STAGE1_RESPONSE_SCHEMA,
    model_for=_stage1_model,
    parse_result=Stage1Result.from_dict,
)


def build_stage2_task(settings: Settings) -> StructuredTaskSpec:
    require_scene_card = settings.llm.image.enabled
    return StructuredTaskSpec(
        schema_name="stage2_result",
        instructions_by_language={
            CHINESE_OUTPUT_LANGUAGE: STAGE2_INSTRUCTIONS if require_scene_card else STAGE2_TEXT_ONLY_INSTRUCTIONS,
            ENGLISH_OUTPUT_LANGUAGE: STAGE2_INSTRUCTIONS_ENGLISH
            if require_scene_card
            else STAGE2_TEXT_ONLY_INSTRUCTIONS_ENGLISH,
        },
        schema=build_stage2_response_schema(require_scene_card=require_scene_card),
        model_for=_stage2_model,
        parse_result=Stage2Result.from_dict,
    )


CHARACTER_PROFILE_TASK = StructuredTaskSpec(
    schema_name="character_profile",
    instructions_by_language={
        CHINESE_OUTPUT_LANGUAGE: CHARACTER_PROFILE_INSTRUCTIONS,
        ENGLISH_OUTPUT_LANGUAGE: CHARACTER_PROFILE_INSTRUCTIONS_ENGLISH,
    },
    schema=CHARACTER_PROFILE_RESPONSE_SCHEMA,
    model_for=_stage2_model,
    parse_result=CharacterProfileDraft.from_dict,
)

RELATIONSHIP_PROFILE_TASK = StructuredTaskSpec(
    schema_name="relationship_profile",
    instructions_by_language={
        CHINESE_OUTPUT_LANGUAGE: RELATIONSHIP_PROFILE_INSTRUCTIONS,
        ENGLISH_OUTPUT_LANGUAGE: RELATIONSHIP_PROFILE_INSTRUCTIONS_ENGLISH,
    },
    schema=RELATIONSHIP_PROFILE_RESPONSE_SCHEMA,
    model_for=_stage2_model,
    parse_result=RelationshipProfileDraft.from_dict,
)


def character_profile_payload(
    *,
    work_title: str | None,
    character_name: str,
) -> dict[str, Any]:
    return {
        "work_title": work_title,
        "character_name": character_name,
    }


def relationship_profile_payload(
    *,
    character_name: str,
    character_prompt_docs: dict[str, str],
    relationship_name: str,
    relationship_description: str,
) -> dict[str, Any]:
    return {
        "character_name": character_name,
        "character_prompt_docs": character_prompt_docs,
        "relationship_name": relationship_name,
        "relationship_description": relationship_description,
    }
