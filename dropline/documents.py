from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def read_optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def read_required_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Required text document is missing: {path}")
    return path.read_text(encoding="utf-8")


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def list_json_files(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted(item for item in path.iterdir() if item.suffix == ".json")


def load_entries(path: Path) -> list[dict[str, Any]]:
    entries = load_json_file(path, default=[])
    if not isinstance(entries, list):
        raise ValueError(f"Memory file must contain a JSON array: {path}")
    return entries


def ensure_entries_file(path: Path) -> None:
    if not path.exists():
        write_json_file(path, [])
