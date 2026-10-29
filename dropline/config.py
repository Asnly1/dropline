from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .provider_registry import (
    IMAGE_CAPABILITY,
    PROVIDER_SPECS,
    TEXT_CAPABILITY,
    ensure_provider_support,
    get_provider_spec,
)
from .profile_manager import (
    ActiveProfileContext,
    character_prompt_dir,
    docs_root,
    load_selected_profile_context,
    profiles_root,
    relationship_prompt_dir,
    session_profile_dir,
)


PLACEHOLDER_VALUE_PATTERN = re.compile(r"^(?:REPLACE_ME(?:_[A-Z0-9_]+)?|YOUR_[A-Z0-9_]+|<[^>]+>)$", re.IGNORECASE)
HHMM_PATTERN = re.compile(r"^\d{2}:\d{2}$")
SUPPORTED_LLM_LANGUAGES = ("简体中文", "English")
SUPPORTED_SMTP_SECURITY = ("ssl", "starttls", "none")
IMAGE_ON_FAILURE_ABORT = "abort"
IMAGE_ON_FAILURE_TEXT_ONLY = "text_only"
SUPPORTED_IMAGE_ON_FAILURE = (IMAGE_ON_FAILURE_ABORT, IMAGE_ON_FAILURE_TEXT_ONLY)
DEFAULT_IMAGE_ON_FAILURE = IMAGE_ON_FAILURE_ABORT
DEFAULT_RETRY_AFTER_MINUTES = 120
DEFAULT_MAX_RUNS_PER_DAY = 3
DEFAULT_MAX_BATCH_ATTEMPTS = 3
DEFAULT_ALERT_AFTER_FAILURES = 2


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    name: str
    timezone: str


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    enabled: bool
    window_start: str
    window_end: str
    daily_target_pick_time: str
    check_every_minutes: int
    retry_after_minutes: int = DEFAULT_RETRY_AFTER_MINUTES
    max_runs_per_day: int = DEFAULT_MAX_RUNS_PER_DAY
    max_batch_attempts: int = DEFAULT_MAX_BATCH_ATTEMPTS


@dataclass(frozen=True, slots=True)
class MailEndpointConfig:
    host: str
    port: int
    ssl: bool
    username: str
    password: str
    folder: str | None = None


@dataclass(frozen=True, slots=True)
class SMTPEndpointConfig:
    host: str
    port: int
    security: str
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class CharacterMailboxConfig:
    address: str
    imap: MailEndpointConfig
    smtp: SMTPEndpointConfig


@dataclass(frozen=True, slots=True)
class MailAlertsConfig:
    enabled: bool
    recipient: str
    after_failures: int


@dataclass(frozen=True, slots=True)
class MailConfig:
    allowlist_senders: tuple[str, ...]
    primary_user_recipient: str
    character_mailbox: CharacterMailboxConfig
    alerts: MailAlertsConfig


@dataclass(frozen=True, slots=True)
class StorageConfig:
    sqlite_path: Path


@dataclass(frozen=True, slots=True)
class LLMStageConfig:
    provider: str


@dataclass(frozen=True, slots=True)
class LLMImageConfig:
    enabled: bool
    provider: str
    on_failure: str = DEFAULT_IMAGE_ON_FAILURE


@dataclass(frozen=True, slots=True)
class LLMConfig:
    language: str
    stage1: LLMStageConfig
    stage2: LLMStageConfig
    image: LLMImageConfig


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    api_key: str
    text_model: str
    image_model: str | None = None


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str


@dataclass(frozen=True, slots=True)
class DocumentPaths:
    persona: Path
    world_state: Path
    cast_relations: Path
    cast_pool: Path
    relationship: Path
    translation_layer: Path
    memory_promotion_rules: Path
    user_profile: Path
    relationship_memory: Path
    active_arc: Path
    conflict_notes: Path
    user_episode_dir: Path
    character_episode_dir: Path
    session_root: Path
    session_archive_dir: Path


@dataclass(frozen=True, slots=True)
class Settings:
    repo_root: Path
    project: ProjectConfig
    schedule: ScheduleConfig
    mail: MailConfig
    storage: StorageConfig
    llm: LLMConfig
    providers: dict[str, ProviderSettings]
    logging: LoggingConfig
    documents: DocumentPaths
    active_profile: ActiveProfileContext | None


def current_character_name(settings: Settings) -> str:
    if settings.active_profile is not None:
        return settings.active_profile.character.character_name
    return settings.mail.character_mailbox.address.split("@", 1)[0]


def ensure_active_profile(settings: Settings) -> ActiveProfileContext:
    if settings.active_profile is None:
        raise ValueError("No active profile selected. Use `python3 -m dropline select ...` first.")
    return settings.active_profile


def _resolve_path(base_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def _load_config_payload(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError("Config file must contain a YAML mapping.")
    return payload


def _resolve_repo_root(config_path: Path, project_payload: dict[str, Any]) -> Path:
    repo_root_value = project_payload.get("repo_root")
    if repo_root_value is None or str(repo_root_value).strip() == "":
        return config_path.parent
    if not isinstance(repo_root_value, str):
        raise ValueError("Missing string config for 'project.repo_root'")
    repo_root = Path(repo_root_value).expanduser()
    if not repo_root.is_absolute():
        raise ValueError("project.repo_root must be an absolute path")
    return repo_root.resolve()


def resolve_repo_root_from_config(config_path: str | Path = "config.yaml") -> Path:
    config_path = Path(config_path).resolve()
    payload = _load_config_payload(config_path)
    project_payload = _require_mapping(payload, "project")
    return _resolve_repo_root(config_path, project_payload)


def _require_mapping(payload: dict[str, Any], key: str, path: str | None = None) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        target = path or key
        raise ValueError(f"Missing mapping config for '{target}'")
    return value


def _require_string(payload: dict[str, Any], key: str, path: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing string config for '{path}'")
    return value.strip()


def _is_placeholder_value(value: str) -> bool:
    return bool(PLACEHOLDER_VALUE_PATTERN.fullmatch(value.strip()))


def _require_bool(payload: dict[str, Any], key: str, path: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"Missing boolean config for '{path}'")
    return value


def _require_int(payload: dict[str, Any], key: str, path: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Missing integer config for '{path}'")
    return value


def _require_positive_int(payload: dict[str, Any], key: str, path: str) -> int:
    value = _require_int(payload, key, path)
    if value <= 0:
        raise ValueError(f"Invalid integer config for '{path}': expected > 0")
    return value


def _require_string_list(payload: dict[str, Any], key: str, path: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"Missing list config for '{path}'")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"Invalid string list item for '{path}[{index}]'")
        items.append(item.strip())
    return items


def _optional_string(payload: dict[str, Any], key: str, path: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid string config for '{path}'")
    return value.strip()


def _optional_bool(payload: dict[str, Any], key: str, path: str, *, default: bool) -> bool:
    if payload.get(key) is None:
        return default
    return _require_bool(payload, key, path)


def _optional_positive_int(payload: dict[str, Any], key: str, path: str, *, default: int) -> int:
    if payload.get(key) is None:
        return default
    return _require_positive_int(payload, key, path)


def _require_image_on_failure(payload: dict[str, Any], key: str, path: str) -> str:
    value = (_optional_string(payload, key, path) or DEFAULT_IMAGE_ON_FAILURE).lower()
    if value not in SUPPORTED_IMAGE_ON_FAILURE:
        supported = ", ".join(repr(item) for item in SUPPORTED_IMAGE_ON_FAILURE)
        raise ValueError(f"Invalid string config for '{path}': expected one of {supported}")
    return value


def _require_supported_llm_language(payload: dict[str, Any], key: str, path: str) -> str:
    value = _require_string(payload, key, path)
    if value not in SUPPORTED_LLM_LANGUAGES:
        supported = ", ".join(repr(item) for item in SUPPORTED_LLM_LANGUAGES)
        raise ValueError(f"Invalid string config for '{path}': expected one of {supported}")
    return value


def _require_smtp_security(payload: dict[str, Any], key: str, path: str) -> str:
    value = _require_string(payload, key, path).lower()
    if value not in SUPPORTED_SMTP_SECURITY:
        supported = ", ".join(repr(item) for item in SUPPORTED_SMTP_SECURITY)
        raise ValueError(f"Invalid string config for '{path}': expected one of {supported}")
    return value


def _require_timezone(payload: dict[str, Any], key: str, path: str) -> str:
    value = _require_string(payload, key, path)
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Invalid timezone config for '{path}'") from exc
    return value


def _require_hhmm(payload: dict[str, Any], key: str, path: str) -> str:
    value = _require_string(payload, key, path)
    if not HHMM_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid time config for '{path}': expected HH:MM")
    hour_str, minute_str = value.split(":")
    hour = int(hour_str)
    minute = int(minute_str)
    if hour > 23 or minute > 59:
        raise ValueError(f"Invalid time config for '{path}': expected HH:MM")
    return value


def _load_schedule_config(payload: dict[str, Any]) -> ScheduleConfig:
    window_start = _require_hhmm(payload, "window_start", "schedule.window_start")
    window_end = _require_hhmm(payload, "window_end", "schedule.window_end")
    return ScheduleConfig(
        enabled=_require_bool(payload, "enabled", "schedule.enabled"),
        window_start=window_start,
        window_end=window_end,
        daily_target_pick_time=_require_hhmm(
            payload,
            "daily_target_pick_time",
            "schedule.daily_target_pick_time",
        ),
        check_every_minutes=_require_positive_int(
            payload,
            "check_every_minutes",
            "schedule.check_every_minutes",
        ),
        retry_after_minutes=_optional_positive_int(
            payload,
            "retry_after_minutes",
            "schedule.retry_after_minutes",
            default=DEFAULT_RETRY_AFTER_MINUTES,
        ),
        max_runs_per_day=_optional_positive_int(
            payload,
            "max_runs_per_day",
            "schedule.max_runs_per_day",
            default=DEFAULT_MAX_RUNS_PER_DAY,
        ),
        max_batch_attempts=_optional_positive_int(
            payload,
            "max_batch_attempts",
            "schedule.max_batch_attempts",
            default=DEFAULT_MAX_BATCH_ATTEMPTS,
        ),
    )


def _load_stage_config(payload: dict[str, Any], path: str) -> LLMStageConfig:
    return LLMStageConfig(
        provider=_require_string(payload, "provider", f"{path}.provider").lower(),
    )


def _load_image_config(payload: dict[str, Any], path: str) -> LLMImageConfig:
    enabled = _require_bool(payload, "enabled", f"{path}.enabled")
    if enabled:
        provider = _require_string(payload, "provider", f"{path}.provider").lower()
    else:
        provider = (_optional_string(payload, "provider", f"{path}.provider") or "").lower()
    return LLMImageConfig(
        enabled=enabled,
        provider=provider,
        on_failure=_require_image_on_failure(payload, "on_failure", f"{path}.on_failure"),
    )


def _load_provider_settings(
    payload: dict[str, Any],
    path: str,
    *,
    require_image_model: bool = False,
) -> ProviderSettings:
    return ProviderSettings(
        api_key=_require_string(payload, "api_key", f"{path}.api_key"),
        text_model=_require_string(payload, "text_model", f"{path}.text_model"),
        image_model=(
            _require_string(payload, "image_model", f"{path}.image_model")
            if require_image_model
            else _optional_string(payload, "image_model", f"{path}.image_model")
        ),
    )


def _load_mail_endpoint(payload: dict[str, Any], path: str, *, require_folder: bool) -> MailEndpointConfig:
    folder = _require_string(payload, "folder", f"{path}.folder") if require_folder else _optional_string(
        payload,
        "folder",
        f"{path}.folder",
    )
    return MailEndpointConfig(
        host=_require_string(payload, "host", f"{path}.host"),
        port=_require_int(payload, "port", f"{path}.port"),
        ssl=_require_bool(payload, "ssl", f"{path}.ssl"),
        username=_require_string(payload, "username", f"{path}.username"),
        password=_require_string(payload, "password", f"{path}.password"),
        folder=folder,
    )


def _load_smtp_endpoint(payload: dict[str, Any], path: str) -> SMTPEndpointConfig:
    return SMTPEndpointConfig(
        host=_require_string(payload, "host", f"{path}.host"),
        port=_require_int(payload, "port", f"{path}.port"),
        security=_require_smtp_security(payload, "security", f"{path}.security"),
        username=_require_string(payload, "username", f"{path}.username"),
        password=_require_string(payload, "password", f"{path}.password"),
    )


def _load_mail_alerts_config(payload: dict[str, Any], *, default_recipient: str) -> MailAlertsConfig:
    alerts_payload: dict[str, Any]
    if payload.get("alerts") is None:
        alerts_payload = {}
    else:
        alerts_payload = _require_mapping(payload, "alerts", "mail.alerts")
    return MailAlertsConfig(
        enabled=_optional_bool(alerts_payload, "enabled", "mail.alerts.enabled", default=True),
        recipient=_optional_string(alerts_payload, "recipient", "mail.alerts.recipient") or default_recipient,
        after_failures=_optional_positive_int(
            alerts_payload,
            "after_failures",
            "mail.alerts.after_failures",
            default=DEFAULT_ALERT_AFTER_FAILURES,
        ),
    )


def _build_document_paths(repo_root: Path, active_profile: ActiveProfileContext | None) -> DocumentPaths:
    docs = docs_root(repo_root)
    if active_profile is None:
        inactive_root = profiles_root(repo_root) / "_inactive"
        prompt_root = inactive_root / "prompt"
        relationship_root = inactive_root / "relationships" / "default" / "prompt"
        memory_root = inactive_root / "memory"
        episodes_root = inactive_root / "episodes"
        archive_root = inactive_root / "archive"
        return DocumentPaths(
            persona=prompt_root / "persona.md",
            world_state=prompt_root / "world_state.md",
            cast_relations=prompt_root / "cast_relations.md",
            cast_pool=prompt_root / "cast_pool.md",
            relationship=relationship_root / "relationship.md",
            translation_layer=prompt_root / "translation_layer.md",
            memory_promotion_rules=docs / "memory_promotion_rules.md",
            user_profile=memory_root / "user_profile.json",
            relationship_memory=memory_root / "relationship_memory.json",
            active_arc=memory_root / "active_arc.json",
            conflict_notes=memory_root / "conflict_notes.json",
            user_episode_dir=episodes_root / "user",
            character_episode_dir=episodes_root / "character",
            session_root=inactive_root,
            session_archive_dir=archive_root,
        )
    session_root = session_profile_dir(repo_root, active_profile.character.slug, active_profile.relationship.slug)
    character_prompt_root = character_prompt_dir(repo_root, active_profile.character.slug)
    relationship_prompt_root = relationship_prompt_dir(
        repo_root,
        active_profile.character.slug,
        active_profile.relationship.slug,
    )
    memory_root = session_root / "memory"
    episodes_root = session_root / "episodes"
    return DocumentPaths(
        persona=character_prompt_root / "persona.md",
        world_state=character_prompt_root / "world_state.md",
        cast_relations=character_prompt_root / "cast_relations.md",
        cast_pool=character_prompt_root / "cast_pool.md",
        relationship=relationship_prompt_root / "relationship.md",
        translation_layer=character_prompt_root / "translation_layer.md",
        memory_promotion_rules=docs / "memory_promotion_rules.md",
        user_profile=memory_root / "user_profile.json",
        relationship_memory=memory_root / "relationship_memory.json",
        active_arc=memory_root / "active_arc.json",
        conflict_notes=memory_root / "conflict_notes.json",
        user_episode_dir=episodes_root / "user",
        character_episode_dir=episodes_root / "character",
        session_root=session_root,
        session_archive_dir=session_root / "archive",
    )


def _validate_stage_provider_names(llm: LLMConfig) -> None:
    ensure_provider_support(stage_name="llm.stage1.provider", provider=llm.stage1.provider, capability=TEXT_CAPABILITY)
    ensure_provider_support(stage_name="llm.stage2.provider", provider=llm.stage2.provider, capability=TEXT_CAPABILITY)
    if llm.image.enabled:
        ensure_provider_support(stage_name="llm.image.provider", provider=llm.image.provider, capability=IMAGE_CAPABILITY)


def _required_provider_capabilities(llm: LLMConfig) -> dict[str, str]:
    required = {
        llm.stage1.provider: TEXT_CAPABILITY,
        llm.stage2.provider: TEXT_CAPABILITY,
    }
    if llm.image.enabled:
        required[llm.image.provider] = IMAGE_CAPABILITY
    return required


def _load_provider_settings_for_llm(payload: dict[str, Any], llm: LLMConfig) -> dict[str, ProviderSettings]:
    providers: dict[str, ProviderSettings] = {}
    for provider_name, capability in _required_provider_capabilities(llm).items():
        spec = get_provider_spec(provider_name)
        provider_payload = _require_mapping(payload, spec.name)
        providers[spec.name] = _load_provider_settings(
            provider_payload,
            spec.name,
            require_image_model="image_model" in spec.required_paths(capability),
        )

    for provider_name, spec in PROVIDER_SPECS.items():
        if provider_name in providers:
            continue
        provider_payload = payload.get(provider_name)
        if not isinstance(provider_payload, dict):
            continue
        try:
            providers[provider_name] = _load_provider_settings(
                provider_payload,
                provider_name,
            )
        except ValueError:
            continue
    return providers


def validate_live_settings(settings: Settings) -> None:
    missing_fields: set[str] = set()

    if settings.active_profile is None:
        missing_fields.add("profiles.active")

    def require(path: str, value: str | None) -> None:
        if not isinstance(value, str) or not value.strip() or _is_placeholder_value(value):
            missing_fields.add(path)

    def require_api_key(provider: str, api_key: str) -> None:
        if not isinstance(api_key, str) or not api_key.strip() or _is_placeholder_value(api_key):
            missing_fields.add(f"{provider}.api_key")

    def require_stage(path: str, provider: str, *, capability: str) -> None:
        provider = provider.strip().lower()
        try:
            spec = get_provider_spec(provider)
        except ValueError:
            missing_fields.add(f"{path}.provider")
            return
        provider_settings = settings.providers.get(spec.name)
        if provider_settings is None:
            missing_fields.add(spec.name)
            return
        if not spec.supports(capability):
            missing_fields.add(f"{path}.provider({provider} lacks image support)")
            return
        for required_name in spec.required_paths(capability):
            require(f"{spec.name}.{required_name}", getattr(provider_settings, required_name))
        require_api_key(spec.name, provider_settings.api_key)

    require("mail.primary_user_recipient", settings.mail.primary_user_recipient)
    require("mail.character_mailbox.address", settings.mail.character_mailbox.address)
    require("mail.character_mailbox.imap.host", settings.mail.character_mailbox.imap.host)
    require("mail.character_mailbox.imap.username", settings.mail.character_mailbox.imap.username)
    require("mail.character_mailbox.imap.password", settings.mail.character_mailbox.imap.password)
    require("mail.character_mailbox.imap.folder", settings.mail.character_mailbox.imap.folder)
    require("mail.character_mailbox.smtp.host", settings.mail.character_mailbox.smtp.host)
    require("mail.character_mailbox.smtp.username", settings.mail.character_mailbox.smtp.username)
    require("mail.character_mailbox.smtp.password", settings.mail.character_mailbox.smtp.password)
    if settings.mail.alerts.enabled:
        require("mail.alerts.recipient", settings.mail.alerts.recipient)
    require_stage("llm.stage1", settings.llm.stage1.provider, capability=TEXT_CAPABILITY)
    require_stage("llm.stage2", settings.llm.stage2.provider, capability=TEXT_CAPABILITY)
    if settings.llm.image.enabled:
        require_stage("llm.image", settings.llm.image.provider, capability=IMAGE_CAPABILITY)

    if missing_fields:
        joined = ", ".join(sorted(missing_fields))
        raise ValueError(f"Missing required settings for live mode: {joined}.")


def load_settings(config_path: str | Path = "config.yaml", *, require_active_profile: bool = True) -> Settings:
    config_path = Path(config_path).resolve()
    payload = _load_config_payload(config_path)

    project_payload = _require_mapping(payload, "project")
    repo_root = _resolve_repo_root(config_path, project_payload)
    schedule_payload = _require_mapping(payload, "schedule")
    mail_payload = _require_mapping(payload, "mail")
    storage_payload = _require_mapping(payload, "storage")
    llm_payload = _require_mapping(payload, "llm")
    logging_payload = _require_mapping(payload, "logging")
    character_mailbox_payload = _require_mapping(mail_payload, "character_mailbox", "mail.character_mailbox")

    selected_profile = load_selected_profile_context(repo_root)
    active_profile: ActiveProfileContext | None = None
    if selected_profile is not None and selected_profile.session is not None:
        active_profile = ActiveProfileContext(
            selection=selected_profile.selection,
            character=selected_profile.character,
            relationship=selected_profile.relationship,
            session=selected_profile.session,
        )
    if active_profile is None and require_active_profile:
        raise ValueError("No active profile selected. Use `python3 -m dropline select ...` first.")

    llm = LLMConfig(
        language=_require_supported_llm_language(llm_payload, "language", "llm.language"),
        stage1=_load_stage_config(_require_mapping(llm_payload, "stage1", "llm.stage1"), "llm.stage1"),
        stage2=_load_stage_config(_require_mapping(llm_payload, "stage2", "llm.stage2"), "llm.stage2"),
        image=_load_image_config(_require_mapping(llm_payload, "image", "llm.image"), "llm.image"),
    )
    _validate_stage_provider_names(llm)
    providers = _load_provider_settings_for_llm(payload, llm)
    documents = _build_document_paths(repo_root, active_profile)
    primary_user_recipient = _require_string(
        mail_payload,
        "primary_user_recipient",
        "mail.primary_user_recipient",
    )
    return Settings(
        repo_root=repo_root,
        project=ProjectConfig(
            name=_require_string(project_payload, "name", "project.name"),
            timezone=_require_timezone(project_payload, "timezone", "project.timezone"),
        ),
        schedule=_load_schedule_config(schedule_payload),
        mail=MailConfig(
            allowlist_senders=tuple(
                item.lower() for item in _require_string_list(mail_payload, "allowlist_senders", "mail.allowlist_senders")
            ),
            primary_user_recipient=primary_user_recipient,
            character_mailbox=CharacterMailboxConfig(
                address=_require_string(character_mailbox_payload, "address", "mail.character_mailbox.address"),
                imap=_load_mail_endpoint(
                    _require_mapping(character_mailbox_payload, "imap", "mail.character_mailbox.imap"),
                    "mail.character_mailbox.imap",
                    require_folder=True,
                ),
                smtp=_load_smtp_endpoint(
                    _require_mapping(character_mailbox_payload, "smtp", "mail.character_mailbox.smtp"),
                    "mail.character_mailbox.smtp",
                ),
            ),
            alerts=_load_mail_alerts_config(mail_payload, default_recipient=primary_user_recipient),
        ),
        storage=StorageConfig(
            sqlite_path=_resolve_path(repo_root, _require_string(storage_payload, "sqlite_path", "storage.sqlite_path")),
        ),
        llm=llm,
        providers=providers,
        logging=LoggingConfig(level=_require_string(logging_payload, "level", "logging.level")),
        documents=documents,
        active_profile=active_profile,
    )
