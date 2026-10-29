from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .launchd import install_launchd_job, uninstall_launchd_job
from .scheduler_support import (
    DEFAULT_SCHEDULER_TASK_NAME,
    build_scheduler_paths,
    normalize_scheduler_task_name,
)
from .windows_task import install_windows_task, uninstall_windows_task


@dataclass(frozen=True, slots=True)
class SchedulerInstallResult:
    platform: str
    task_name: str
    loaded: bool
    logs_dir: Path
    stdout_log_path: Path
    stderr_log_path: Path
    repo_definition_path: Path | None = None
    install_definition_path: Path | None = None
    helper_script_path: Path | None = None


def get_scheduler_platform() -> str:
    system = platform.system()
    if system == "Darwin":
        return "macos"
    if system == "Windows":
        return "windows"
    raise RuntimeError("scheduler install is only supported on macOS and Windows")


def install_scheduler(
    settings: Settings,
    *,
    config_path: Path,
    task_name: str = DEFAULT_SCHEDULER_TASK_NAME,
    python_executable: str,
    no_load: bool = False,
) -> SchedulerInstallResult:
    task_name = normalize_scheduler_task_name(task_name)
    platform_name = get_scheduler_platform()
    if platform_name == "macos":
        paths = install_launchd_job(
            label=task_name,
            python_executable=python_executable,
            repo_root=settings.repo_root,
            config_path=config_path,
            interval_minutes=settings.schedule.check_every_minutes,
            no_load=no_load,
        )
        return SchedulerInstallResult(
            platform="macos",
            task_name=task_name,
            loaded=not no_load,
            logs_dir=paths.logs_dir,
            stdout_log_path=paths.stdout_log_path,
            stderr_log_path=paths.stderr_log_path,
            repo_definition_path=paths.repo_plist_path,
            install_definition_path=paths.install_plist_path,
        )

    paths = install_windows_task(
        repo_root=settings.repo_root,
        config_path=config_path,
        task_name=task_name,
        python_executable=python_executable,
        interval_minutes=settings.schedule.check_every_minutes,
        no_load=no_load,
    )
    return SchedulerInstallResult(
        platform="windows",
        task_name=task_name,
        loaded=not no_load,
        logs_dir=paths.logs_dir,
        stdout_log_path=paths.stdout_log_path,
        stderr_log_path=paths.stderr_log_path,
        helper_script_path=paths.wrapper_script_path,
    )


def uninstall_scheduler(repo_root: Path, task_name: str = DEFAULT_SCHEDULER_TASK_NAME) -> tuple[bool, bool]:
    platform_name = get_scheduler_platform()
    if platform_name == "macos":
        return uninstall_launchd_job(repo_root, task_name)
    return uninstall_windows_task(repo_root, task_name)


def build_scheduler_runtime_paths(repo_root: Path, task_name: str = DEFAULT_SCHEDULER_TASK_NAME):
    return build_scheduler_paths(repo_root, task_name)
