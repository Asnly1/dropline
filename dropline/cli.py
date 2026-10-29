from __future__ import annotations

import argparse
import logging
import shutil
import shlex
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence, TextIO

from . import (
    init_state,
    install_scheduler,
    profiles,
    recovery,
    reset_dev_state,
    run_once,
    scheduler_tick,
    stale_runs,
    uninstall_scheduler,
)
from .config import load_settings, resolve_repo_root_from_config
from .scheduler_support import DEFAULT_SCHEDULER_TASK_NAME
from .profile_manager import (
    character_display_name,
    list_character_profile_metas,
    list_relationship_profile_metas,
    resolve_character_profile_meta,
    resolve_relationship_profile_meta,
)


PromptFn = Callable[[str], str]
CommandHandler = Callable[[argparse.Namespace], int]
PromptInfoFn = Callable[[argparse.Namespace, TextIO], None]
OptionResolver = Callable[[str, argparse.Namespace], str]
CommandValidator = Callable[[argparse.Namespace], None]
_PROMPT_SESSION = None


class CLIArgumentError(ValueError):
    """Raised when a CLI command cannot be parsed."""


class CommandParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CLIArgumentError(message)


@dataclass(frozen=True)
class OptionSpec:
    flags: tuple[str, ...]
    dest: str
    help: str
    prompt: str | None = None
    required: bool = False
    prompt_if_missing: bool = False
    default: object = None
    action: str | None = None
    metavar: str | None = None
    prompt_info: PromptInfoFn | None = None
    resolver: OptionResolver | None = None


@dataclass(frozen=True)
class CommandSpec:
    name: str
    help: str
    handler: CommandHandler
    options: tuple[OptionSpec, ...] = ()
    confirm_message: str | None = None
    validator: CommandValidator | None = None


def _interactive_input(prompt_text: str) -> str:
    global _PROMPT_SESSION
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return input(prompt_text)
    try:
        from prompt_toolkit import PromptSession
    except ImportError:
        return input(prompt_text)
    if _PROMPT_SESSION is None:
        _PROMPT_SESSION = PromptSession()
    return _PROMPT_SESSION.prompt(prompt_text)


def _repo_root_from_config_path(config_path: str) -> Path:
    return resolve_repo_root_from_config(config_path)


def _print_available_characters(args: argparse.Namespace, output: TextIO) -> None:
    characters = list_character_profile_metas(_repo_root_from_config_path(args.config))
    if not characters:
        print("Available characters: none", file=output)
        return
    print("Available characters:", file=output)
    for character in characters:
        print(f"  - {character_display_name(character)}", file=output)


def _print_available_relationships(args: argparse.Namespace, output: TextIO) -> None:
    character_value = str(getattr(args, "character", "") or "").strip()
    if not character_value:
        print("Available relationships: enter a character name first.", file=output)
        return
    repo_root = _repo_root_from_config_path(args.config)
    try:
        character = resolve_character_profile_meta(repo_root, character_value)
    except ValueError as exc:
        print(f"Available relationships: {exc}", file=output)
        return
    if character is None:
        print(f"Available relationships: character not found: {character_value}", file=output)
        return
    relationships = list_relationship_profile_metas(repo_root, character.slug)
    if not relationships:
        print(f"Available relationships for {character_display_name(character)}: none", file=output)
        return
    print(f"Available relationships for {character_display_name(character)}:", file=output)
    for relationship in relationships:
        print(f"  - {relationship.name}", file=output)


def _resolve_character_input(value: str, args: argparse.Namespace) -> str:
    repo_root = _repo_root_from_config_path(args.config)
    try:
        character = resolve_character_profile_meta(repo_root, value)
    except ValueError as exc:
        raise CLIArgumentError(str(exc)) from exc
    if character is not None:
        return character.slug
    return value


def _resolve_relationship_input(value: str, args: argparse.Namespace) -> str:
    try:
        character_slug = _resolve_character_input(str(getattr(args, "character", "") or ""), args)
    except CLIArgumentError as exc:
        raise CLIArgumentError(str(exc)) from exc
    repo_root = _repo_root_from_config_path(args.config)
    try:
        relationship = resolve_relationship_profile_meta(repo_root, character_slug, value)
    except ValueError as exc:
        raise CLIArgumentError(str(exc)) from exc
    if relationship is not None:
        return relationship.slug
    return value


def _configure_logging_from_config(config_path: str, *, require_active_profile: bool = True) -> None:
    settings = load_settings(config_path, require_active_profile=require_active_profile)
    logging.basicConfig(
        level=getattr(logging, settings.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _handle_list(args: argparse.Namespace) -> int:
    return profiles.list_profiles(args.config)


def _handle_show_active(args: argparse.Namespace) -> int:
    return profiles.show_active_profile(args.config)


def _handle_generate_character(args: argparse.Namespace) -> int:
    _configure_logging_from_config(args.config, require_active_profile=False)
    return profiles.generate_character_profile(args.config, work_title=args.work, character_name=args.character)


def _handle_generate_relationship(args: argparse.Namespace) -> int:
    _configure_logging_from_config(args.config, require_active_profile=False)
    return profiles.generate_relationship_profile(
        args.config,
        character_slug_value=args.character,
        relationship_name=args.name,
        relationship_description=args.relationship,
    )


def _handle_select(args: argparse.Namespace) -> int:
    return profiles.select_profile(
        args.config,
        character_slug_value=args.character,
        relationship_slug_value=args.relationship,
    )


def _handle_show_inbox(args: argparse.Namespace) -> int:
    return profiles.show_inbox(
        args.config,
        character_slug_value=args.character,
        relationship_slug_value=args.relationship,
        limit=int(args.limit),
    )


def _handle_show_outbox(args: argparse.Namespace) -> int:
    return profiles.show_outbox(
        args.config,
        character_slug_value=args.character,
        relationship_slug_value=args.relationship,
        limit=int(args.limit),
    )


def _handle_delete_session(args: argparse.Namespace) -> int:
    return profiles.delete_session(
        args.config,
        character_slug_value=args.character,
        relationship_slug_value=args.relationship,
        yes=True,
    )


def _handle_delete_inbound(args: argparse.Namespace) -> int:
    return profiles.delete_inbound(
        args.config,
        character_slug_value=args.character,
        relationship_slug_value=args.relationship,
        imap_uid=int(args.uid) if args.uid else None,
        delete_all=args.all,
        yes=True,
    )


def _handle_delete_outbound(args: argparse.Namespace) -> int:
    return profiles.delete_outbound(
        args.config,
        character_slug_value=args.character,
        relationship_slug_value=args.relationship,
        message_id=args.message_id,
        delete_all=args.all,
        yes=True,
    )


def _handle_init_state(args: argparse.Namespace) -> int:
    return init_state.initialize_state(args.config)


def _handle_run_once(args: argparse.Namespace) -> int:
    return run_once.run_once_command(args.config)


def _handle_scheduler_tick(args: argparse.Namespace) -> int:
    return scheduler_tick.scheduler_tick_command(args.config)


def _handle_install_scheduler(args: argparse.Namespace) -> int:
    return install_scheduler.install_scheduler_command(
        args.config,
        task_name=args.task_name,
        python_executable=args.python_executable,
        no_load=args.no_load,
    )


def _handle_uninstall_scheduler(args: argparse.Namespace) -> int:
    return uninstall_scheduler.uninstall_scheduler_command(args.config, task_name=args.task_name)


def _handle_reset_dev_state(args: argparse.Namespace) -> int:
    return reset_dev_state.reset_dev_state_command(args.config, task_name=args.task_name)


def _handle_mark_stale_runs_failed(args: argparse.Namespace) -> int:
    return stale_runs.mark_stale_runs_failed_command(
        args.config,
        older_than_days=int(args.older_than_days),
        dry_run=args.dry_run,
    )


def _handle_status(args: argparse.Namespace) -> int:
    return recovery.status_command(args.config)


def _handle_requeue_inbound(args: argparse.Namespace) -> int:
    return recovery.requeue_inbound_command(args.config, imap_uid=int(args.uid))


def _validate_delete_inbound_args(args: argparse.Namespace) -> None:
    has_uid = not _is_blank(getattr(args, "uid", None))
    has_all = bool(getattr(args, "all", False))
    if has_uid == has_all:
        raise CLIArgumentError("exactly one of --uid or --all is required")
    if has_uid:
        try:
            parsed = int(str(args.uid))
        except ValueError as exc:
            raise CLIArgumentError("--uid must be an integer") from exc
        if parsed <= 0:
            raise CLIArgumentError("--uid must be a positive integer")
        args.uid = str(parsed)


def _validate_delete_outbound_args(args: argparse.Namespace) -> None:
    has_message_id = not _is_blank(getattr(args, "message_id", None))
    has_all = bool(getattr(args, "all", False))
    if has_message_id == has_all:
        raise CLIArgumentError("exactly one of --message-id or --all is required")


def _validate_requeue_inbound_args(args: argparse.Namespace) -> None:
    try:
        parsed = int(str(args.uid))
    except ValueError as exc:
        raise CLIArgumentError("--uid must be a positive integer") from exc
    if parsed <= 0:
        raise CLIArgumentError("--uid must be a positive integer")
    args.uid = str(parsed)


def _validate_mark_stale_runs_failed_args(args: argparse.Namespace) -> None:
    try:
        parsed = int(str(args.older_than_days))
    except ValueError as exc:
        raise CLIArgumentError("--older-than-days must be an integer") from exc
    if parsed <= 0:
        raise CLIArgumentError("--older-than-days must be a positive integer")
    args.older_than_days = str(parsed)


def _validate_positive_limit_args(args: argparse.Namespace) -> None:
    try:
        parsed = int(str(args.limit))
    except ValueError as exc:
        raise CLIArgumentError("--limit must be a positive integer") from exc
    if parsed <= 0:
        raise CLIArgumentError("--limit must be a positive integer")
    args.limit = str(parsed)


YES_OPTION = OptionSpec(("--yes",), "yes", "Skip interactive confirmation", default=False, action="store_true")


COMMAND_SPECS = (
    CommandSpec(
        name="list",
        help="List character, relationship, and session profiles.",
        handler=_handle_list,
        options=(
            OptionSpec(("--config",), "config", "Path to config.yaml", default="config.yaml", metavar="PATH"),
        ),
    ),
    CommandSpec(
        name="show-active",
        help="Show the currently selected character and relationship.",
        handler=_handle_show_active,
        options=(
            OptionSpec(("--config",), "config", "Path to config.yaml", default="config.yaml", metavar="PATH"),
        ),
    ),
    CommandSpec(
        name="show-inbox",
        help="Show unconsumed inbound mail for the active or specified session.",
        handler=_handle_show_inbox,
        options=(
            OptionSpec(("--config",), "config", "Path to config.yaml", default="config.yaml", metavar="PATH"),
            OptionSpec(
                ("--character",),
                "character",
                "Character name",
                default=None,
                metavar="NAME",
                resolver=_resolve_character_input,
            ),
            OptionSpec(
                ("--relationship",),
                "relationship",
                "Relationship name",
                default=None,
                metavar="NAME",
                resolver=_resolve_relationship_input,
            ),
            OptionSpec(("--limit",), "limit", "Maximum number of messages to show", default=5, metavar="N"),
        ),
        validator=_validate_positive_limit_args,
    ),
    CommandSpec(
        name="show-outbox",
        help="Show recent outbound mail for the active or specified session.",
        handler=_handle_show_outbox,
        options=(
            OptionSpec(("--config",), "config", "Path to config.yaml", default="config.yaml", metavar="PATH"),
            OptionSpec(
                ("--character",),
                "character",
                "Character name",
                default=None,
                metavar="NAME",
                resolver=_resolve_character_input,
            ),
            OptionSpec(
                ("--relationship",),
                "relationship",
                "Relationship name",
                default=None,
                metavar="NAME",
                resolver=_resolve_relationship_input,
            ),
            OptionSpec(("--limit",), "limit", "Maximum number of outbound messages to show", default=5, metavar="N"),
        ),
        validator=_validate_positive_limit_args,
    ),
    CommandSpec(
        name="generate-character",
        help="Generate a character profile with the stage2 model.",
        handler=_handle_generate_character,
        options=(
            OptionSpec(("--config",), "config", "Path to config.yaml", default="config.yaml", metavar="PATH"),
            OptionSpec(("--character",), "character", "Character name", prompt="Character name", required=True, metavar="NAME"),
            OptionSpec(
                ("--work",),
                "work",
                "Optional work title",
                prompt="Work title (optional)",
                prompt_if_missing=True,
                metavar="TITLE",
            ),
        ),
    ),
    CommandSpec(
        name="generate-relationship",
        help="Generate a relationship profile under an existing character.",
        handler=_handle_generate_relationship,
        options=(
            OptionSpec(("--config",), "config", "Path to config.yaml", default="config.yaml", metavar="PATH"),
            OptionSpec(
                ("--character",),
                "character",
                "Character name",
                prompt="Character name",
                required=True,
                metavar="NAME",
                prompt_info=_print_available_characters,
                resolver=_resolve_character_input,
            ),
            OptionSpec(
                ("--relationship",),
                "relationship",
                "Relationship description",
                prompt="Relationship description",
                required=True,
                metavar="TEXT",
            ),
            OptionSpec(
                ("--name",),
                "name",
                "Optional short display name",
                prompt="Relationship name (optional)",
                prompt_if_missing=True,
                metavar="NAME",
            ),
        ),
    ),
    CommandSpec(
        name="select",
        help="Select the active character and relationship session.",
        handler=_handle_select,
        options=(
            OptionSpec(("--config",), "config", "Path to config.yaml", default="config.yaml", metavar="PATH"),
            OptionSpec(
                ("--character",),
                "character",
                "Character name",
                prompt="Character name",
                required=True,
                metavar="NAME",
                prompt_info=_print_available_characters,
                resolver=_resolve_character_input,
            ),
            OptionSpec(
                ("--relationship",),
                "relationship",
                "Relationship name",
                prompt="Relationship name",
                required=True,
                metavar="NAME",
                prompt_info=_print_available_relationships,
                resolver=_resolve_relationship_input,
            ),
        ),
    ),
    CommandSpec(
        name="delete-session",
        help="Delete a session and all of its scoped state.",
        handler=_handle_delete_session,
        options=(
            OptionSpec(("--config",), "config", "Path to config.yaml", default="config.yaml", metavar="PATH"),
            OptionSpec(
                ("--character",),
                "character",
                "Character name",
                prompt="Character name",
                required=True,
                metavar="NAME",
                prompt_info=_print_available_characters,
                resolver=_resolve_character_input,
            ),
            OptionSpec(
                ("--relationship",),
                "relationship",
                "Relationship name",
                prompt="Relationship name",
                required=True,
                metavar="NAME",
                prompt_info=_print_available_relationships,
                resolver=_resolve_relationship_input,
            ),
            YES_OPTION,
        ),
        confirm_message="Delete this session and all scoped state?",
    ),
    CommandSpec(
        name="delete-inbound",
        help="Delete one inbound message or clear the current session inbox.",
        handler=_handle_delete_inbound,
        options=(
            OptionSpec(("--config",), "config", "Path to config.yaml", default="config.yaml", metavar="PATH"),
            OptionSpec(
                ("--character",),
                "character",
                "Character name",
                prompt="Character name",
                required=True,
                metavar="NAME",
                prompt_info=_print_available_characters,
                resolver=_resolve_character_input,
            ),
            OptionSpec(
                ("--relationship",),
                "relationship",
                "Relationship name",
                prompt="Relationship name",
                required=True,
                metavar="NAME",
                prompt_info=_print_available_relationships,
                resolver=_resolve_relationship_input,
            ),
            OptionSpec(("--uid",), "uid", "Inbound IMAP UID", default=None, metavar="UID"),
            OptionSpec(("--all",), "all", "Delete all inbound messages", default=False, action="store_true"),
            YES_OPTION,
        ),
        confirm_message="Delete inbound message data?",
        validator=_validate_delete_inbound_args,
    ),
    CommandSpec(
        name="delete-outbound",
        help="Delete one outbound message or clear the current session outbox.",
        handler=_handle_delete_outbound,
        options=(
            OptionSpec(("--config",), "config", "Path to config.yaml", default="config.yaml", metavar="PATH"),
            OptionSpec(
                ("--character",),
                "character",
                "Character name",
                prompt="Character name",
                required=True,
                metavar="NAME",
                prompt_info=_print_available_characters,
                resolver=_resolve_character_input,
            ),
            OptionSpec(
                ("--relationship",),
                "relationship",
                "Relationship name",
                prompt="Relationship name",
                required=True,
                metavar="NAME",
                prompt_info=_print_available_relationships,
                resolver=_resolve_relationship_input,
            ),
            OptionSpec(("--message-id",), "message_id", "Outbound message id", default=None, metavar="ID"),
            OptionSpec(("--all",), "all", "Delete all outbound messages", default=False, action="store_true"),
            YES_OPTION,
        ),
        confirm_message="Delete outbound message data?",
        validator=_validate_delete_outbound_args,
    ),
    CommandSpec(
        name="requeue-inbound",
        help="Clear quarantine and reset the failed-attempt count for one inbound message in the active session.",
        handler=_handle_requeue_inbound,
        options=(
            OptionSpec(("--config",), "config", "Path to config.yaml", default="config.yaml", metavar="PATH"),
            OptionSpec(("--uid",), "uid", "Inbound IMAP UID", prompt="Inbound IMAP UID", required=True, metavar="UID"),
        ),
        validator=_validate_requeue_inbound_args,
    ),
    CommandSpec(
        name="init-state",
        help="Initialize SQLite state and memory files for the active profile.",
        handler=_handle_init_state,
        options=(
            OptionSpec(("--config",), "config", "Path to config.yaml", default="config.yaml", metavar="PATH"),
        ),
    ),
    CommandSpec(
        name="run-once",
        help="Run the virtual-character mail pipeline once.",
        handler=_handle_run_once,
        options=(
            OptionSpec(("--config",), "config", "Path to config.yaml", default="config.yaml", metavar="PATH"),
        ),
    ),
    CommandSpec(
        name="scheduler-tick",
        help="Run a single scheduler tick.",
        handler=_handle_scheduler_tick,
        options=(
            OptionSpec(("--config",), "config", "Path to config.yaml", default="config.yaml", metavar="PATH"),
        ),
    ),
    CommandSpec(
        name="mark-stale-runs-failed",
        help="Mark stale running runs as failed for the active session.",
        handler=_handle_mark_stale_runs_failed,
        options=(
            OptionSpec(("--config",), "config", "Path to config.yaml", default="config.yaml", metavar="PATH"),
            OptionSpec(
                ("--older-than-days",),
                "older_than_days",
                "Only mark running runs older than this many days",
                default=7,
                metavar="N",
            ),
            OptionSpec(
                ("--dry-run",),
                "dry_run",
                "List stale running runs without updating them",
                default=False,
                action="store_true",
            ),
        ),
        validator=_validate_mark_stale_runs_failed_args,
    ),
    CommandSpec(
        name="status",
        help="Show recent runs, failure counters, inbox/outbox state, and today's schedule for the active session.",
        handler=_handle_status,
        options=(
            OptionSpec(("--config",), "config", "Path to config.yaml", default="config.yaml", metavar="PATH"),
        ),
    ),
    CommandSpec(
        name="install-scheduler",
        help="Install the scheduler job for macOS or Windows.",
        handler=_handle_install_scheduler,
        options=(
            OptionSpec(("--config",), "config", "Path to config.yaml", default="config.yaml", metavar="PATH"),
            OptionSpec(
                ("--task-name",),
                "task_name",
                "Scheduler task name",
                default=DEFAULT_SCHEDULER_TASK_NAME,
                metavar="NAME",
            ),
            OptionSpec(("--python",), "python_executable", "Python executable to run", default=sys.executable, metavar="PATH"),
            OptionSpec(
                ("--no-load",),
                "no_load",
                "Write scheduler files but skip registering the task",
                default=False,
                action="store_true",
            ),
            YES_OPTION,
        ),
        confirm_message="Install or reload the scheduler task?",
    ),
    CommandSpec(
        name="uninstall-scheduler",
        help="Uninstall the scheduler job for macOS or Windows.",
        handler=_handle_uninstall_scheduler,
        options=(
            OptionSpec(("--config",), "config", "Path to config.yaml", default="config.yaml", metavar="PATH"),
            OptionSpec(
                ("--task-name",),
                "task_name",
                "Scheduler task name",
                default=DEFAULT_SCHEDULER_TASK_NAME,
                metavar="NAME",
            ),
            YES_OPTION,
        ),
        confirm_message="Uninstall the scheduler task?",
    ),
    CommandSpec(
        name="reset-dev-state",
        help="Delete local runtime state and rebuild the active session.",
        handler=_handle_reset_dev_state,
        options=(
            OptionSpec(("--config",), "config", "Path to config.yaml", default="config.yaml", metavar="PATH"),
            OptionSpec(
                ("--task-name",),
                "task_name",
                "Scheduler task name to unload",
                default=DEFAULT_SCHEDULER_TASK_NAME,
                metavar="NAME",
            ),
            YES_OPTION,
        ),
        confirm_message="Reset local runtime state?",
    ),
)

COMMANDS = {spec.name: spec for spec in COMMAND_SPECS}


def _build_parser(spec: CommandSpec) -> CommandParser:
    parser = CommandParser(prog=spec.name, description=spec.help, add_help=False, allow_abbrev=False)
    parser.add_argument("-h", "--help", action="store_true", dest="_help", help="Show this help message and exit")
    for option in spec.options:
        kwargs: dict[str, object] = {
            "dest": option.dest,
            "help": option.help,
            "default": option.default,
        }
        if option.metavar is not None:
            kwargs["metavar"] = option.metavar
        if option.action is not None:
            kwargs["action"] = option.action
        parser.add_argument(*option.flags, **kwargs)
    return parser


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _prompt_for_option(
    option: OptionSpec,
    *,
    args: argparse.Namespace,
    input_fn: PromptFn,
    output: TextIO,
) -> str | None:
    while True:
        if option.prompt_info is not None:
            option.prompt_info(args, output)
        response = input_fn(f"{option.prompt}: ").strip()
        if response:
            return response
        if option.required:
            print("Value required.", file=output)
            continue
        return None


def _fill_missing_arguments(
    spec: CommandSpec,
    args: argparse.Namespace,
    *,
    input_fn: PromptFn,
    output: TextIO,
) -> None:
    for option in spec.options:
        if option.action is not None or option.prompt is None:
            continue
        current_value = getattr(args, option.dest)
        if option.required and _is_blank(current_value):
            setattr(args, option.dest, _prompt_for_option(option, args=args, input_fn=input_fn, output=output))
            continue
        if option.prompt_if_missing and _is_blank(current_value):
            setattr(args, option.dest, _prompt_for_option(option, args=args, input_fn=input_fn, output=output))


def _resolve_option_values(spec: CommandSpec, args: argparse.Namespace) -> None:
    for option in spec.options:
        if option.resolver is None:
            continue
        current_value = getattr(args, option.dest)
        if _is_blank(current_value):
            continue
        setattr(args, option.dest, option.resolver(str(current_value), args))


def _confirm(spec: CommandSpec, args: argparse.Namespace, *, input_fn: PromptFn, output: TextIO) -> bool:
    if spec.confirm_message is None:
        return True
    if bool(getattr(args, "yes", False)):
        return True
    response = input_fn(f"{spec.confirm_message} [y/N]: ").strip().lower()
    if response in {"y", "yes"}:
        return True
    print("Cancelled.", file=output)
    return False


def _root_help_width(output: TextIO) -> int:
    if hasattr(output, "isatty") and output.isatty():
        return shutil.get_terminal_size(fallback=(88, 24)).columns
    return 88


def _print_help_row(name: str, help_text: str, *, name_width: int, total_width: int, output: TextIO) -> None:
    indent = "  "
    help_indent = indent + " " * name_width
    available_width = max(32, total_width - len(help_indent))
    wrapped = textwrap.wrap(help_text, width=available_width) or [""]
    print(f"{indent}{name:<{name_width}}{wrapped[0]}", file=output)
    for line in wrapped[1:]:
        print(f"{help_indent}{line}", file=output)


def _print_root_help(output: TextIO) -> None:
    name_width = max(
        max(len(spec.name) for spec in COMMAND_SPECS),
        len("help [command]"),
        len("exit, quit"),
    ) + 2
    total_width = _root_help_width(output)
    print("Commands:", file=output)
    for spec in COMMAND_SPECS:
        _print_help_row(spec.name, spec.help, name_width=name_width, total_width=total_width, output=output)
    print("", file=output)
    print("Shell commands:", file=output)
    _print_help_row(
        "help [command]",
        "Show root help or command help.",
        name_width=name_width,
        total_width=total_width,
        output=output,
    )
    _print_help_row(
        "exit, quit",
        "Leave the interactive CLI.",
        name_width=name_width,
        total_width=total_width,
        output=output,
    )


def _print_command_help(command_name: str, *, output: TextIO) -> int:
    spec = COMMANDS.get(command_name)
    if spec is None:
        print(f"Unknown command: {command_name}", file=output)
        return 1
    print(_build_parser(spec).format_help(), end="", file=output)
    return 0


def execute_command(
    argv: Sequence[str],
    *,
    interactive: bool = False,
    input_fn: PromptFn = _interactive_input,
    output: TextIO | None = None,
) -> int:
    output = output or sys.stdout
    if not argv:
        _print_root_help(output)
        return 0

    command_name = argv[0]
    spec = COMMANDS.get(command_name)
    if spec is None:
        print(f"Unknown command: {argv[0]}", file=output)
        print("Type `help` to list available commands.", file=output)
        return 1

    parser = _build_parser(spec)
    try:
        args = parser.parse_args(argv[1:])
    except CLIArgumentError as exc:
        print(f"Argument error: {exc}", file=output)
        print(_build_parser(spec).format_usage().strip(), file=output)
        return 1

    if getattr(args, "_help"):
        print(parser.format_help(), end="", file=output)
        return 0

    try:
        if interactive:
            _fill_missing_arguments(spec, args, input_fn=input_fn, output=output)
        else:
            missing_required = [
                option.flags[0]
                for option in spec.options
                if option.required and _is_blank(getattr(args, option.dest))
            ]
            if missing_required:
                print(f"Argument error: missing required arguments: {', '.join(missing_required)}", file=output)
                print(_build_parser(spec).format_usage().strip(), file=output)
                return 1
        if spec.validator is not None:
            spec.validator(args)
        _resolve_option_values(spec, args)
        if not _confirm(spec, args, input_fn=input_fn, output=output):
            return 1
    except (EOFError, KeyboardInterrupt):
        print("\nCommand cancelled.", file=output)
        return 1
    except CLIArgumentError as exc:
        print(f"Argument error: {exc}", file=output)
        return 1
    except (RuntimeError, OSError) as exc:
        print(f"Runtime error: {exc}", file=output)
        return 1
    try:
        return spec.handler(args)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=output)
        return 1
    except (RuntimeError, OSError) as exc:
        print(f"Runtime error: {exc}", file=output)
        return 1


def run_repl(*, input_fn: PromptFn = _interactive_input, output: TextIO | None = None) -> int:
    output = output or sys.stdout
    print("dropline interactive CLI. Type `help` to list commands, `exit` to quit.", file=output)
    while True:
        try:
            raw_line = input_fn("dropline> ")
        except (EOFError, KeyboardInterrupt):
            print("", file=output)
            return 0
        line = raw_line.strip()
        if not line:
            continue
        if line in {"exit", "quit"}:
            return 0
        try:
            argv = shlex.split(line)
        except ValueError as exc:
            print(f"Input error: {exc}", file=output)
            continue
        if not argv:
            continue
        if argv[0] == "help":
            if len(argv) == 1:
                _print_root_help(output)
            else:
                _print_command_help(argv[1], output=output)
            continue
        execute_command(argv, interactive=True, input_fn=input_fn, output=output)


def main(
    argv: Sequence[str] | None = None,
    *,
    input_fn: PromptFn = _interactive_input,
    output: TextIO | None = None,
) -> int:
    output = output or sys.stdout
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return run_repl(input_fn=input_fn, output=output)
    if args[0] in {"help", "-h", "--help"}:
        if len(args) == 1:
            _print_root_help(output)
            return 0
        return _print_command_help(args[1], output=output)
    return execute_command(args, interactive=False, input_fn=input_fn, output=output)
