from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .scheduler_support import DEFAULT_SCHEDULER_TASK_NAME, build_scheduler_paths, normalize_scheduler_task_name


DEFAULT_LAUNCHD_LABEL = DEFAULT_SCHEDULER_TASK_NAME


@dataclass(frozen=True, slots=True)
class LaunchdPaths:
    repo_plist_path: Path
    install_plist_path: Path
    logs_dir: Path
    stdout_log_path: Path
    stderr_log_path: Path


def build_launchd_paths(repo_root: Path, label: str) -> LaunchdPaths:
    safe_label = normalize_scheduler_task_name(label)
    scheduler_paths = build_scheduler_paths(repo_root, safe_label)
    repo_plist_path = repo_root / "launchd" / f"{safe_label}.plist"
    install_plist_path = Path.home() / "Library" / "LaunchAgents" / f"{safe_label}.plist"
    return LaunchdPaths(
        repo_plist_path=repo_plist_path,
        install_plist_path=install_plist_path,
        logs_dir=scheduler_paths.logs_dir,
        stdout_log_path=scheduler_paths.stdout_log_path,
        stderr_log_path=scheduler_paths.stderr_log_path,
    )


def build_launchd_plist(
    *,
    label: str,
    python_executable: str,
    repo_root: Path,
    config_path: Path,
    interval_seconds: int,
    stdout_log_path: Path,
    stderr_log_path: Path,
) -> dict[str, object]:
    return {
        "Label": label,
        "ProgramArguments": [
            python_executable,
            "-m",
            "dropline",
            "scheduler-tick",
            "--config",
            str(config_path),
        ],
        "WorkingDirectory": str(repo_root),
        "RunAtLoad": True,
        "StartInterval": max(interval_seconds, 60),
        "StandardOutPath": str(stdout_log_path),
        "StandardErrorPath": str(stderr_log_path),
    }


def write_launchd_plist(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)
    return path


def install_launchd_job(
    *,
    label: str,
    python_executable: str,
    repo_root: Path,
    config_path: Path,
    interval_minutes: int,
    no_load: bool = False,
) -> LaunchdPaths:
    safe_label = normalize_scheduler_task_name(label)
    paths = build_launchd_paths(repo_root, safe_label)
    payload = build_launchd_plist(
        label=safe_label,
        python_executable=str(Path(python_executable).resolve()),
        repo_root=repo_root,
        config_path=config_path,
        interval_seconds=interval_minutes * 60,
        stdout_log_path=paths.stdout_log_path,
        stderr_log_path=paths.stderr_log_path,
    )
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    write_launchd_plist(paths.repo_plist_path, payload)
    paths.install_plist_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(paths.repo_plist_path, paths.install_plist_path)

    if no_load:
        return paths

    user_domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", user_domain, str(paths.install_plist_path)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["launchctl", "bootstrap", user_domain, str(paths.install_plist_path)],
        check=True,
    )
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"{user_domain}/{safe_label}"],
        check=True,
    )
    return paths


def uninstall_launchd_job(repo_root: Path, label: str) -> tuple[bool, bool]:
    paths = build_launchd_paths(repo_root, label)
    user_domain = f"gui/{os.getuid()}"
    try:
        subprocess.run(
            ["launchctl", "bootout", user_domain, str(paths.install_plist_path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        unload_attempted = False
    else:
        unload_attempted = True

    if paths.install_plist_path.exists():
        paths.install_plist_path.unlink()
        definition_removed = True
    else:
        definition_removed = False
    return unload_attempted, definition_removed
