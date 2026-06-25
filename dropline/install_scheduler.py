from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .config import load_settings
from .scheduler_install import DEFAULT_SCHEDULER_TASK_NAME, install_scheduler
from .scheduler_support import resolve_python_executable


def install_scheduler_command(
    config_path: str = "config.yaml",
    *,
    task_name: str = DEFAULT_SCHEDULER_TASK_NAME,
    python_executable: str = sys.executable,
    no_load: bool = False,
) -> int:
    try:
        settings = load_settings(config_path, require_active_profile=False)
        resolved_python_executable = resolve_python_executable(python_executable)
        result = install_scheduler(
            settings,
            config_path=Path(config_path).resolve(),
            task_name=task_name,
            python_executable=resolved_python_executable,
            no_load=no_load,
        )
    except subprocess.CalledProcessError as exc:
        print(_format_subprocess_error(exc))
        return 1
    except (RuntimeError, ValueError) as exc:
        print(str(exc))
        return 1

    if result.platform == "macos":
        print(f"Wrote launchd plist to {result.repo_definition_path}")
        print(f"Installed launchd plist to {result.install_definition_path}")
    else:
        print(f"Wrote Windows scheduler wrapper to {result.helper_script_path}")
    print(f"Logs will be written to {result.logs_dir}")
    if result.loaded:
        if result.platform == "macos":
            print(f"Loaded and started launchd agent: {result.task_name}")
        else:
            print(f"Registered Windows scheduled task: {result.task_name}")
    return 0


def _format_subprocess_error(exc: subprocess.CalledProcessError) -> str:
    command = " ".join(str(item) for item in exc.cmd) if isinstance(exc.cmd, (list, tuple)) else str(exc.cmd)
    details = exc.stderr or exc.output or ""
    suffix = f": {details}" if details else ""
    return f"Scheduler install failed ({command}) with exit code {exc.returncode}{suffix}"
