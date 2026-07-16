from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import logging
import re
from pathlib import Path
from typing import Callable, Iterable, TypeVar

from .documents import load_json_file, write_json_file


LOGGER = logging.getLogger(__name__)

CHARACTERS_DIR = "characters"
RELATIONSHIPS_DIR = "relationships"
SESSIONS_DIR = "sessions"
PROMPT_DIR = "prompt"
MEMORY_DIR = "memory"
EPISODES_DIR = "episodes"
ARCHIVE_DIR = "archive"
ACTIVE_PROFILE_FILENAME = "active.json"
META_FILENAME = "meta.json"
CHARACTER_PROMPT_FILENAMES = (
    "persona.md",
    "world_state.md",
    "cast_relations.md",
    "cast_pool.md",
    "translation_layer.md",
)
RELATIONSHIP_PROMPT_FILENAMES = ("relationship.md",)
SLUG_SANITIZE_RE = re.compile(r"[^\w-]+", re.UNICODE)
MULTI_HYPHEN_RE = re.compile(r"-{2,}")
_MetaT = TypeVar("_MetaT")


def validate_profile_slug(value: str, field_name: str = "profile.slug") -> str:
    slug = value.strip()
    if (
        not slug
        or Path(slug).is_absolute()
        or "/" in slug
        or "\\" in slug
        or slug in {".", ".."}
    ):
        raise ValueError(f"Invalid profile slug for {field_name}: {value}")
    return slug


@dataclass(frozen=True, slots=True)
class CharacterProfileMeta:
    slug: str
    character_name: str
    work_title: str
    created_at: str

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "CharacterProfileMeta":
        return cls(
            slug=validate_profile_slug(str(payload.get("slug", "")).strip(), "character.slug"),
            character_name=str(payload.get("character_name", "")).strip(),
            work_title=str(payload.get("work_title", "")).strip(),
            created_at=str(payload.get("created_at", "")).strip(),
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RelationshipProfileMeta:
    slug: str
    character_slug: str
    name: str
    description: str
    created_at: str

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "RelationshipProfileMeta":
        return cls(
            slug=validate_profile_slug(str(payload.get("slug", "")).strip(), "relationship.slug"),
            character_slug=validate_profile_slug(
                str(payload.get("character_slug", "")).strip(),
                "relationship.character_slug",
            ),
            name=str(payload.get("name", "")).strip(),
            description=str(payload.get("description", "")).strip(),
            created_at=str(payload.get("created_at", "")).strip(),
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SessionProfileMeta:
    slug: str
    character_slug: str
    relationship_slug: str
    character_name: str
    relationship_name: str
    created_at: str

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "SessionProfileMeta":
        return cls(
            slug=validate_profile_slug(str(payload.get("slug", "")).strip(), "session.slug"),
            character_slug=validate_profile_slug(str(payload.get("character_slug", "")).strip(), "session.character_slug"),
            relationship_slug=validate_profile_slug(
                str(payload.get("relationship_slug", "")).strip(),
                "session.relationship_slug",
            ),
            character_name=str(payload.get("character_name", "")).strip(),
            relationship_name=str(payload.get("relationship_name", "")).strip(),
            created_at=str(payload.get("created_at", "")).strip(),
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ActiveProfileSelection:
    character_slug: str
    relationship_slug: str

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "ActiveProfileSelection":
        return cls(
            character_slug=validate_profile_slug(str(payload.get("character_slug", "")).strip(), "active.character_slug"),
            relationship_slug=validate_profile_slug(
                str(payload.get("relationship_slug", "")).strip(),
                "active.relationship_slug",
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ActiveProfileContext:
    selection: ActiveProfileSelection
    character: CharacterProfileMeta
    relationship: RelationshipProfileMeta
    session: SessionProfileMeta


@dataclass(frozen=True, slots=True)
class SelectedProfileContext:
    selection: ActiveProfileSelection
    character: CharacterProfileMeta
    relationship: RelationshipProfileMeta
    session: SessionProfileMeta | None


def profiles_root(repo_root: Path) -> Path:
    return repo_root / "profiles"


def docs_root(repo_root: Path) -> Path:
    return repo_root / "docs"


def active_profile_path(repo_root: Path) -> Path:
    return profiles_root(repo_root) / ACTIVE_PROFILE_FILENAME


def characters_root(repo_root: Path) -> Path:
    return profiles_root(repo_root) / CHARACTERS_DIR


def character_profile_dir(repo_root: Path, character_slug: str) -> Path:
    character_slug = validate_profile_slug(character_slug, "character_slug")
    return characters_root(repo_root) / character_slug


def character_prompt_dir(repo_root: Path, character_slug: str) -> Path:
    return character_profile_dir(repo_root, character_slug) / PROMPT_DIR


def relationships_root(repo_root: Path, character_slug: str) -> Path:
    character_slug = validate_profile_slug(character_slug, "character_slug")
    return character_profile_dir(repo_root, character_slug) / RELATIONSHIPS_DIR


def relationship_profile_dir(repo_root: Path, character_slug: str, relationship_slug: str) -> Path:
    relationship_slug = validate_profile_slug(relationship_slug, "relationship_slug")
    return relationships_root(repo_root, character_slug) / relationship_slug


def relationship_prompt_dir(repo_root: Path, character_slug: str, relationship_slug: str) -> Path:
    return relationship_profile_dir(repo_root, character_slug, relationship_slug) / PROMPT_DIR


def sessions_root(repo_root: Path, character_slug: str) -> Path:
    character_slug = validate_profile_slug(character_slug, "character_slug")
    return character_profile_dir(repo_root, character_slug) / SESSIONS_DIR


def session_profile_dir(repo_root: Path, character_slug: str, relationship_slug: str) -> Path:
    relationship_slug = validate_profile_slug(relationship_slug, "relationship_slug")
    return sessions_root(repo_root, character_slug) / relationship_slug


def session_archive_dir(repo_root: Path, character_slug: str, relationship_slug: str) -> Path:
    return session_profile_dir(repo_root, character_slug, relationship_slug) / ARCHIVE_DIR


def character_display_name(character: CharacterProfileMeta) -> str:
    if character.work_title:
        return f"{character.character_name} / {character.work_title}"
    return character.character_name


def list_session_root_dirs(repo_root: Path) -> list[Path]:
    roots: list[Path] = []
    for character_meta in list_character_profile_metas(repo_root):
        root = sessions_root(repo_root, character_meta.slug)
        if root.exists():
            roots.append(root)
    return roots


def _missing_prompt_files(prompt_root: Path, filenames: Iterable[str]) -> list[str]:
    return [name for name in filenames if not (prompt_root / name).exists()]


def _assert_character_prompt_bundle(repo_root: Path, character_slug: str) -> None:
    prompt_root = character_prompt_dir(repo_root, character_slug)
    missing = _missing_prompt_files(prompt_root, CHARACTER_PROMPT_FILENAMES)
    if missing:
        raise ValueError(f"Character profile '{character_slug}' is missing prompt files: {', '.join(missing)}")


def _assert_relationship_prompt_bundle(repo_root: Path, character_slug: str, relationship_slug: str) -> None:
    prompt_root = relationship_prompt_dir(repo_root, character_slug, relationship_slug)
    missing = _missing_prompt_files(prompt_root, RELATIONSHIP_PROMPT_FILENAMES)
    if missing:
        raise ValueError(
            f"Relationship profile '{relationship_slug}' for character '{character_slug}' is missing prompt file: {', '.join(missing)}"
        )


def slugify(value: str, *, fallback_prefix: str = "profile") -> str:
    normalized = value.strip().lower().replace("_", "-")
    normalized = re.sub(r"\s+", "-", normalized)
    normalized = SLUG_SANITIZE_RE.sub("-", normalized)
    normalized = MULTI_HYPHEN_RE.sub("-", normalized).strip("-")
    if normalized:
        return normalized
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    return f"{fallback_prefix}-{digest}"


def _normalize_lookup_key(value: str) -> str:
    return " ".join(value.split()).strip().casefold()


def _relationship_disambiguation_label(relationship: RelationshipProfileMeta) -> str:
    collapsed_description = " ".join(relationship.description.split())
    if not collapsed_description:
        return relationship.name
    if len(collapsed_description) > 32:
        collapsed_description = collapsed_description[:32].rstrip() + "..."
    return f"{relationship.name} ({collapsed_description})"


def ensure_profile_roots(repo_root: Path) -> None:
    for path in (profiles_root(repo_root), characters_root(repo_root)):
        path.mkdir(parents=True, exist_ok=True)


def _write_meta(path: Path, payload: dict[str, object]) -> Path:
    write_json_file(path, payload)
    return path


def save_character_profile_meta(repo_root: Path, meta: CharacterProfileMeta) -> Path:
    ensure_profile_roots(repo_root)
    return _write_meta(character_profile_dir(repo_root, meta.slug) / META_FILENAME, meta.to_dict())


def save_relationship_profile_meta(repo_root: Path, meta: RelationshipProfileMeta) -> Path:
    ensure_profile_roots(repo_root)
    return _write_meta(relationship_profile_dir(repo_root, meta.character_slug, meta.slug) / META_FILENAME, meta.to_dict())


def save_session_profile_meta(repo_root: Path, meta: SessionProfileMeta) -> Path:
    ensure_profile_roots(repo_root)
    return _write_meta(session_profile_dir(repo_root, meta.character_slug, meta.relationship_slug) / META_FILENAME, meta.to_dict())


def _load_meta(
    path: Path,
    *,
    parse: Callable[[dict[str, object]], _MetaT],
) -> _MetaT | None:
    payload = load_json_file(path, default=None)
    if not isinstance(payload, dict):
        return None
    try:
        return parse(payload)
    except ValueError as exc:
        LOGGER.warning("Skipping invalid profile meta at %s: %s", path, exc)
        return None


def _list_metas(
    meta_paths: Iterable[Path],
    *,
    parse: Callable[[dict[str, object]], _MetaT],
) -> list[_MetaT]:
    metas: list[_MetaT] = []
    for meta_path in meta_paths:
        meta = _load_meta(meta_path, parse=parse)
        if meta is not None:
            metas.append(meta)
    return metas


def load_character_profile_meta(repo_root: Path, character_slug: str) -> CharacterProfileMeta | None:
    return _load_meta(
        character_profile_dir(repo_root, character_slug) / META_FILENAME,
        parse=CharacterProfileMeta.from_dict,
    )


def load_relationship_profile_meta(
    repo_root: Path,
    character_slug: str,
    relationship_slug: str,
) -> RelationshipProfileMeta | None:
    return _load_meta(
        relationship_profile_dir(repo_root, character_slug, relationship_slug) / META_FILENAME,
        parse=RelationshipProfileMeta.from_dict,
    )


def load_session_profile_meta(repo_root: Path, character_slug: str, relationship_slug: str) -> SessionProfileMeta | None:
    return _load_meta(
        session_profile_dir(repo_root, character_slug, relationship_slug) / META_FILENAME,
        parse=SessionProfileMeta.from_dict,
    )


def list_character_profile_metas(repo_root: Path) -> list[CharacterProfileMeta]:
    return _list_metas(
        sorted(characters_root(repo_root).glob(f"*/{META_FILENAME}")),
        parse=CharacterProfileMeta.from_dict,
    )


def list_relationship_profile_metas(repo_root: Path, character_slug: str | None = None) -> list[RelationshipProfileMeta]:
    if character_slug is not None:
        meta_paths = sorted(relationships_root(repo_root, character_slug).glob(f"*/{META_FILENAME}"))
    else:
        meta_paths = sorted(characters_root(repo_root).glob(f"*/{RELATIONSHIPS_DIR}/*/{META_FILENAME}"))
    return _list_metas(
        meta_paths,
        parse=RelationshipProfileMeta.from_dict,
    )


def resolve_character_profile_meta(repo_root: Path, value: str) -> CharacterProfileMeta | None:
    normalized = _normalize_lookup_key(value)
    if not normalized:
        return None
    characters = list_character_profile_metas(repo_root)

    slug_matches = [character for character in characters if _normalize_lookup_key(character.slug) == normalized]
    if slug_matches:
        return slug_matches[0]

    display_matches = [
        character for character in characters if _normalize_lookup_key(character_display_name(character)) == normalized
    ]
    if len(display_matches) == 1:
        return display_matches[0]
    if len(display_matches) > 1:
        raise ValueError(
            "Character name is ambiguous. Matching profiles: "
            + ", ".join(sorted(character_display_name(character) for character in display_matches))
        )

    name_matches = [character for character in characters if _normalize_lookup_key(character.character_name) == normalized]
    if len(name_matches) == 1:
        return name_matches[0]
    if len(name_matches) > 1:
        raise ValueError(
            "Character name is ambiguous. Matching profiles: "
            + ", ".join(sorted(character_display_name(character) for character in name_matches))
        )
    return None


def resolve_relationship_profile_meta(
    repo_root: Path,
    character_slug: str,
    value: str,
) -> RelationshipProfileMeta | None:
    normalized = _normalize_lookup_key(value)
    if not normalized:
        return None
    relationships = list_relationship_profile_metas(repo_root, character_slug)

    slug_matches = [relationship for relationship in relationships if _normalize_lookup_key(relationship.slug) == normalized]
    if slug_matches:
        return slug_matches[0]

    name_matches = [relationship for relationship in relationships if _normalize_lookup_key(relationship.name) == normalized]
    if len(name_matches) == 1:
        return name_matches[0]
    if len(name_matches) > 1:
        raise ValueError(
            "Relationship name is ambiguous. Matching relationships: "
            + ", ".join(sorted(_relationship_disambiguation_label(relationship) for relationship in name_matches))
        )
    return None


def create_session_profile(
    repo_root: Path,
    *,
    character: CharacterProfileMeta,
    relationship: RelationshipProfileMeta,
    created_at: str | None = None,
) -> SessionProfileMeta:
    ensure_profile_roots(repo_root)
    existing = load_session_profile_meta(repo_root, character.slug, relationship.slug)
    if existing is not None:
        return existing
    timestamp = created_at or datetime.now(UTC).isoformat()
    meta = SessionProfileMeta(
        slug=relationship.slug,
        character_slug=character.slug,
        relationship_slug=relationship.slug,
        character_name=character.character_name,
        relationship_name=relationship.name,
        created_at=timestamp,
    )
    root = session_profile_dir(repo_root, character.slug, relationship.slug)
    (root / MEMORY_DIR).mkdir(parents=True, exist_ok=True)
    (root / EPISODES_DIR / "user").mkdir(parents=True, exist_ok=True)
    (root / EPISODES_DIR / "character").mkdir(parents=True, exist_ok=True)
    (root / ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)
    save_session_profile_meta(repo_root, meta)
    return meta


def write_active_profile_selection(repo_root: Path, selection: ActiveProfileSelection) -> Path:
    ensure_profile_roots(repo_root)
    path = active_profile_path(repo_root)
    write_json_file(path, selection.to_dict())
    return path


def clear_active_profile_selection(repo_root: Path) -> bool:
    path = active_profile_path(repo_root)
    if not path.exists():
        return False
    path.unlink()
    return True


def load_selected_profile_context(repo_root: Path) -> SelectedProfileContext | None:
    payload = load_json_file(active_profile_path(repo_root), default=None)
    if not isinstance(payload, dict):
        return None
    selection = ActiveProfileSelection.from_dict(payload)
    if not selection.character_slug or not selection.relationship_slug:
        return None
    character = load_character_profile_meta(repo_root, selection.character_slug)
    if character is None:
        raise ValueError(f"Active character profile not found: {selection.character_slug}")
    relationship = load_relationship_profile_meta(repo_root, selection.character_slug, selection.relationship_slug)
    if relationship is None:
        raise ValueError(
            f"Active relationship profile not found for '{selection.character_slug}': {selection.relationship_slug}"
        )
    session = load_session_profile_meta(repo_root, selection.character_slug, selection.relationship_slug)
    return SelectedProfileContext(
        selection=selection,
        character=character,
        relationship=relationship,
        session=session,
    )


def select_active_profile(
    repo_root: Path,
    *,
    character_slug_value: str,
    relationship_slug_value: str,
) -> ActiveProfileContext:
    character = load_character_profile_meta(repo_root, character_slug_value)
    if character is None:
        raise ValueError(f"Character profile not found: {character_slug_value}")
    _assert_character_prompt_bundle(repo_root, character.slug)
    relationship = load_relationship_profile_meta(repo_root, character.slug, relationship_slug_value)
    if relationship is None:
        raise ValueError(
            f"Relationship profile not found for character '{character.slug}': {relationship_slug_value}"
        )
    _assert_relationship_prompt_bundle(repo_root, character.slug, relationship.slug)
    session = create_session_profile(repo_root, character=character, relationship=relationship)
    selection = ActiveProfileSelection(
        character_slug=character.slug,
        relationship_slug=relationship.slug,
    )
    write_active_profile_selection(repo_root, selection)
    return ActiveProfileContext(selection=selection, character=character, relationship=relationship, session=session)
