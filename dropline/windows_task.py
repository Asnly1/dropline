from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .scheduler_support import build_scheduler_paths, normalize_scheduler_task_name


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


def render_windows_wrapper_script(
    *,
    repo_root: Path,
    python_executable: str,
    config_path: Path,
    stdout_log_path: Path,
    stderr_log_path: Path,
) -> str:
    return "\n".join(
        [
            "@echo off",
            f'cd /d "{repo_root}"',
            "if errorlevel 1 exit /b %errorlevel%",
            (
                f'"{Path(python_executable).resolve()}" -m dropline scheduler-tick --config "{config_path}" '
                f'>> "{stdout_log_path}" 2>> "{stderr_log_path}"'
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
    )
    return paths


def uninstall_windows_task(repo_root: Path, task_name: str) -> tuple[bool, bool]:
    paths = build_windows_task_paths(repo_root, task_name)
    try:
        result = subprocess.run(
            build_schtasks_delete_command(task_name=paths.task_name),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        unload_attempted = False
    else:
        unload_attempted = True

    wrapper_removed = False
    if paths.wrapper_script_path.exists():
        paths.wrapper_script_path.unlink()
        wrapper_removed = True

    definition_removed = wrapper_removed
    if unload_attempted and result.returncode == 0:
        definition_removed = True
    return unload_attempted, definition_removed
