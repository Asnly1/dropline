from __future__ import annotations

import logging

from .config import load_settings
from .pipeline import PipelineService


def run_once_command(config_path: str = "config.yaml") -> int:
    settings = load_settings(config_path)
    logging.basicConfig(
        level=getattr(logging, settings.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        pipeline = PipelineService(settings)
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 1
    outcome = pipeline.run_once()
    if outcome.success:
        print(f"Run succeeded: {outcome.run_id} ({outcome.mode}, {outcome.batch_id})")
        return 0
    print(f"Run failed: {outcome.run_id} [{outcome.error_stage}] {outcome.message}")
    return 1
