from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .config import Settings, load_settings, resolve_repo_root_from_config
from .documents import read_optional_text
from .llm import build_profile_generation_client
from .llm_contracts import CHINESE_OUTPUT_LANGUAGE, ENGLISH_OUTPUT_LANGUAGE
from .memory import MemoryManager
from .models import CharacterProfileDraft, RelationshipProfileDraft, StoredInboundMessage
from .provider_registry import get_provider_text_model
from .profile_manager import (
    CharacterProfileMeta,
    RelationshipProfileMeta,
    SessionProfileMeta,
    character_display_name,
    character_profile_dir,
    character_prompt_dir,
    clear_active_profile_selection,
    ensure_profile_roots,
    list_character_profile_metas,
    list_relationship_profile_metas,
    load_character_profile_meta,
    load_relationship_profile_meta,
    load_session_profile_meta,
    load_selected_profile_context,
    relationship_profile_dir,
    relationship_prompt_dir,
    resolve_character_profile_meta,
    resolve_relationship_profile_meta,
    save_character_profile_meta,
    save_relationship_profile_meta,
    select_active_profile,
    session_profile_dir,
    sessions_root,
    slugify,
)
from .storage import SessionScope, StateStore
from .utils import resolve_path_within_roots


LOGGER = logging.getLogger(__name__)

PROFILE_RENDER_TEXT = {
    CHINESE_OUTPUT_LANGUAGE: {
        "mode_work": "作品角色延展型",
        "mode_public": "现实公众人物通信化",
        "public_anchor": "现实公众人物公开语境",
        "canon_status_supplement": "非原作补设",
        "persona_sections": [
            "身份与定位",
            "核心价值观",
            "性格特征",
            "语言风格",
            "角色可反复使用的生活细节",
            "明确禁止的 OOC",
        ],
        "world_state_sections": [
            "世界观类型",
            "时间锚点",
            "整体氛围",
            "角色的工作域 / 活动域",
            "可持续主线池",
            "出场边界",
            "写信尺度",
            "附图原则",
        ],
        "cast_relations_scope": "与{character_name}关系最强、最常影响其判断和口吻的人物",
        "relationship_tone": "关系底色",
        "writing_points": "写法要点",
        "forbidden": "禁止",
        "cast_pool_scope": "{character_name}可出场角色池，按功能分层",
        "usage_principles": "使用原则",
        "principles": "原则",
        "tier_1": "第一层：高频关系轴",
        "high_frequency_characters": "高频角色",
        "tier_2": "第二层：事务协力圈",
        "task_support_characters": "事务协力角色",
        "tier_3": "第三层：市井生活圈",
        "daily_life_characters": "生活补味角色",
        "tier_4": "第四层：低频高重量圈",
        "heavyweight_low_frequency_characters": "低频高重量角色",
        "tier_5": "第五层：背景补味圈",
        "background_characters": "背景角色",
        "frequency_rules": "出场频率规则",
        "rules": "规则",
        "translation_scope": "用户现实表述 -> {character_name}可自然回应的书信表述",
        "translation_purpose": "保持原意不丢失，同时避免现代口吻直接击穿角色感",
        "purpose": "目的",
        "general_principles": "总原则",
        "processing_modes": "处理模式",
        "semantic_mapping_table": "常见语义映射表",
        "emotion_fidelity_rules": "情绪保真规则",
        "reply_patterns": "回应方式",
        "explicit_forbidden": "明确禁止",
        "examples": "示例：",
        "preferred": "优先：",
        "avoid": "避免：",
        "relationship_sections": [
            "设定声明",
            "关系定义",
            "共同过去",
            "关系底色",
            "对用户的态度",
            "这段关系如何影响写信",
            "亲密阈值（非常关键）",
            "明确边界",
            "关系升温方式（若后续需要）",
        ],
    },
    ENGLISH_OUTPUT_LANGUAGE: {
        "mode_work": "Fictional work adaptation",
        "mode_public": "Public-figure communication mode",
        "public_anchor": "Real public figure public context",
        "canon_status_supplement": "Non-canon supplement",
        "persona_sections": [
            "Identity and Role",
            "Core Values",
            "Personality Traits",
            "Language Style",
            "Repeatable Life Details",
            "Explicit OOC Boundaries",
        ],
        "world_state_sections": [
            "Worldview Type",
            "Time Anchor",
            "Overall Atmosphere",
            "Character Work Domain / Activity Domain",
            "Sustainable Storyline Pool",
            "Appearance Boundaries",
            "Letter Scale",
            "Image Principles",
        ],
        "cast_relations_scope": "Characters whose relationships with {character_name} most strongly affect judgment and tone",
        "relationship_tone": "Relationship Tone",
        "writing_points": "Writing Points",
        "forbidden": "Forbidden",
        "cast_pool_scope": "Cast pool available to {character_name}, grouped by function",
        "usage_principles": "Usage Principles",
        "principles": "Principles",
        "tier_1": "Tier 1: High-Frequency Relationship Axis",
        "high_frequency_characters": "High-Frequency Characters",
        "tier_2": "Tier 2: Task Support Circle",
        "task_support_characters": "Task Support Characters",
        "tier_3": "Tier 3: Daily Life Circle",
        "daily_life_characters": "Daily Life Characters",
        "tier_4": "Tier 4: Low-Frequency Heavyweight Circle",
        "heavyweight_low_frequency_characters": "Low-Frequency Heavyweight Characters",
        "tier_5": "Tier 5: Background Flavor Circle",
        "background_characters": "Background Characters",
        "frequency_rules": "Appearance Frequency Rules",
        "rules": "Rules",
        "translation_scope": "User real-world expression -> letter expression {character_name} can naturally answer",
        "translation_purpose": "Preserve the original meaning while preventing modern wording from breaking character voice",
        "purpose": "Purpose",
        "general_principles": "General Principles",
        "processing_modes": "Processing Modes",
        "semantic_mapping_table": "Common Semantic Mapping Table",
        "emotion_fidelity_rules": "Emotion Fidelity Rules",
        "reply_patterns": "Reply Patterns",
        "explicit_forbidden": "Explicitly Forbidden",
        "examples": "Examples:",
        "preferred": "Preferred:",
        "avoid": "Avoid:",
        "relationship_sections": [
            "Setup Statement",
            "Relationship Definition",
            "Shared Past",
            "Relationship Tone",
            "Attitude Toward the User",
            "How This Relationship Affects Letters",
            "Intimacy Threshold (Critical)",
            "Explicit Boundaries",
            "Warming-Up Path (If Needed Later)",
        ],
    },
}


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def _repo_root_from_config(config_path: str) -> Path:
    return resolve_repo_root_from_config(config_path)


def _created_at() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_work_title(work_title: str | None) -> str:
    return (work_title or "").strip()


def _relationship_name_from_input(name: str | None, relationship: str) -> str:
    if name and name.strip():
        return name.strip()
    collapsed = " ".join(relationship.split())
    return collapsed[:24] or "relationship"


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _log_profile_step(profile_label: str, step: int, total: int, message: str, **fields: object) -> None:
    suffix = ""
    if fields:
        suffix = " " + ", ".join(f"{key}={value}" for key, value in fields.items())
    LOGGER.info("[%s] [%s/%s] %s%s", profile_label, step, total, message, suffix)


def _load_character_prompt_docs(repo_root: Path, character_slug: str) -> dict[str, str]:
    prompt_root = character_prompt_dir(repo_root, character_slug)
    return {
        "persona": read_optional_text(prompt_root / "persona.md"),
        "world_state": read_optional_text(prompt_root / "world_state.md"),
        "cast_relations": read_optional_text(prompt_root / "cast_relations.md"),
        "cast_pool": read_optional_text(prompt_root / "cast_pool.md"),
        "translation_layer": read_optional_text(prompt_root / "translation_layer.md"),
    }


def _render_language(language: str) -> str:
    if language == ENGLISH_OUTPUT_LANGUAGE:
        return ENGLISH_OUTPUT_LANGUAGE
    return CHINESE_OUTPUT_LANGUAGE


def _render_text(language: str, key: str) -> Any:
    return PROFILE_RENDER_TEXT[_render_language(language)][key]


def _profile_mode_label(work_title: str, *, language: str = CHINESE_OUTPUT_LANGUAGE) -> str:
    if work_title:
        return str(_render_text(language, "mode_work"))
    return str(_render_text(language, "mode_public"))


def _render_frontmatter(lines: list[tuple[str, str]]) -> str:
    rendered = ["---"]
    for key, value in lines:
        if not value:
            continue
        rendered.append(f"{key}: {value}")
    rendered.append("---")
    return "\n".join(rendered)


def _render_bullet_lines(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items]


def _render_markdown_document(*, frontmatter: list[tuple[str, str]], title: str, blocks: list[str]) -> str:
    parts = [_render_frontmatter(frontmatter), "", f"# {title}", ""]
    for index, block in enumerate(blocks):
        if index:
            parts.append("")
        parts.append(block.rstrip())
    return "\n".join(parts).rstrip() + "\n"


def _render_numbered_section(number: int, title: str, items: list[str]) -> str:
    return "\n".join([f"## {number}. {title}", "", *_render_bullet_lines(items)])


def _render_named_subsection(title: str, sections: list[tuple[str, list[str]]]) -> str:
    lines = [f"## {title}", ""]
    for index, (subtitle, items) in enumerate(sections):
        if index:
            lines.append("")
        lines.append(f"### {subtitle}")
        lines.append("")
        lines.extend(_render_bullet_lines(items))
    return "\n".join(lines)


def _render_persona_markdown(
    meta: CharacterProfileMeta,
    draft: CharacterProfileDraft,
    *,
    language: str = CHINESE_OUTPUT_LANGUAGE,
) -> str:
    sections = list(
        zip(
            _render_text(language, "persona_sections"),
            (
                draft.persona.identity_and_role,
                draft.persona.core_values,
                draft.persona.personality_traits,
                draft.persona.language_style,
                draft.persona.repeatable_life_details,
                draft.persona.ooc_boundaries,
            ),
        )
    )
    return _render_markdown_document(
        frontmatter=[
            ("version", "0.1"),
            ("character", meta.character_name),
            ("mode", _profile_mode_label(meta.work_title, language=language)),
        ],
        title="persona.md",
        blocks=[
            _render_numbered_section(number, title, items) for number, (title, items) in enumerate(sections, start=1)
        ],
    )


def _render_world_state_markdown(
    meta: CharacterProfileMeta,
    draft: CharacterProfileDraft,
    *,
    language: str = CHINESE_OUTPUT_LANGUAGE,
) -> str:
    sections = list(
        zip(
            _render_text(language, "world_state_sections"),
            (
                draft.world_state.worldview_type,
                draft.world_state.time_anchor,
                draft.world_state.overall_atmosphere,
                draft.world_state.character_work_domain,
                draft.world_state.sustainable_storylines,
                draft.world_state.appearance_boundaries,
                draft.world_state.letter_scale,
                draft.world_state.image_principles,
            ),
        )
    )
    anchor = meta.work_title or str(_render_text(language, "public_anchor"))
    return _render_markdown_document(
        frontmatter=[
            ("version", "0.1"),
            ("anchor", anchor),
            ("mode", _profile_mode_label(meta.work_title, language=language)),
        ],
        title="world_state.md",
        blocks=[
            _render_numbered_section(number, title, items) for number, (title, items) in enumerate(sections, start=1)
        ],
    )


def _render_cast_relations_markdown(
    meta: CharacterProfileMeta,
    draft: CharacterProfileDraft,
    *,
    language: str = CHINESE_OUTPUT_LANGUAGE,
) -> str:
    blocks: list[str] = []
    for relationship in draft.cast_relations.focus_relationships:
        blocks.append(
            "\n".join(
                    [
                        f"## {relationship.name}",
                        "",
                        f"### {_render_text(language, 'relationship_tone')}",
                        "",
                        *_render_bullet_lines(relationship.relationship_tone),
                        "",
                        f"### {_render_text(language, 'writing_points')}",
                        "",
                        *_render_bullet_lines(relationship.writing_points),
                        "",
                        f"### {_render_text(language, 'forbidden')}",
                        "",
                        *_render_bullet_lines(relationship.forbidden_patterns),
                    ]
            )
        )
    return _render_markdown_document(
        frontmatter=[
            ("version", "0.1"),
            ("scope", str(_render_text(language, "cast_relations_scope")).format(character_name=meta.character_name)),
        ],
        title="cast_relations.md",
        blocks=blocks,
    )


def _render_cast_pool_markdown(
    meta: CharacterProfileMeta,
    draft: CharacterProfileDraft,
    *,
    language: str = CHINESE_OUTPUT_LANGUAGE,
) -> str:
    return _render_markdown_document(
        frontmatter=[
            ("version", "0.1"),
            ("scope", str(_render_text(language, "cast_pool_scope")).format(character_name=meta.character_name)),
        ],
        title="cast_pool.md",
        blocks=[
            _render_named_subsection(
                str(_render_text(language, "usage_principles")),
                [(str(_render_text(language, "principles")), draft.cast_pool.usage_principles)],
            ),
            _render_named_subsection(
                str(_render_text(language, "tier_1")),
                [(str(_render_text(language, "high_frequency_characters")), draft.cast_pool.tier_1_high_frequency)],
            ),
            _render_named_subsection(
                str(_render_text(language, "tier_2")),
                [(str(_render_text(language, "task_support_characters")), draft.cast_pool.tier_2_task_support)],
            ),
            _render_named_subsection(
                str(_render_text(language, "tier_3")),
                [(str(_render_text(language, "daily_life_characters")), draft.cast_pool.tier_3_daily_life)],
            ),
            _render_named_subsection(
                str(_render_text(language, "tier_4")),
                [
                    (
                        str(_render_text(language, "heavyweight_low_frequency_characters")),
                        draft.cast_pool.tier_4_heavyweight_low_frequency,
                    )
                ],
            ),
            _render_named_subsection(
                str(_render_text(language, "tier_5")),
                [(str(_render_text(language, "background_characters")), draft.cast_pool.tier_5_background_flavor)],
            ),
            _render_named_subsection(
                str(_render_text(language, "frequency_rules")),
                [(str(_render_text(language, "rules")), draft.cast_pool.frequency_rules)],
            ),
        ],
    )


def _render_translation_layer_markdown(
    meta: CharacterProfileMeta,
    draft: CharacterProfileDraft,
    *,
    language: str = CHINESE_OUTPUT_LANGUAGE,
) -> str:
    processing_mode_blocks = []
    for mode in draft.translation_layer.processing_modes:
        lines = [f"### {mode.name}", "", *_render_bullet_lines(mode.guidance)]
        if mode.examples:
            lines.extend(["", str(_render_text(language, "examples")), "", *_render_bullet_lines(mode.examples)])
        processing_mode_blocks.append("\n".join(lines))
    mapping_blocks = [
        "\n".join([f"### {group.category}", "", *_render_bullet_lines(group.mappings)])
        for group in draft.translation_layer.semantic_mappings
    ]
    reply_blocks = []
    for pattern in draft.translation_layer.replying_patterns:
        reply_blocks.append(
            "\n".join(
                [
                    f"### {pattern.scenario}",
                    "",
                    str(_render_text(language, "preferred")),
                    "",
                    *_render_bullet_lines(pattern.preferred),
                    "",
                    str(_render_text(language, "avoid")),
                    "",
                    *_render_bullet_lines(pattern.avoid),
                ]
            )
        )
    return _render_markdown_document(
        frontmatter=[
            ("version", "0.1"),
            ("scope", str(_render_text(language, "translation_scope")).format(character_name=meta.character_name)),
            ("purpose", str(_render_text(language, "translation_purpose"))),
        ],
        title="translation_layer.md",
        blocks=[
            _render_numbered_section(1, str(_render_text(language, "purpose")), draft.translation_layer.purpose),
            _render_numbered_section(
                2,
                str(_render_text(language, "general_principles")),
                draft.translation_layer.general_principles,
            ),
            "\n\n".join([f"## 3. {_render_text(language, 'processing_modes')}", *processing_mode_blocks]),
            "\n\n".join([f"## 4. {_render_text(language, 'semantic_mapping_table')}", *mapping_blocks]),
            _render_numbered_section(
                5,
                str(_render_text(language, "emotion_fidelity_rules")),
                draft.translation_layer.emotion_fidelity_rules,
            ),
            "\n\n".join([f"## 6. {_render_text(language, 'reply_patterns')}", *reply_blocks]),
            _render_numbered_section(
                7,
                str(_render_text(language, "explicit_forbidden")),
                draft.translation_layer.forbidden_patterns,
            ),
        ],
    )


def _render_relationship_markdown(
    character: CharacterProfileMeta,
    relationship: RelationshipProfileMeta,
    draft: RelationshipProfileDraft,
    *,
    language: str = CHINESE_OUTPUT_LANGUAGE,
) -> str:
    sections = list(
        zip(
            _render_text(language, "relationship_sections"),
            (
                draft.setup_statement,
                draft.relationship_definition,
                draft.shared_past,
                draft.relationship_tone,
                draft.attitude_toward_user,
                draft.how_it_affects_letters,
                draft.intimacy_threshold,
                draft.explicit_boundaries,
                draft.warming_up_path,
            ),
        )
    )
    return _render_markdown_document(
        frontmatter=[
            ("version", "0.1"),
            ("canon_status", str(_render_text(language, "canon_status_supplement"))),
            ("character", character.character_name),
            ("relationship", relationship.name),
        ],
        title="relationship.md",
        blocks=[
            _render_numbered_section(number, title, items) for number, (title, items) in enumerate(sections, start=1)
        ],
    )


def _render_character_prompt_bundle(
    meta: CharacterProfileMeta,
    draft: CharacterProfileDraft,
    *,
    language: str = CHINESE_OUTPUT_LANGUAGE,
) -> dict[str, str]:
    return {
        "persona.md": _render_persona_markdown(meta, draft, language=language),
        "world_state.md": _render_world_state_markdown(meta, draft, language=language),
        "cast_relations.md": _render_cast_relations_markdown(meta, draft, language=language),
        "cast_pool.md": _render_cast_pool_markdown(meta, draft, language=language),
        "translation_layer.md": _render_translation_layer_markdown(meta, draft, language=language),
    }


def _character_list_label(character: CharacterProfileMeta) -> str:
    return character_display_name(character)


def _session_label(character: CharacterProfileMeta, relationship: RelationshipProfileMeta) -> str:
    return f"{_character_list_label(character)} + {relationship.name}"


def _resolve_session_target(
    config_path: str,
    *,
    character_slug_value: str | None = None,
    relationship_slug_value: str | None = None,
) -> tuple[CharacterProfileMeta, RelationshipProfileMeta, SessionProfileMeta | None]:
    repo_root = _repo_root_from_config(config_path)
    character_input = (character_slug_value or "").strip()
    relationship_input = (relationship_slug_value or "").strip()
    if not character_input and not relationship_input:
        active = load_selected_profile_context(repo_root)
        if active is None:
            raise ValueError("No active profile selected.")
        return active.character, active.relationship, active.session
    if not character_input or not relationship_input:
        raise ValueError("Both --character and --relationship are required together.")
    character = resolve_character_profile_meta(repo_root, character_input)
    if character is None:
        raise ValueError(f"Character profile not found: {character_input}")
    relationship = resolve_relationship_profile_meta(repo_root, character.slug, relationship_input)
    if relationship is None:
        raise ValueError(
            f"Relationship profile not found for character '{character_display_name(character)}': {relationship_input}"
        )
    session = load_session_profile_meta(repo_root, character.slug, relationship.slug)
    return character, relationship, session


def _build_scoped_store(config_path: str, *, character_slug: str, relationship_slug: str) -> StateStore:
    settings = load_settings(config_path, require_active_profile=False)
    return StateStore(
        settings,
        scope=SessionScope(character_slug=character_slug, relationship_slug=relationship_slug),
    )


def _resolve_scoped_store(
    config_path: str,
    *,
    character_slug_value: str | None = None,
    relationship_slug_value: str | None = None,
) -> tuple[CharacterProfileMeta, RelationshipProfileMeta, SessionProfileMeta | None, StateStore]:
    character, relationship, session = _resolve_session_target(
        config_path,
        character_slug_value=character_slug_value,
        relationship_slug_value=relationship_slug_value,
    )
    store = _build_scoped_store(
        config_path,
        character_slug=character.slug,
        relationship_slug=relationship.slug,
    )
    return character, relationship, session, store


def _confirm_destructive_action(prompt: str, *, yes: bool) -> bool:
    if yes:
        return True
    response = input(f"{prompt} [y/N]: ").strip().lower()
    return response in {"y", "yes"}


def _clear_active_if_target_matches(repo_root: Path, *, character_slug: str, relationship_slug: str) -> bool:
    active = load_selected_profile_context(repo_root)
    if active is None:
        return False
    if active.character.slug != character_slug or active.relationship.slug != relationship_slug:
        return False
    return clear_active_profile_selection(repo_root)


def _print_session_summary(
    *,
    config_path: str,
    character: CharacterProfileMeta,
    relationship: RelationshipProfileMeta,
    session: SessionProfileMeta | None,
) -> None:
    if session is None:
        print("Pending inbound: unavailable (session missing)")
        print("Last run: unavailable (session missing)")
        print("Last outbound: unavailable (session missing)")
        return
    store = _build_scoped_store(
        config_path,
        character_slug=character.slug,
        relationship_slug=relationship.slug,
    )
    pending_inbound = store.count_unconsumed_inbound()
    recent_runs = store.list_recent_runs(limit=1)
    recent_outbound = store.list_recent_outbound_messages(limit=1)
    print(f"Pending inbound: {pending_inbound}")
    if recent_runs:
        run = recent_runs[0]
        print(f"Last run: {run.status} / {run.mode} / {run.started_at}")
    else:
        print("Last run: none")
    if recent_outbound:
        outbound = recent_outbound[0]
        print(f"Last outbound: {outbound.sent_at} / {outbound.subject}")
    else:
        print("Last outbound: none")


def _warn_before_switch(
    config_path: str,
    *,
    target_character_slug: str,
    target_relationship_slug: str,
) -> None:
    repo_root = _repo_root_from_config(config_path)
    current = load_selected_profile_context(repo_root)
    if current is None or current.session is None:
        return
    if (
        current.character.slug == target_character_slug
        and current.relationship.slug == target_relationship_slug
    ):
        return
    store = _build_scoped_store(
        config_path,
        character_slug=current.character.slug,
        relationship_slug=current.relationship.slug,
    )
    pending = store.count_unconsumed_inbound()
    if pending <= 0:
        return
    print(
        "Warning: current session "
        f"{_session_label(current.character, current.relationship)} has {pending} unconsumed inbound message(s)."
    )


def generate_character_profile(config_path: str, *, work_title: str | None, character_name: str) -> int:
    repo_root = _repo_root_from_config(config_path)
    ensure_profile_roots(repo_root)
    slug = slugify(character_name, fallback_prefix="character")
    profile_label = f"character/{slug}"
    normalized_work_title = _normalize_work_title(work_title)
    _log_profile_step(
        profile_label,
        1,
        5,
        "Preparing character profile generation.",
        character_name=character_name.strip(),
        work_title=normalized_work_title or "none",
    )
    if load_character_profile_meta(repo_root, slug) is not None:
        LOGGER.warning("[%s] Character profile already exists.", profile_label)
        print(f"Character profile already exists: {character_name.strip()}")
        return 1

    _log_profile_step(profile_label, 2, 5, "Loading settings and building profile generation client.")
    settings = load_settings(config_path, require_active_profile=False)
    client = build_profile_generation_client(settings)
    _log_profile_step(
        profile_label,
        2,
        5,
        "Profile generation client ready.",
        provider=settings.llm.stage2.provider,
        model=get_provider_text_model(settings, settings.llm.stage2.provider),
    )
    _log_profile_step(profile_label, 3, 5, "Requesting character profile draft from LLM.")
    draft = client.generate_character_profile(
        work_title=normalized_work_title or None,
        character_name=character_name,
    )
    _log_profile_step(profile_label, 3, 5, "Character profile draft received.")
    meta = CharacterProfileMeta(
        slug=slug,
        character_name=character_name.strip(),
        work_title=normalized_work_title,
        created_at=_created_at(),
    )
    _log_profile_step(profile_label, 4, 5, "Writing character profile files.")
    save_character_profile_meta(repo_root, meta)
    prompt_root = character_prompt_dir(repo_root, slug)
    for filename, content in _render_character_prompt_bundle(meta, draft, language=settings.llm.language).items():
        _write_text(prompt_root / filename, content)
    _log_profile_step(profile_label, 5, 5, "Character profile generation completed.", path=character_profile_dir(repo_root, slug))
    print(f"Generated character profile: {meta.character_name}")
    print(f"Path: {character_profile_dir(repo_root, slug)}")
    return 0


def generate_relationship_profile(
    config_path: str,
    *,
    character_slug_value: str,
    relationship_name: str | None,
    relationship_description: str,
) -> int:
    repo_root = _repo_root_from_config(config_path)
    ensure_profile_roots(repo_root)
    character_input = character_slug_value.strip()
    try:
        character = resolve_character_profile_meta(repo_root, character_input)
    except ValueError as exc:
        print(str(exc))
        return 1
    if character is None:
        LOGGER.warning("[relationship/%s] Character profile not found.", character_input)
        print(f"Character profile not found: {character_slug_value}")
        return 1
    display_name = _relationship_name_from_input(relationship_name, relationship_description)
    slug = slugify(display_name, fallback_prefix="relationship")
    profile_label = f"relationship/{character.slug}/{slug}"
    _log_profile_step(
        profile_label,
        1,
        6,
        "Preparing relationship profile generation.",
        character_slug=character.slug,
        relationship_name=display_name,
    )
    if load_relationship_profile_meta(repo_root, character.slug, slug) is not None:
        LOGGER.warning("[%s] Relationship profile already exists.", profile_label)
        print(f"Relationship profile already exists for {character_display_name(character)}: {display_name}")
        return 1

    _log_profile_step(profile_label, 2, 6, "Loading settings and building profile generation client.")
    settings = load_settings(config_path, require_active_profile=False)
    client = build_profile_generation_client(settings)
    _log_profile_step(
        profile_label,
        2,
        6,
        "Profile generation client ready.",
        provider=settings.llm.stage2.provider,
        model=get_provider_text_model(settings, settings.llm.stage2.provider),
    )
    _log_profile_step(profile_label, 3, 6, "Loading character prompt documents.")
    character_prompt_docs = _load_character_prompt_docs(repo_root, character.slug)
    _log_profile_step(profile_label, 3, 6, "Character prompt documents ready.", document_count=len(character_prompt_docs))
    _log_profile_step(profile_label, 4, 6, "Requesting relationship profile draft from LLM.")
    draft = client.generate_relationship_profile(
        character_name=character.character_name,
        character_prompt_docs=character_prompt_docs,
        relationship_name=display_name,
        relationship_description=relationship_description.strip(),
    )
    _log_profile_step(profile_label, 4, 6, "Relationship profile draft received.")
    meta = RelationshipProfileMeta(
        slug=slug,
        character_slug=character.slug,
        name=display_name,
        description=relationship_description.strip(),
        created_at=_created_at(),
    )
    _log_profile_step(profile_label, 5, 6, "Writing relationship profile files.")
    save_relationship_profile_meta(repo_root, meta)
    prompt_root = relationship_prompt_dir(repo_root, character.slug, slug)
    _write_text(
        prompt_root / "relationship.md",
        _render_relationship_markdown(character, meta, draft, language=settings.llm.language),
    )
    _log_profile_step(
        profile_label,
        6,
        6,
        "Relationship profile generation completed.",
        path=relationship_profile_dir(repo_root, character.slug, slug),
    )
    print(f"Generated relationship profile: {character.character_name} / {display_name}")
    print(f"Path: {relationship_profile_dir(repo_root, character.slug, slug)}")
    return 0


def list_profiles(config_path: str) -> int:
    repo_root = _repo_root_from_config(config_path)
    ensure_profile_roots(repo_root)
    active = load_selected_profile_context(repo_root)

    print("Characters:")
    for character in list_character_profile_metas(repo_root):
        print(f"  - {_character_list_label(character)}")
        relationships = list_relationship_profile_metas(repo_root, character.slug)
        if relationships:
            print("    relationships:")
            for relationship in relationships:
                print(f"      - {relationship.name}")

    if active is None:
        print("Active: none")
        return 0

    session_status = "session ready" if active.session is not None else "session missing"
    print(f"Active: {_character_list_label(active.character)} + {active.relationship.name} ({session_status})")
    return 0


def select_profile(config_path: str, *, character_slug_value: str, relationship_slug_value: str) -> int:
    repo_root = _repo_root_from_config(config_path)
    character_input = character_slug_value.strip()
    relationship_input = relationship_slug_value.strip()
    character = resolve_character_profile_meta(repo_root, character_input)
    if character is None:
        raise ValueError(f"Character profile not found: {character_input}")
    relationship = resolve_relationship_profile_meta(repo_root, character.slug, relationship_input)
    if relationship is None:
        raise ValueError(f"Relationship profile not found for character '{character_display_name(character)}': {relationship_input}")
    _warn_before_switch(
        config_path,
        target_character_slug=character.slug,
        target_relationship_slug=relationship.slug,
    )
    active = select_active_profile(
        repo_root,
        character_slug_value=character.slug,
        relationship_slug_value=relationship.slug,
    )
    settings = load_settings(config_path, require_active_profile=True)
    MemoryManager(settings).bootstrap()
    print(f"Selected active profile: {_character_list_label(active.character)} + {active.relationship.name}")
    print(f"Session path: {session_profile_dir(repo_root, active.character.slug, active.relationship.slug)}")
    return 0


def show_active_profile(config_path: str) -> int:
    repo_root = _repo_root_from_config(config_path)
    active = load_selected_profile_context(repo_root)
    if active is None:
        print("Active: none")
        return 1
    print(f"Character: {_character_list_label(active.character)}")
    print(f"Relationship: {active.relationship.name}")
    print(f"Session: {'ready' if active.session is not None else 'missing'}")
    print(f"Persona: {character_prompt_dir(repo_root, active.character.slug) / 'persona.md'}")
    print(f"Relationship doc: {relationship_prompt_dir(repo_root, active.character.slug, active.relationship.slug) / 'relationship.md'}")
    print(f"Session root: {session_profile_dir(repo_root, active.character.slug, active.relationship.slug)}")
    _print_session_summary(
        config_path=config_path,
        character=active.character,
        relationship=active.relationship,
        session=active.session,
    )
    return 0


def show_inbox(
    config_path: str,
    *,
    character_slug_value: str | None = None,
    relationship_slug_value: str | None = None,
    limit: int = 5,
) -> int:
    character, relationship, session, store = _resolve_scoped_store(
        config_path,
        character_slug_value=character_slug_value,
        relationship_slug_value=relationship_slug_value,
    )
    messages = store.list_unconsumed_inbound(limit=limit, include_quarantined=True)
    print(f"Session: {_session_label(character, relationship)} ({'ready' if session is not None else 'missing'})")
    print(f"Unconsumed inbound: {store.count_unconsumed_inbound()}")
    print(f"Quarantined inbound: {store.count_quarantined_inbound()}")
    if not messages:
        print("Messages: none")
        return 0
    print("Messages:")
    for message in messages:
        print(
            f"  - uid={message.imap_uid} received_at={message.received_at} "
            f"from={message.from_email} subject={message.subject}{_format_inbound_attempt_state(message)}"
        )
    return 0


def _format_inbound_attempt_state(message: StoredInboundMessage) -> str:
    if message.quarantined_at is None and message.attempt_count == 0:
        return ""
    state = f" attempts={message.attempt_count}"
    if message.quarantined_at is not None:
        state += f" QUARANTINED quarantined_at={message.quarantined_at}"
    if message.last_error:
        state += f" last_error={message.last_error}"
    return state


def show_outbox(
    config_path: str,
    *,
    character_slug_value: str | None = None,
    relationship_slug_value: str | None = None,
    limit: int = 5,
) -> int:
    character, relationship, session, store = _resolve_scoped_store(
        config_path,
        character_slug_value=character_slug_value,
        relationship_slug_value=relationship_slug_value,
    )
    outbound_messages = store.list_recent_outbound_messages(limit=limit)
    if not outbound_messages:
        print("Outbox: none")
        return 0
    for message in outbound_messages:
        print(f"message_id={message.message_id} sent_at={message.sent_at} subject={message.subject}")
    return 0


def delete_session(
    config_path: str,
    *,
    character_slug_value: str,
    relationship_slug_value: str,
    yes: bool = False,
) -> int:
    repo_root = _repo_root_from_config(config_path)
    character, relationship, _session, store = _resolve_scoped_store(
        config_path,
        character_slug_value=character_slug_value,
        relationship_slug_value=relationship_slug_value,
    )
    if not _confirm_destructive_action(
        f"Delete session {_session_label(character, relationship)} and all scoped state?",
        yes=yes,
    ):
        print("Cancelled.")
        return 1
    summary = store.delete_session_records()
    session_root = session_profile_dir(repo_root, character.slug, relationship.slug)
    session_root = resolve_path_within_roots(
        session_root,
        [sessions_root(repo_root, character.slug)],
    )
    if session_root.exists():
        shutil.rmtree(session_root)
    cleared_active = _clear_active_if_target_matches(
        repo_root,
        character_slug=character.slug,
        relationship_slug=relationship.slug,
    )
    print(f"Deleted session: {_session_label(character, relationship)}")
    print(
        "Removed database rows: "
        f"kv_state={summary.kv_entries_deleted} "
        f"inbound={summary.inbound_messages_deleted} "
        f"outbound={summary.outbound_messages_deleted} "
        f"runs={summary.runs_deleted} "
        f"assets={summary.assets_deleted}"
    )
    print(f"Removed session directory: {session_root}")
    if cleared_active:
        print("Active profile cleared.")
    return 0


def _delete_scoped_messages(
    config_path: str,
    *,
    character_slug_value: str,
    relationship_slug_value: str,
    target: object | None,
    delete_all: bool,
    yes: bool,
    kind: str,
    field_name: str,
    delete_one: Callable[[StateStore, object], bool],
    delete_all_fn: Callable[[StateStore], int],
) -> int:
    if (target is None) == (not delete_all):
        raise ValueError(f"Use exactly one of --{field_name.replace('_', '-')} or --all.")
    character, relationship, _session, store = _resolve_scoped_store(
        config_path,
        character_slug_value=character_slug_value,
        relationship_slug_value=relationship_slug_value,
    )
    target_label = f"all {kind} messages" if delete_all else f"{kind} {field_name}={target}"
    if not _confirm_destructive_action(
        f"Delete {target_label} for {_session_label(character, relationship)}?",
        yes=yes,
    ):
        print("Cancelled.")
        return 1
    if delete_all:
        deleted_count = delete_all_fn(store)
        print(f"Deleted {kind} message(s): {deleted_count}")
        print(f"Session: {_session_label(character, relationship)}")
        print("Scope: all")
        return 0
    deleted = delete_one(store, target)
    if not deleted:
        print(f"{kind.capitalize()} message not found in {_session_label(character, relationship)}: {field_name}={target}")
        return 1
    print(f"Deleted {kind} message(s): 1")
    print(f"Session: {_session_label(character, relationship)}")
    print(f"Target: {field_name}={target}")
    return 0


def delete_inbound(
    config_path: str,
    *,
    character_slug_value: str,
    relationship_slug_value: str,
    imap_uid: int | None = None,
    delete_all: bool = False,
    yes: bool = False,
) -> int:
    return _delete_scoped_messages(
        config_path,
        character_slug_value=character_slug_value,
        relationship_slug_value=relationship_slug_value,
        target=imap_uid,
        delete_all=delete_all,
        yes=yes,
        kind="inbound",
        field_name="uid",
        delete_one=lambda store, value: store.delete_inbound_message(value),
        delete_all_fn=lambda store: store.delete_all_inbound_messages(),
    )


def delete_outbound(
    config_path: str,
    *,
    character_slug_value: str,
    relationship_slug_value: str,
    message_id: str | None = None,
    delete_all: bool = False,
    yes: bool = False,
) -> int:
    return _delete_scoped_messages(
        config_path,
        character_slug_value=character_slug_value,
        relationship_slug_value=relationship_slug_value,
        target=message_id,
        delete_all=delete_all,
        yes=yes,
        kind="outbound",
        field_name="message_id",
        delete_one=lambda store, value: store.delete_outbound_message(value),
        delete_all_fn=lambda store: store.delete_all_outbound_messages(),
    )
