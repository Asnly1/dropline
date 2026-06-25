from __future__ import annotations

import logging
from datetime import timedelta

from .config import load_settings
from .storage import StateStore
from .utils import now_in_timezone


def mark_stale_runs_failed_command(
    config_path: str = "config.yaml",
    *,
    older_than_days: int = 7,
    dry_run: bool = False,
) -> int:
    settings = load_settings(config_path)
    logging.basicConfig(
        level=getattr(logging, settings.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    now = now_in_timezone(settings.project.timezone)
    cutoff = now - timedelta(days=older_than_days)
    store = StateStore(settings)
    store.initialize()
    stale_runs = store.list_stale_running_runs(cutoff=cutoff)
    if dry_run:
        print(f"Stale running runs candidate count: {len(stale_runs)}")
    else:
        stale_runs = store.mark_stale_running_runs_failed(cutoff=cutoff, finished_at=now)
        print(f"Marked stale running runs failed: {len(stale_runs)}")
    print(f"Scope: {store.scope.character_slug} / {store.scope.relationship_slug}")
    print(f"Older than days: {older_than_days}")
    print(f"Cutoff: {cutoff.isoformat()}")
    if not stale_runs:
        print("Runs: none")
        return 0
    print("Runs:")
    for run in stale_runs:
        print(f"  - run_id={run.run_id} started_at={run.started_at} mode={run.mode} batch_id={run.batch_id}")
    return 0
