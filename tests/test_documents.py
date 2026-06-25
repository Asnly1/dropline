from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dropline.documents import ensure_entries_file, load_entries, write_json_file


class MemoryEntriesFileTests(unittest.TestCase):
    def test_ensure_entries_file_creates_empty_array(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "active_arc.json"
            ensure_entries_file(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "[]\n")
            self.assertEqual(load_entries(path), [])

    def test_ensure_entries_file_keeps_existing_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "active_arc.json"
            write_json_file(path, [{"key": "entry-1"}])
            ensure_entries_file(path)
            self.assertEqual(load_entries(path), [{"key": "entry-1"}])

    def test_load_entries_rejects_non_array(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "active_arc.json"
            write_json_file(path, {"not": "a list"})
            with self.assertRaises(ValueError):
                load_entries(path)
