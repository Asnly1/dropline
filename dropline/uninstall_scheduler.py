from __future__ import annotations

from .config import load_settings
from .scheduler_install import DEFAULT_SCHEDULER_TASK_NAME, get_scheduler_platform, uninstall_scheduler


def uninstall_scheduler_command(
    config_path: str = "config.yaml",
    *,
    task_name: str = DEFAULT_SCHEDULER_TASK_NAME,
) -> int:
    try:
        settings = load_settings(config_path, require_active_profile=False)
        platform_name = get_scheduler_platform()
        unload_attempted, definition_removed = uninstall_scheduler(settings.repo_root, task_name)
    except (RuntimeError, ValueError) as exc:
        print(str(exc))
        return 1

    print(f"Scheduler platform: {platform_name}")
    print(f"Scheduler unload attempted: {unload_attempted}")
    print(f"Scheduler definition removed: {definition_removed}")
    return 0
