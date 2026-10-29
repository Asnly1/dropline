from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class ModelValidationError(ValueError):
    """Raised when model payloads do not match expected shapes."""


CANDIDATE_MEMORY_TYPES = {"user_profile", "relationship_memory", "active_arc", "user_episode", "discard"}
CANDIDATE_MEMORY_DURABILITIES = {"transient", "short", "stable"}
ACTIVE_ARC_ACTIONS = {"none", "keep", "update", "archive"}


def _expect_object(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelValidationError(f"Expected object field '{field_name}'")
    return value


def _expect_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ModelValidationError(f"Expected string field '{key}'")
    return value


def _normalize_literal_newlines(value: str) -> str:
    return value.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")


def _expect_list(payload: dict[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ModelValidationError(f"Expected list field '{key}'")
    return value


def _expect_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    return _expect_object(payload.get(key), key)


def _expect_string_list(payload: dict[str, Any], key: str) -> list[str]:
    items = _expect_list(payload, key)
    values: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ModelValidationError(f"Expected string items in field '{key}'")
        values.append(item)
    return values


@dataclass(slots=True)
class AttachmentMeta:
    filename: str
    content_type: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RawInboundMessage:
    imap_uid: int
    raw_bytes: bytes


@dataclass(slots=True)
class NormalizedInboundMessage:
    message_id: str
    imap_uid: int
    from_email: str
    subject: str
    received_at: str
    body_text: str
    body_html_present: bool
    attachments: list[AttachmentMeta]
    thread_message_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["from"] = payload.pop("from_email")
        return payload


@dataclass(slots=True)
class UserDigest:
    main_event: str
    state: str
    possible_needs: list[str]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "UserDigest":
        payload = _expect_object(payload, "user_digest")
        return cls(
            main_event=_expect_string(payload, "main_event"),
            state=_expect_string(payload, "state"),
            possible_needs=[str(item) for item in _expect_list(payload, "possible_needs")],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CandidateMemory:
    content: str
    type: str
    confidence: float
    durability: str
    evidence: str
    why_it_matters: str = ""
    source_date: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CandidateMemory":
        payload = _expect_object(payload, "candidate_memories[]")
        confidence = payload.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ModelValidationError("Candidate memory requires numeric confidence")
        if not 0 <= float(confidence) <= 1:
            raise ModelValidationError("Candidate memory confidence must be between 0 and 1")
        memory_type = _expect_string(payload, "type")
        if memory_type not in CANDIDATE_MEMORY_TYPES:
            raise ModelValidationError(f"Invalid candidate memory type '{memory_type}'")
        durability = _expect_string(payload, "durability")
        if durability not in CANDIDATE_MEMORY_DURABILITIES:
            raise ModelValidationError(f"Invalid candidate memory durability '{durability}'")
        return cls(
            content=_expect_string(payload, "content"),
            type=memory_type,
            confidence=float(confidence),
            durability=durability,
            evidence=_expect_string(payload, "evidence"),
            why_it_matters=str(payload.get("why_it_matters", "")),
            source_date=str(payload.get("source_date", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Stage1Result:
    user_digest: UserDigest
    candidate_memories: list[CandidateMemory]
    emotional_priority: str
    open_loops: list[str]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Stage1Result":
        payload = _expect_object(payload, "stage1")
        return cls(
            user_digest=UserDigest.from_dict(_expect_dict(payload, "user_digest")),
            candidate_memories=[CandidateMemory.from_dict(item) for item in _expect_list(payload, "candidate_memories")],
            emotional_priority=_expect_string(payload, "emotional_priority"),
            open_loops=[str(item) for item in _expect_list(payload, "open_loops")],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_digest": self.user_digest.to_dict(),
            "candidate_memories": [item.to_dict() for item in self.candidate_memories],
            "emotional_priority": self.emotional_priority,
            "open_loops": self.open_loops,
        }


@dataclass(slots=True)
class CharacterEpisode:
    summary: str
    continuity_value: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CharacterEpisode":
        payload = _expect_object(payload, "character_episode")
        return cls(
            summary=_expect_string(payload, "summary"),
            continuity_value=_expect_string(payload, "continuity_value"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ActiveArcUpdateSuggestion:
    action: str
    content: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ActiveArcUpdateSuggestion":
        payload = _expect_object(payload, "active_arc_update_suggestion")
        action = _expect_string(payload, "action")
        if action not in ACTIVE_ARC_ACTIONS:
            raise ModelValidationError(f"Invalid active arc action '{action}'")
        return cls(action=action, content=_expect_string(payload, "content"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SceneCard:
    visual_style: str
    location: str
    action: str
    key_object: str
    time: str
    mood: str
    camera_distance: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SceneCard":
        payload = _expect_object(payload, "scene_card")
        return cls(
            visual_style=_expect_string(payload, "visual_style"),
            location=_expect_string(payload, "location"),
            action=_expect_string(payload, "action"),
            key_object=_expect_string(payload, "key_object"),
            time=_expect_string(payload, "time"),
            mood=_expect_string(payload, "mood"),
            camera_distance=_expect_string(payload, "camera_distance"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Stage2Result:
    subject: str
    body: str
    character_episode: CharacterEpisode
    active_arc_update_suggestion: ActiveArcUpdateSuggestion
    scene_card: SceneCard | None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Stage2Result":
        payload = _expect_object(payload, "stage2")
        scene_card_payload = payload.get("scene_card")
        if scene_card_payload is None:
            scene_card = None
        else:
            scene_card = SceneCard.from_dict(scene_card_payload)
        return cls(
            subject=_expect_string(payload, "subject"),
            body=_normalize_literal_newlines(_expect_string(payload, "body")),
            character_episode=CharacterEpisode.from_dict(_expect_dict(payload, "character_episode")),
            active_arc_update_suggestion=ActiveArcUpdateSuggestion.from_dict(_expect_dict(payload, "active_arc_update_suggestion")),
            scene_card=scene_card,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "body": self.body,
            "character_episode": self.character_episode.to_dict(),
            "active_arc_update_suggestion": self.active_arc_update_suggestion.to_dict(),
            "scene_card": self.scene_card.to_dict() if self.scene_card is not None else None,
        }


@dataclass(slots=True)
class PersonaProfileDraft:
    identity_and_role: list[str]
    core_values: list[str]
    personality_traits: list[str]
    language_style: list[str]
    repeatable_life_details: list[str]
    ooc_boundaries: list[str]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PersonaProfileDraft":
        return cls(
            identity_and_role=_expect_string_list(payload, "identity_and_role"),
            core_values=_expect_string_list(payload, "core_values"),
            personality_traits=_expect_string_list(payload, "personality_traits"),
            language_style=_expect_string_list(payload, "language_style"),
            repeatable_life_details=_expect_string_list(payload, "repeatable_life_details"),
            ooc_boundaries=_expect_string_list(payload, "ooc_boundaries"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class WorldStateProfileDraft:
    worldview_type: list[str]
    time_anchor: list[str]
    overall_atmosphere: list[str]
    character_work_domain: list[str]
    sustainable_storylines: list[str]
    appearance_boundaries: list[str]
    letter_scale: list[str]
    image_principles: list[str]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorldStateProfileDraft":
        return cls(
            worldview_type=_expect_string_list(payload, "worldview_type"),
            time_anchor=_expect_string_list(payload, "time_anchor"),
            overall_atmosphere=_expect_string_list(payload, "overall_atmosphere"),
            character_work_domain=_expect_string_list(payload, "character_work_domain"),
            sustainable_storylines=_expect_string_list(payload, "sustainable_storylines"),
            appearance_boundaries=_expect_string_list(payload, "appearance_boundaries"),
            letter_scale=_expect_string_list(payload, "letter_scale"),
            image_principles=_expect_string_list(payload, "image_principles"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FocusRelationshipDraft:
    name: str
    relationship_tone: list[str]
    writing_points: list[str]
    forbidden_patterns: list[str]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FocusRelationshipDraft":
        return cls(
            name=_expect_string(payload, "name"),
            relationship_tone=_expect_string_list(payload, "relationship_tone"),
            writing_points=_expect_string_list(payload, "writing_points"),
            forbidden_patterns=_expect_string_list(payload, "forbidden_patterns"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CastRelationsProfileDraft:
    focus_relationships: list[FocusRelationshipDraft]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CastRelationsProfileDraft":
        return cls(
            focus_relationships=[
                FocusRelationshipDraft.from_dict(item) for item in _expect_list(payload, "focus_relationships")
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "focus_relationships": [item.to_dict() for item in self.focus_relationships],
        }


@dataclass(slots=True)
class CastPoolProfileDraft:
    usage_principles: list[str]
    tier_1_high_frequency: list[str]
    tier_2_task_support: list[str]
    tier_3_daily_life: list[str]
    tier_4_heavyweight_low_frequency: list[str]
    tier_5_background_flavor: list[str]
    frequency_rules: list[str]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CastPoolProfileDraft":
        return cls(
            usage_principles=_expect_string_list(payload, "usage_principles"),
            tier_1_high_frequency=_expect_string_list(payload, "tier_1_high_frequency"),
            tier_2_task_support=_expect_string_list(payload, "tier_2_task_support"),
            tier_3_daily_life=_expect_string_list(payload, "tier_3_daily_life"),
            tier_4_heavyweight_low_frequency=_expect_string_list(payload, "tier_4_heavyweight_low_frequency"),
            tier_5_background_flavor=_expect_string_list(payload, "tier_5_background_flavor"),
            frequency_rules=_expect_string_list(payload, "frequency_rules"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TranslationProcessingModeDraft:
    name: str
    guidance: list[str]
    examples: list[str]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TranslationProcessingModeDraft":
        return cls(
            name=_expect_string(payload, "name"),
            guidance=_expect_string_list(payload, "guidance"),
            examples=_expect_string_list(payload, "examples"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TranslationMappingGroupDraft:
    category: str
    mappings: list[str]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TranslationMappingGroupDraft":
        return cls(
            category=_expect_string(payload, "category"),
            mappings=_expect_string_list(payload, "mappings"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TranslationReplyPatternDraft:
    scenario: str
    preferred: list[str]
    avoid: list[str]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TranslationReplyPatternDraft":
        return cls(
            scenario=_expect_string(payload, "scenario"),
            preferred=_expect_string_list(payload, "preferred"),
            avoid=_expect_string_list(payload, "avoid"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TranslationLayerProfileDraft:
    purpose: list[str]
    general_principles: list[str]
    processing_modes: list[TranslationProcessingModeDraft]
    semantic_mappings: list[TranslationMappingGroupDraft]
    emotion_fidelity_rules: list[str]
    replying_patterns: list[TranslationReplyPatternDraft]
    forbidden_patterns: list[str]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TranslationLayerProfileDraft":
        return cls(
            purpose=_expect_string_list(payload, "purpose"),
            general_principles=_expect_string_list(payload, "general_principles"),
            processing_modes=[
                TranslationProcessingModeDraft.from_dict(item) for item in _expect_list(payload, "processing_modes")
            ],
            semantic_mappings=[
                TranslationMappingGroupDraft.from_dict(item) for item in _expect_list(payload, "semantic_mappings")
            ],
            emotion_fidelity_rules=_expect_string_list(payload, "emotion_fidelity_rules"),
            replying_patterns=[
                TranslationReplyPatternDraft.from_dict(item) for item in _expect_list(payload, "replying_patterns")
            ],
            forbidden_patterns=_expect_string_list(payload, "forbidden_patterns"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "general_principles": self.general_principles,
            "processing_modes": [item.to_dict() for item in self.processing_modes],
            "semantic_mappings": [item.to_dict() for item in self.semantic_mappings],
            "emotion_fidelity_rules": self.emotion_fidelity_rules,
            "replying_patterns": [item.to_dict() for item in self.replying_patterns],
            "forbidden_patterns": self.forbidden_patterns,
        }


@dataclass(slots=True)
class CharacterProfileDraft:
    persona: PersonaProfileDraft
    world_state: WorldStateProfileDraft
    cast_relations: CastRelationsProfileDraft
    cast_pool: CastPoolProfileDraft
    translation_layer: TranslationLayerProfileDraft

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CharacterProfileDraft":
        return cls(
            persona=PersonaProfileDraft.from_dict(_expect_dict(payload, "persona")),
            world_state=WorldStateProfileDraft.from_dict(_expect_dict(payload, "world_state")),
            cast_relations=CastRelationsProfileDraft.from_dict(_expect_dict(payload, "cast_relations")),
            cast_pool=CastPoolProfileDraft.from_dict(_expect_dict(payload, "cast_pool")),
            translation_layer=TranslationLayerProfileDraft.from_dict(_expect_dict(payload, "translation_layer")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "persona": self.persona.to_dict(),
            "world_state": self.world_state.to_dict(),
            "cast_relations": self.cast_relations.to_dict(),
            "cast_pool": self.cast_pool.to_dict(),
            "translation_layer": self.translation_layer.to_dict(),
        }


@dataclass(slots=True)
class RelationshipProfileDraft:
    setup_statement: list[str]
    relationship_definition: list[str]
    shared_past: list[str]
    relationship_tone: list[str]
    attitude_toward_user: list[str]
    how_it_affects_letters: list[str]
    intimacy_threshold: list[str]
    explicit_boundaries: list[str]
    warming_up_path: list[str]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RelationshipProfileDraft":
        return cls(
            setup_statement=_expect_string_list(payload, "setup_statement"),
            relationship_definition=_expect_string_list(payload, "relationship_definition"),
            shared_past=_expect_string_list(payload, "shared_past"),
            relationship_tone=_expect_string_list(payload, "relationship_tone"),
            attitude_toward_user=_expect_string_list(payload, "attitude_toward_user"),
            how_it_affects_letters=_expect_string_list(payload, "how_it_affects_letters"),
            intimacy_threshold=_expect_string_list(payload, "intimacy_threshold"),
            explicit_boundaries=_expect_string_list(payload, "explicit_boundaries"),
            warming_up_path=_expect_string_list(payload, "warming_up_path"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class StoredInboundMessage:
    imap_uid: int
    message_id: str
    received_at: str
    from_email: str
    subject: str
    raw_path: str
    normalized_text_path: str
    body_text: str
    character_slug: str
    relationship_slug: str
    consumed_batch_id: str | None = None
    thread_message_id: str | None = None
    attempt_count: int = 0
    last_error: str | None = None
    last_attempt_at: str | None = None
    quarantined_at: str | None = None


@dataclass(slots=True)
class StoredOutboundMessage:
    message_id: str
    sent_at: str
    to_email: str
    subject: str
    raw_path: str
    image_path: str | None
    run_id: str
    batch_id: str
    character_slug: str
    relationship_slug: str
    delivery_status: str


@dataclass(slots=True)
class StoredRun:
    run_id: str
    batch_id: str
    started_at: str
    finished_at: str | None
    mode: str
    status: str
    error_stage: str | None
    stage1_path: str | None
    stage2_path: str | None
    character_slug: str
    relationship_slug: str
    error_message: str | None = None


@dataclass(slots=True)
class StoredAsset:
    asset_id: str
    run_id: str
    image_path: str
    scene_card: dict[str, Any]
    prompt_text: str
    created_at: str
    character_slug: str
    relationship_slug: str


@dataclass(slots=True)
class ManagedMemoryEntry:
    key: str
    content: str
    memory_type: str
    confidence: float
    durability: str
    evidence: list[str]
    source_dates: list[str]
    source_message_ids: list[str]
    created_at: str
    updated_at: str
    why_it_matters: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ManagedMemoryEntry":
        return cls(
            key=_expect_string(payload, "key"),
            content=_expect_string(payload, "content"),
            memory_type=_expect_string(payload, "memory_type"),
            confidence=float(payload.get("confidence", 0.0)),
            durability=str(payload.get("durability", "")),
            evidence=[str(item) for item in payload.get("evidence", [])],
            source_dates=[str(item) for item in payload.get("source_dates", [])],
            source_message_ids=[str(item) for item in payload.get("source_message_ids", [])],
            created_at=str(payload.get("created_at", "")),
            updated_at=str(payload.get("updated_at", "")),
            why_it_matters=str(payload.get("why_it_matters", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ConflictNote:
    key: str
    memory_type: str
    existing_content: str
    new_content: str
    reason: str
    source_date: str
    source_message_ids: list[str]
    created_at: str
    occurrence_dates: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ConflictNote":
        source_date = _expect_string(payload, "source_date")
        occurrence_dates_raw = payload.get("occurrence_dates")
        if isinstance(occurrence_dates_raw, list) and occurrence_dates_raw:
            occurrence_dates = [str(item) for item in occurrence_dates_raw]
        else:
            occurrence_dates = [source_date] if source_date else []
        return cls(
            key=_expect_string(payload, "key"),
            memory_type=_expect_string(payload, "memory_type"),
            existing_content=_expect_string(payload, "existing_content"),
            new_content=_expect_string(payload, "new_content"),
            reason=_expect_string(payload, "reason"),
            source_date=source_date,
            source_message_ids=[str(item) for item in payload.get("source_message_ids", [])],
            created_at=_expect_string(payload, "created_at"),
            occurrence_dates=occurrence_dates,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EpisodeRecord:
    entry_id: str
    batch_id: str
    created_at: str
    source_message_ids: list[str]
    source_dates: list[str]
    payload: dict[str, Any]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EpisodeRecord":
        return cls(
            entry_id=_expect_string(payload, "entry_id"),
            batch_id=_expect_string(payload, "batch_id"),
            created_at=_expect_string(payload, "created_at"),
            source_message_ids=[str(item) for item in payload.get("source_message_ids", [])],
            source_dates=[str(item) for item in payload.get("source_dates", [])],
            payload=dict(payload.get("payload", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class GeneratedImage:
    prompt_text: str
    image_bytes: bytes
    file_extension: str = "png"


@dataclass(slots=True)
class RunOutcome:
    success: bool
    run_id: str
    mode: str
    batch_id: str
    error_stage: str | None = None
    message: str = ""
    outbound_message_id: str | None = None
    image_generated: bool = False
