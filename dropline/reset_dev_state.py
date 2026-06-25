from __future__ import annotations

import logging
from pathlib import Path
import shutil

from .config import Settings, load_settings
from .memory import MemoryManager
from .profile_manager import list_session_root_dirs
from .scheduler_install import DEFAULT_SCHEDULER_TASK_NAME, build_scheduler_runtime_paths, uninstall_scheduler
from .storage import StateStore
from .utils import resolve_path_within_roots


LOGGER = logging.getLogger(__name__)


def _remove_path(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
    return True


def _collect_repo_pycache_dirs(repo_root: Path) -> list[Path]:
    paths: list[Path] = []
    for path in sorted(repo_root.rglob("__pycache__")):
        if path.is_dir():
            paths.append(path)
    return paths


def _resolve_reset_delete_paths(settings: Settings, task_name: str) -> list[Path]:
    targets = [
        *list_session_root_dirs(settings.repo_root),
        build_scheduler_runtime_paths(settings.repo_root, task_name).logs_dir,
        settings.storage.sqlite_path,
        settings.repo_root / "pipeline.lock",
        *_collect_repo_pycache_dirs(settings.repo_root),
    ]
    resolved_paths: list[Path] = []
    seen: set[Path] = set()
    for target in targets:
        path = resolve_path_within_roots(target, (settings.repo_root,))
        if path in seen:
            continue
        seen.add(path)
        resolved_paths.append(path)
    return resolved_paths


def _unload_scheduler_task(settings: Settings, task_name: str) -> tuple[bool, bool]:
    try:
        return uninstall_scheduler(settings.repo_root, task_name)
    except RuntimeError:
        LOGGER.info("scheduler uninstall is not supported on this platform; skipping task unload.")
        return False, False


def reset_dev_state(settings: Settings, *, task_name: str = DEFAULT_SCHEDULER_TASK_NAME) -> dict[str, object]:
    removed_paths: list[str] = []
    skipped_paths: list[str] = []
    targets = _resolve_reset_delete_paths(settings, task_name)
    unload_attempted, definition_removed = _unload_scheduler_task(settings, task_name)

    for target in targets:
        if _remove_path(target):
            removed_paths.append(str(target))
        else:
            skipped_paths.append(str(target))

    if settings.active_profile is not None:
        store = StateStore(settings)
        store.initialize()
        MemoryManager(settings).bootstrap()

    return {
        "scheduler_unload_attempted": unload_attempted,
        "scheduler_definition_removed": definition_removed,
        "removed_paths": removed_paths,
        "skipped_paths": skipped_paths,
        "sqlite_path": str(settings.storage.sqlite_path),
    }


def reset_dev_state_command(
    config_path: str = "config.yaml",
    *,
    task_name: str = DEFAULT_SCHEDULER_TASK_NAME,
) -> int:
    settings = load_settings(config_path, require_active_profile=False)
    logging.basicConfig(
        level=getattr(logging, settings.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    result = reset_dev_state(settings, task_name=task_name)
    print(f"Reset complete. State reinitialized at {result['sqlite_path']}")
    print(f"scheduler unload attempted: {result['scheduler_unload_attempted']}")
    print(f"scheduler definition removed: {result['scheduler_definition_removed']}")
    if result["removed_paths"]:
        print("Removed:")
        for item in result["removed_paths"]:
            print(f"  - {item}")
    if result["skipped_paths"]:
        print("Skipped (missing):")
        for item in result["skipped_paths"]:
            print(f"  - {item}")
    return 0
