from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .scheduler_support import (
    build_scheduler_paths,
    normalize_scheduler_task_name,
    remove_file_if_exists,
    run_best_effort_uninstall_command,
)


@dataclass(frozen=True, slots=True)
class WindowsTaskPaths:
    task_name: str
    logs_dir: Path
    stdout_log_path: Path
    stderr_log_path: Path
    wrapper_script_path: Path


def build_windows_task_paths(repo_root: Path, task_name: str) -> WindowsTaskPaths:
    safe_task_name = normalize_scheduler_task_name(task_name)
    scheduler_paths = build_scheduler_paths(repo_root, safe_task_name)
    return WindowsTaskPaths(
        task_name=safe_task_name,
        logs_dir=scheduler_paths.logs_dir,
        stdout_log_path=scheduler_paths.stdout_log_path,
        stderr_log_path=scheduler_paths.stderr_log_path,
        wrapper_script_path=scheduler_paths.windows_wrapper_script_path,
    )


def _escape_cmd_percent(value: object) -> str:
    return str(value).replace("%", "%%")


def render_windows_wrapper_script(
    *,
    repo_root: Path,
    python_executable: str,
    config_path: Path,
    stdout_log_path: Path,
    stderr_log_path: Path,
) -> str:
    safe_repo_root = _escape_cmd_percent(repo_root)
    safe_python_executable = _escape_cmd_percent(Path(python_executable).resolve())
    safe_config_path = _escape_cmd_percent(config_path)
    safe_stdout_log_path = _escape_cmd_percent(stdout_log_path)
    safe_stderr_log_path = _escape_cmd_percent(stderr_log_path)
    return "\n".join(
        [
            "@echo off",
            f'cd /d "{safe_repo_root}"',
            "if errorlevel 1 exit /b %errorlevel%",
            (
                f'"{safe_python_executable}" -m dropline scheduler-tick --config "{safe_config_path}" '
                f'>> "{safe_stdout_log_path}" 2>> "{safe_stderr_log_path}"'
            ),
            "exit /b %errorlevel%",
            "",
        ]
    )


def build_schtasks_create_command(*, task_name: str, wrapper_script_path: Path, interval_minutes: int) -> list[str]:
    return [
        "schtasks",
        "/Create",
        "/SC",
        "MINUTE",
        "/MO",
        str(max(interval_minutes, 1)),
        "/TN",
        task_name,
        "/TR",
        f'cmd.exe /c "{wrapper_script_path}"',
        "/ST",
        "00:00",
        "/F",
    ]


def build_schtasks_delete_command(*, task_name: str) -> list[str]:
    return [
        "schtasks",
        "/Delete",
        "/TN",
        task_name,
        "/F",
    ]


def install_windows_task(
    *,
    repo_root: Path,
    config_path: Path,
    task_name: str,
    python_executable: str,
    interval_minutes: int,
    no_load: bool = False,
) -> WindowsTaskPaths:
    paths = build_windows_task_paths(repo_root, task_name)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    wrapper = render_windows_wrapper_script(
        repo_root=repo_root,
        python_executable=python_executable,
        config_path=config_path,
        stdout_log_path=paths.stdout_log_path,
        stderr_log_path=paths.stderr_log_path,
    )
    paths.wrapper_script_path.write_text(wrapper, encoding="utf-8", newline="\r\n")

    if no_load:
        return paths

    subprocess.run(
        build_schtasks_create_command(
            task_name=paths.task_name,
            wrapper_script_path=paths.wrapper_script_path,
            interval_minutes=interval_minutes,
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    return paths


def uninstall_windows_task(repo_root: Path, task_name: str) -> tuple[bool, bool]:
    paths = build_windows_task_paths(repo_root, task_name)
    unload_attempted, result = run_best_effort_uninstall_command(
        build_schtasks_delete_command(task_name=paths.task_name)
    )
    wrapper_removed = remove_file_if_exists(paths.wrapper_script_path)

    definition_removed = wrapper_removed
    if unload_attempted and result is not None and result.returncode == 0:
        definition_removed = True
    return unload_attempted, definition_removed
