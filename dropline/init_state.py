from __future__ import annotations

import logging

from .config import load_settings
from .memory import MemoryManager
from .storage import StateStore


def initialize_state(config_path: str = "config.yaml") -> int:
    settings = load_settings(config_path)
    logging.basicConfig(
        level=getattr(logging, settings.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    store = StateStore(settings)
    store.initialize()
    MemoryManager(settings).bootstrap()
    print(f"Initialized state at {settings.storage.sqlite_path}")
    return 0
