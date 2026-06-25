from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path


DEFAULT_SCHEDULER_TASK_NAME = "dropline"
TASK_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class SchedulerPaths:
    logs_dir: Path
    stdout_log_path: Path
    stderr_log_path: Path
    windows_wrapper_script_path: Path


def build_scheduler_paths(repo_root: Path, task_name: str) -> SchedulerPaths:
    safe_task_name = normalize_scheduler_task_name(task_name)
    logs_dir = repo_root / "runtime_logs"
    return SchedulerPaths(
        logs_dir=logs_dir,
        stdout_log_path=logs_dir / f"{safe_task_name}.out.log",
        stderr_log_path=logs_dir / f"{safe_task_name}.err.log",
        windows_wrapper_script_path=logs_dir / f"{safe_task_name}.cmd",
    )


def normalize_scheduler_task_name(task_name: str) -> str:
    safe_task_name = task_name.strip()
    if not safe_task_name:
        raise ValueError("scheduler task name must not be empty")
    if "/" in safe_task_name or "\\" in safe_task_name:
        raise ValueError("scheduler task name must not contain path separators")
    if ".." in safe_task_name:
        raise ValueError("scheduler task name must not contain '..'")
    if not TASK_NAME_RE.fullmatch(safe_task_name):
        raise ValueError("scheduler task name may only contain letters, numbers, dot, underscore, and hyphen")
    return safe_task_name


def resolve_python_executable(python_executable: str) -> str:
    value = python_executable.strip()
    if not value:
        raise ValueError("Python executable must not be empty")
    if "/" not in value and "\\" not in value and not Path(value).is_absolute():
        resolved = shutil.which(value)
        if resolved is None:
            raise ValueError(f"Python executable not found on PATH: {value}")
        return resolved
    return str(Path(value).expanduser().resolve())
