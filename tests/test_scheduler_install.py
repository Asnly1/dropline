from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dropline.install_scheduler import install_scheduler_command
from dropline.launchd import LaunchdPaths
from dropline.scheduler_support import build_scheduler_paths
from dropline.scheduler_install import install_scheduler, uninstall_scheduler
from dropline.windows_task import (
    build_schtasks_create_command,
    build_schtasks_delete_command,
    build_windows_task_paths,
    install_windows_task,
    render_windows_wrapper_script,
)

from tests.helpers import create_test_repo, load_test_settings


class SchedulerInstallTests(unittest.TestCase):
    def test_build_scheduler_paths_rejects_path_like_task_names(self) -> None:
        for task_name in ("../evil", "/tmp/evil", "folder\\evil", "com..evil", ""):
            with self.subTest(task_name=task_name):
                with self.assertRaises(ValueError):
                    build_scheduler_paths(Path("/tmp/repo"), task_name)

    def test_install_scheduler_dispatches_to_launchd_on_macos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            fake_paths = LaunchdPaths(
                repo_plist_path=root / "launchd" / "com.example.dropline.plist",
                install_plist_path=root / "LaunchAgents" / "com.example.dropline.plist",
                logs_dir=root / "runtime_logs",
                stdout_log_path=root / "runtime_logs" / "out.log",
                stderr_log_path=root / "runtime_logs" / "err.log",
            )

            with patch("dropline.scheduler_install.platform.system", return_value="Darwin"), patch(
                "dropline.scheduler_install.install_launchd_job",
                return_value=fake_paths,
            ) as install_launchd_job:
                result = install_scheduler(
                    settings,
                    config_path=root / "config.yaml",
                    task_name="com.example.dropline",
                    python_executable="/usr/bin/python3",
                )

            self.assertEqual(result.platform, "macos")
            self.assertEqual(result.repo_definition_path, fake_paths.repo_plist_path)
            install_launchd_job.assert_called_once()

    def test_install_scheduler_dispatches_to_windows_task_scheduler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            fake_paths = build_windows_task_paths(root, "com.example.dropline")

            with patch("dropline.scheduler_install.platform.system", return_value="Windows"), patch(
                "dropline.scheduler_install.install_windows_task",
                return_value=fake_paths,
            ) as install_windows_task_mock:
                result = install_scheduler(
                    settings,
                    config_path=root / "config.yaml",
                    task_name="com.example.dropline",
                    python_executable="C:/Python312/python.exe",
                    no_load=True,
                )

            self.assertEqual(result.platform, "windows")
            self.assertFalse(result.loaded)
            self.assertEqual(result.helper_script_path, fake_paths.wrapper_script_path)
            install_windows_task_mock.assert_called_once()

    def test_install_scheduler_command_resolves_python_from_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            fake_result = SimpleNamespace(
                platform="windows",
                helper_script_path=root / "runtime_logs" / "task.cmd",
                logs_dir=root / "runtime_logs",
                loaded=False,
                task_name="com.example.dropline",
            )

            with patch("shutil.which", return_value="/usr/bin/python3"), patch(
                "dropline.install_scheduler.install_scheduler",
                return_value=fake_result,
            ) as install_scheduler_mock, redirect_stdout(io.StringIO()):
                exit_code = install_scheduler_command(
                    str(root / "config.yaml"),
                    task_name="com.example.dropline",
                    python_executable="python3",
                    no_load=True,
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(install_scheduler_mock.call_args.kwargs["python_executable"], "/usr/bin/python3")

    def test_install_scheduler_command_rejects_unresolved_python_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            output = io.StringIO()

            with patch("shutil.which", return_value=None), patch(
                "dropline.install_scheduler.install_scheduler",
            ) as install_scheduler_mock, redirect_stdout(output):
                exit_code = install_scheduler_command(
                    str(root / "config.yaml"),
                    task_name="com.example.dropline",
                    python_executable="missing-python",
                    no_load=True,
                )

        self.assertEqual(exit_code, 1)
        install_scheduler_mock.assert_not_called()
        self.assertIn("Python executable not found", output.getvalue())

    def test_install_scheduler_command_reports_subprocess_failure_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            output = io.StringIO()

            with patch(
                "dropline.install_scheduler.install_scheduler",
                side_effect=subprocess.CalledProcessError(1, ["launchctl", "bootstrap"], stderr="bootstrap failed"),
            ), redirect_stdout(output):
                exit_code = install_scheduler_command(
                    str(root / "config.yaml"),
                    task_name="com.example.dropline",
                    python_executable="/usr/bin/python3",
                )

        self.assertEqual(exit_code, 1)
        self.assertIn("Scheduler install failed", output.getvalue())
        self.assertIn("launchctl bootstrap", output.getvalue())

    def test_uninstall_scheduler_dispatches_to_windows_task_scheduler(self) -> None:
        with patch("dropline.scheduler_install.platform.system", return_value="Windows"), patch(
            "dropline.scheduler_install.uninstall_windows_task",
            return_value=(True, True),
        ) as uninstall_windows_task_mock:
            result = uninstall_scheduler(Path("/tmp/repo"), "com.example.dropline")

        self.assertEqual(result, (True, True))
        uninstall_windows_task_mock.assert_called_once_with(Path("/tmp/repo"), "com.example.dropline")

    def test_render_windows_wrapper_script_uses_absolute_paths_and_logs(self) -> None:
        rendered = render_windows_wrapper_script(
            repo_root=Path("C:/repo").resolve(),
            python_executable="C:/Python312/python.exe",
            config_path=Path("C:/repo/config.yaml").resolve(),
            stdout_log_path=Path("C:/repo/runtime_logs/task.out.log").resolve(),
            stderr_log_path=Path("C:/repo/runtime_logs/task.err.log").resolve(),
        )

        self.assertIn('cd /d "', rendered)
        self.assertIn("-m dropline scheduler-tick --config", rendered)
        self.assertIn('>> "', rendered)
        self.assertIn('2>> "', rendered)

    def test_render_windows_wrapper_script_escapes_percent_in_paths(self) -> None:
        rendered = render_windows_wrapper_script(
            repo_root=Path("C:/repo%1"),
            python_executable="C:/Python312%2/python.exe",
            config_path=Path("C:/repo/config%3.yaml"),
            stdout_log_path=Path("C:/repo/runtime_logs/task.out.log"),
            stderr_log_path=Path("C:/repo/runtime_logs/task.err.log"),
        )

        self.assertIn("repo%%1", rendered)
        self.assertIn("Python312%%2", rendered)
        self.assertIn("config%%3.yaml", rendered)

    def test_build_schtasks_commands_include_expected_arguments(self) -> None:
        create_command = build_schtasks_create_command(
            task_name="com.example.dropline",
            wrapper_script_path=Path("C:/repo/runtime_logs/com.example.dropline.cmd"),
            interval_minutes=15,
        )
        delete_command = build_schtasks_delete_command(task_name="com.example.dropline")

        self.assertEqual(create_command[:6], ["schtasks", "/Create", "/SC", "MINUTE", "/MO", "15"])
        self.assertIn("cmd.exe /c", create_command[9])
        self.assertEqual(delete_command, ["schtasks", "/Delete", "/TN", "com.example.dropline", "/F"])

    def test_install_windows_task_writes_wrapper_script_during_no_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            paths = install_windows_task(
                repo_root=root,
                config_path=root / "config.yaml",
                task_name="com.example.dropline",
                python_executable="/usr/bin/python3",
                interval_minutes=10,
                no_load=True,
            )

            self.assertTrue(paths.wrapper_script_path.exists())
            content = paths.wrapper_script_path.read_text(encoding="utf-8")
            self.assertIn("dropline scheduler-tick", content)
            self.assertIn(str(root / "config.yaml"), content)
