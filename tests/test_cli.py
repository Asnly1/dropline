from __future__ import annotations

import io
import shlex
import sys
import tempfile
import unittest
from collections.abc import Iterable
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dropline import cli
from dropline.storage import StateStore
from tests.helpers import create_test_repo, load_test_settings


def _input_sequence(values: Iterable[str]):
    iterator = iter(values)

    def _reader(_prompt: str) -> str:
        return next(iterator)

    return _reader


class InteractiveCLITests(unittest.TestCase):
    def test_repl_dispatches_list_command(self) -> None:
        output = io.StringIO()
        with patch("dropline.cli.profiles.list_profiles", return_value=0) as list_profiles:
            exit_code = cli.main([], input_fn=_input_sequence(["list", "exit"]), output=output)

        self.assertEqual(exit_code, 0)
        list_profiles.assert_called_once_with("config.yaml")
        self.assertIn("dropline interactive CLI", output.getvalue())

    def test_direct_config_command_is_rejected(self) -> None:
        output = io.StringIO()
        exit_code = cli.main(["config", "show"], output=output)
        self.assertEqual(exit_code, 1)
        self.assertIn("Unknown command: config", output.getvalue())

    def test_help_omits_usage_header(self) -> None:
        output = io.StringIO()
        exit_code = cli.main(["help"], output=output)

        self.assertEqual(exit_code, 0)
        rendered = output.getvalue()
        self.assertNotIn("dropline interactive CLI", rendered)
        self.assertNotIn("Usage:", rendered)
        self.assertNotIn("python -m dropline", rendered)
        self.assertNotIn("config", rendered)

    def test_help_config_is_rejected(self) -> None:
        output = io.StringIO()
        exit_code = cli.main(["help", "config"], output=output)

        self.assertEqual(exit_code, 1)
        self.assertIn("Unknown command: config", output.getvalue())

    def test_help_aligns_long_command_names_and_wraps_descriptions(self) -> None:
        output = io.StringIO()
        with patch("dropline.cli._root_help_width", return_value=64):
            exit_code = cli.main(["help"], output=output)

        self.assertEqual(exit_code, 0)
        rendered = output.getvalue()
        self.assertNotIn("mark-stale-runs-failedMark", rendered)
        self.assertIn("  mark-stale-runs-failed  Mark stale running runs as failed", rendered)
        for line in rendered.splitlines():
            self.assertLessEqual(len(line), 64)

    def test_generate_character_prompts_for_missing_values(self) -> None:
        output = io.StringIO()
        with patch("dropline.cli._configure_logging_from_config") as configure_logging:
            with patch("dropline.cli.profiles.generate_character_profile", return_value=0) as generate_character:
                exit_code = cli.main(
                    [],
                    input_fn=_input_sequence(["generate-character", "甘雨", "原神", "exit"]),
                    output=output,
                )

        self.assertEqual(exit_code, 0)
        configure_logging.assert_called_once_with("config.yaml", require_active_profile=False)
        generate_character.assert_called_once_with("config.yaml", work_title="原神", character_name="甘雨")

    def test_generate_character_command_configures_logging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            output = io.StringIO()
            with patch("dropline.cli.profiles.generate_character_profile", return_value=0):
                with patch("dropline.cli.logging.basicConfig") as basic_config:
                    exit_code = cli.main(
                        [
                            "generate-character",
                            "--config",
                            str(root / "config.yaml"),
                            "--character",
                            "甘雨",
                        ],
                        output=output,
                    )

        self.assertEqual(exit_code, 0)
        basic_config.assert_called_once()
        self.assertEqual(basic_config.call_args.kwargs["level"], 20)
        self.assertEqual(
            basic_config.call_args.kwargs["format"],
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

    def test_select_prompt_lists_available_characters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            output = io.StringIO()
            with patch("dropline.cli.profiles.select_profile", return_value=0) as select_profile:
                exit_code = cli.main(
                    [],
                    input_fn=_input_sequence(
                        [
                            f"select --config {shlex.quote(str(root / 'config.yaml'))}",
                            "刻晴",
                            "青梅竹马",
                            "exit",
                        ]
                    ),
                    output=output,
                )

            self.assertEqual(exit_code, 0)
            select_profile.assert_called_once_with(
                str(root / "config.yaml"),
                character_slug_value="keqing",
                relationship_slug_value="childhood-friends",
            )
            rendered = output.getvalue()
            self.assertIn("Available characters:", rendered)
            self.assertIn("- 刻晴 / 原神", rendered)
            self.assertIn("刻晴 / 原神", rendered)

    def test_select_prompt_lists_relationships_for_chosen_character(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            output = io.StringIO()
            with patch("dropline.cli.profiles.select_profile", return_value=0):
                exit_code = cli.main(
                    [],
                    input_fn=_input_sequence(
                        [
                            f"select --config {shlex.quote(str(root / 'config.yaml'))}",
                            "刻晴",
                            "青梅竹马",
                            "exit",
                        ]
                    ),
                    output=output,
                )

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("Available relationships for 刻晴 / 原神:", rendered)
            self.assertIn("- 青梅竹马", rendered)
            self.assertIn("青梅竹马", rendered)

    def test_select_accepts_relationship_display_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            with patch("dropline.cli.profiles.select_profile", return_value=0) as select_profile:
                exit_code = cli.main(
                    [
                        "select",
                        "--config",
                        str(root / "config.yaml"),
                        "--character",
                        "刻晴",
                        "--relationship",
                        "青梅竹马",
                    ],
                    output=io.StringIO(),
                )

            self.assertEqual(exit_code, 0)
            select_profile.assert_called_once_with(
                str(root / "config.yaml"),
                character_slug_value="keqing",
                relationship_slug_value="childhood-friends",
            )

    def test_alias_command_is_rejected(self) -> None:
        output = io.StringIO()
        with patch("dropline.cli.profiles.list_profiles", return_value=0) as list_profiles:
            exit_code = cli.main(["show_active"], output=output)

        self.assertEqual(exit_code, 1)
        list_profiles.assert_not_called()
        self.assertIn("Unknown command: show_active", output.getvalue())

    def test_direct_show_inbox_dispatches_with_resolved_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            with patch("dropline.cli.profiles.show_inbox", return_value=0) as show_inbox:
                exit_code = cli.main(
                    [
                        "show-inbox",
                        "--config",
                        str(root / "config.yaml"),
                        "--character",
                        "刻晴",
                        "--relationship",
                        "青梅竹马",
                        "--limit",
                        "2",
                    ],
                    output=io.StringIO(),
                )

            self.assertEqual(exit_code, 0)
            show_inbox.assert_called_once_with(
                str(root / "config.yaml"),
                character_slug_value="keqing",
                relationship_slug_value="childhood-friends",
                limit=2,
            )

    def test_show_commands_reject_invalid_limits(self) -> None:
        cases = (
            ("show-inbox", "-1"),
            ("show-outbox", "not-an-int"),
        )
        for command_name, limit in cases:
            with self.subTest(command_name=command_name, limit=limit):
                output = io.StringIO()
                with patch("dropline.cli.profiles.show_inbox", return_value=0) as show_inbox, patch(
                    "dropline.cli.profiles.show_outbox",
                    return_value=0,
                ) as show_outbox:
                    exit_code = cli.main([command_name, "--limit", limit], output=output)

                self.assertEqual(exit_code, 1)
                show_inbox.assert_not_called()
                show_outbox.assert_not_called()
                self.assertIn("--limit must be a positive integer", output.getvalue())

    def test_direct_mode_reports_runtime_errors_without_traceback(self) -> None:
        for error_type in (RuntimeError, OSError):
            with self.subTest(error_type=error_type.__name__):
                output = io.StringIO()
                with patch("dropline.cli.profiles.list_profiles", side_effect=error_type("boom")):
                    exit_code = cli.main(["list"], output=output)

                self.assertEqual(exit_code, 1)
                rendered = output.getvalue()
                self.assertIn("Runtime error: boom", rendered)
                self.assertNotIn("Traceback", rendered)

    def test_delete_session_dispatches_with_resolved_scope_and_yes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            with patch("dropline.cli.profiles.delete_session", return_value=0) as delete_session:
                exit_code = cli.main(
                    [
                        "delete-session",
                        "--config",
                        str(root / "config.yaml"),
                        "--character",
                        "刻晴",
                        "--relationship",
                        "青梅竹马",
                    ],
                    input_fn=_input_sequence(["y"]),
                    output=io.StringIO(),
                )

            self.assertEqual(exit_code, 0)
            delete_session.assert_called_once_with(
                str(root / "config.yaml"),
                character_slug_value="keqing",
                relationship_slug_value="childhood-friends",
                yes=True,
            )

    def test_destructive_commands_yes_skip_confirmation_prompt(self) -> None:
        def _unexpected_prompt(_prompt: str) -> str:
            raise AssertionError("confirmation prompt should be skipped")

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            config_path = str(root / "config.yaml")
            cases = (
                (
                    "dropline.cli.profiles.delete_session",
                    [
                        "delete-session",
                        "--config",
                        config_path,
                        "--character",
                        "刻晴",
                        "--relationship",
                        "青梅竹马",
                        "--yes",
                    ],
                ),
                (
                    "dropline.cli.profiles.delete_inbound",
                    [
                        "delete-inbound",
                        "--config",
                        config_path,
                        "--character",
                        "刻晴",
                        "--relationship",
                        "青梅竹马",
                        "--uid",
                        "7",
                        "--yes",
                    ],
                ),
                (
                    "dropline.cli.profiles.delete_outbound",
                    [
                        "delete-outbound",
                        "--config",
                        config_path,
                        "--character",
                        "刻晴",
                        "--relationship",
                        "青梅竹马",
                        "--message-id",
                        "<outbound@example.com>",
                        "--yes",
                    ],
                ),
                (
                    "dropline.cli.reset_dev_state.reset_dev_state_command",
                    ["reset-dev-state", "--config", config_path, "--yes"],
                ),
                (
                    "dropline.cli.install_scheduler.install_scheduler_command",
                    [
                        "install-scheduler",
                        "--config",
                        config_path,
                        "--task-name",
                        "com.example.scheduler",
                        "--yes",
                    ],
                ),
                (
                    "dropline.cli.uninstall_scheduler.uninstall_scheduler_command",
                    [
                        "uninstall-scheduler",
                        "--config",
                        config_path,
                        "--task-name",
                        "com.example.scheduler",
                        "--yes",
                    ],
                ),
            )

            for patch_target, argv in cases:
                with self.subTest(command=argv[0]):
                    output = io.StringIO()
                    with patch(patch_target, return_value=0) as handler:
                        exit_code = cli.main(argv, input_fn=_unexpected_prompt, output=output)

                    self.assertEqual(exit_code, 0)
                    handler.assert_called_once()

    def test_delete_inbound_requires_exactly_one_target_flag(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_config_path = str(Path(tmp_dir) / "config.yaml")
            with patch("dropline.cli.profiles.delete_inbound", return_value=0) as delete_inbound:
                exit_code = cli.main(
                    [
                        "delete-inbound",
                        "--config",
                        missing_config_path,
                        "--character",
                        "keqing",
                        "--relationship",
                        "childhood-friends",
                    ],
                    output=output,
                )

        self.assertEqual(exit_code, 1)
        delete_inbound.assert_not_called()
        self.assertIn("exactly one of --uid or --all is required", output.getvalue())

    def test_removed_command_is_rejected(self) -> None:
        output = io.StringIO()
        with patch("dropline.cli.profiles.list_profiles", return_value=0) as list_profiles:
            exit_code = cli.main(["show-runs"], output=output)

        self.assertEqual(exit_code, 1)
        list_profiles.assert_not_called()
        self.assertIn("Unknown command: show-runs", output.getvalue())

    def test_direct_mode_requires_required_arguments_instead_of_prompting(self) -> None:
        output = io.StringIO()
        with patch("dropline.cli.profiles.generate_character_profile", return_value=0) as generate_character:
            exit_code = cli.main(["generate-character"], input_fn=_input_sequence([]), output=output)

        self.assertEqual(exit_code, 1)
        generate_character.assert_not_called()
        self.assertIn("missing required arguments: --character", output.getvalue())

    def test_reset_dev_state_requires_confirmation(self) -> None:
        output = io.StringIO()
        with patch("dropline.cli.reset_dev_state.reset_dev_state_command", return_value=0) as reset_command:
            exit_code = cli.main(["reset-dev-state"], input_fn=_input_sequence(["n"]), output=output)

        self.assertEqual(exit_code, 1)
        reset_command.assert_not_called()
        self.assertIn("Cancelled.", output.getvalue())

    def test_install_scheduler_dispatches_to_module(self) -> None:
        output = io.StringIO()
        with patch("dropline.cli.install_scheduler.install_scheduler_command", return_value=0) as install_command:
            exit_code = cli.main(
                ["install-scheduler", "--task-name", "com.example.scheduler"],
                input_fn=_input_sequence(["y"]),
                output=output,
            )

        self.assertEqual(exit_code, 0)
        install_command.assert_called_once_with(
            "config.yaml",
            task_name="com.example.scheduler",
            python_executable=sys.executable,
            no_load=False,
        )

    def test_install_launchd_command_is_rejected(self) -> None:
        output = io.StringIO()
        exit_code = cli.main(["install-launchd"], output=output)

        self.assertEqual(exit_code, 1)
        self.assertIn("Unknown command: install-launchd", output.getvalue())

    def test_mark_stale_runs_failed_help_is_available(self) -> None:
        output = io.StringIO()
        exit_code = cli.main(["mark-stale-runs-failed", "--help"], output=output)

        self.assertEqual(exit_code, 0)
        self.assertIn("Mark stale running runs as failed", output.getvalue())
        self.assertIn("--older-than-days", output.getvalue())
        self.assertIn("--dry-run", output.getvalue())

    def test_mark_stale_runs_failed_rejects_non_positive_days(self) -> None:
        output = io.StringIO()
        exit_code = cli.main(["mark-stale-runs-failed", "--older-than-days", "0"], output=output)

        self.assertEqual(exit_code, 1)
        self.assertIn("--older-than-days must be a positive integer", output.getvalue())

    def test_mark_stale_runs_failed_dry_run_and_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            store.start_run("run-stale", "2026-04-08T11:59:59+00:00")

            fixed_now = datetime(2026, 4, 15, 12, 0, tzinfo=ZoneInfo("UTC"))
            dry_run_stdout = io.StringIO()
            with patch("dropline.stale_runs.now_in_timezone", return_value=fixed_now), redirect_stdout(dry_run_stdout):
                dry_run_exit_code = cli.main(
                    [
                        "mark-stale-runs-failed",
                        "--config",
                        str(root / "config.yaml"),
                        "--dry-run",
                    ],
                    output=io.StringIO(),
                )

            self.assertEqual(dry_run_exit_code, 0)
            self.assertIn("Stale running runs candidate count: 1", dry_run_stdout.getvalue())
            self.assertIn("run_id=run-stale", dry_run_stdout.getvalue())
            self.assertEqual(store.list_recent_runs(limit=1)[0].status, "running")

            update_stdout = io.StringIO()
            with patch("dropline.stale_runs.now_in_timezone", return_value=fixed_now), redirect_stdout(update_stdout):
                update_exit_code = cli.main(
                    [
                        "mark-stale-runs-failed",
                        "--config",
                        str(root / "config.yaml"),
                    ],
                    output=io.StringIO(),
                )

            self.assertEqual(update_exit_code, 0)
            self.assertIn("Marked stale running runs failed: 1", update_stdout.getvalue())
            self.assertIn("run_id=run-stale", update_stdout.getvalue())
            updated_run = store.list_recent_runs(limit=1)[0]
            self.assertEqual(updated_run.status, "failed")
            self.assertEqual(updated_run.error_stage, "stale_running")


if __name__ == "__main__":
    unittest.main()
