from __future__ import annotations

import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from dropline.config import load_settings
from dropline.memory import MemoryManager
from dropline.reset_dev_state import reset_dev_state, reset_dev_state_command
from dropline.storage import SessionScope, StateStore

from tests.helpers import create_test_repo, load_test_settings


class ResetDevStateTests(unittest.TestCase):
    def test_reset_dev_state_refuses_sqlite_path_outside_repo_before_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            root = base / "repo"
            create_test_repo(root)
            outside_sqlite = base / "outside-state.db"
            outside_sqlite.write_text("external state", encoding="utf-8")
            config_path = root / "config.yaml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    'sqlite_path: "./state.db"',
                    f'sqlite_path: "{outside_sqlite}"',
                ),
                encoding="utf-8",
            )
            settings = load_test_settings(root)

            archive_file = settings.documents.session_archive_dir / "2026-04-05" / "run-1" / "stage1.json"
            archive_file.parent.mkdir(parents=True, exist_ok=True)
            archive_file.write_text("{}", encoding="utf-8")
            runtime_logs_dir = root / "runtime_logs"
            runtime_logs_dir.mkdir(parents=True, exist_ok=True)
            runtime_log = runtime_logs_dir / "agent.err.log"
            runtime_log.write_text("log", encoding="utf-8")
            pipeline_lock = root / "pipeline.lock"
            pipeline_lock.write_text("", encoding="utf-8")

            with patch("dropline.reset_dev_state.uninstall_scheduler", return_value=(True, True)) as uninstall_scheduler:
                with self.assertRaisesRegex(RuntimeError, "Refusing to delete path outside managed roots"):
                    reset_dev_state(settings, task_name="com.example.dropline")

            uninstall_scheduler.assert_not_called()
            self.assertTrue(outside_sqlite.exists())
            self.assertEqual(outside_sqlite.read_text(encoding="utf-8"), "external state")
            self.assertTrue(archive_file.exists())
            self.assertTrue(runtime_log.exists())
            self.assertTrue(pipeline_lock.exists())

    def test_reset_dev_state_clears_runtime_and_reinitializes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            session_meta_path = root / "profiles" / "characters" / "keqing" / "sessions" / "childhood-friends" / "meta.json"
            meta_payload = json.loads(session_meta_path.read_text(encoding="utf-8"))
            meta_payload["created_at"] = "2026-01-01T00:00:00+00:00"
            session_meta_path.write_text(json.dumps(meta_payload, ensure_ascii=False), encoding="utf-8")
            settings = load_test_settings(root)

            archive_file = settings.documents.session_archive_dir / "2026-04-05" / "run-1" / "stage1.json"
            archive_file.parent.mkdir(parents=True, exist_ok=True)
            archive_file.write_text("{}", encoding="utf-8")

            runtime_logs_dir = root / "runtime_logs"
            runtime_logs_dir.mkdir(parents=True, exist_ok=True)
            (runtime_logs_dir / "agent.err.log").write_text("log", encoding="utf-8")

            (settings.documents.user_episode_dir / "2026-04-05.json").write_text("[]", encoding="utf-8")
            settings.documents.active_arc.write_text("dirty", encoding="utf-8")
            (root / "pipeline.lock").write_text("", encoding="utf-8")

            store = StateStore(settings)
            store.initialize()
            store.set_kv("scheduled_for_date", "2026-04-05")

            pycache_dir = root / "dropline" / "__pycache__"
            pycache_dir.mkdir(parents=True, exist_ok=True)
            (pycache_dir / "x.pyc").write_bytes(b"cache")

            with patch("dropline.reset_dev_state.uninstall_scheduler", return_value=(True, True)) as uninstall_scheduler:
                result = reset_dev_state(settings, task_name="com.example.dropline")

            self.assertTrue(result["scheduler_unload_attempted"])
            self.assertTrue(result["scheduler_definition_removed"])
            self.assertTrue(result["reinitialized"])
            self.assertFalse(archive_file.exists())
            self.assertFalse(runtime_logs_dir.exists())
            self.assertFalse((root / "pipeline.lock").exists())
            self.assertFalse(pycache_dir.exists())
            self.assertTrue(settings.storage.sqlite_path.exists())
            self.assertTrue((root / "profiles" / "characters" / "keqing" / "sessions").exists())
            self.assertTrue(session_meta_path.exists())
            reloaded = load_settings(root / "config.yaml")
            self.assertIsNotNone(reloaded.active_profile)
            self.assertEqual(reloaded.active_profile.session.created_at, "2026-01-01T00:00:00+00:00")
            self.assertTrue(settings.documents.user_episode_dir.exists())
            self.assertTrue(settings.documents.character_episode_dir.exists())
            self.assertEqual(list(settings.documents.user_episode_dir.glob("*.json")), [])

            active_arc_text = settings.documents.active_arc.read_text(encoding="utf-8")
            self.assertEqual(active_arc_text, "[]\n")

            conn = sqlite3.connect(settings.storage.sqlite_path)
            row = conn.execute("SELECT value FROM kv_state WHERE key = 'scheduled_for_date'").fetchone()
            conn.close()
            self.assertIsNone(row)
            uninstall_scheduler.assert_called_once_with(settings.repo_root, "com.example.dropline")

    def test_reset_dev_state_skips_missing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            MemoryManager(settings).bootstrap()

            with patch("dropline.reset_dev_state.uninstall_scheduler", return_value=(False, False)):
                result = reset_dev_state(settings, task_name="com.example.dropline")

            self.assertIn(str(settings.repo_root / "pipeline.lock"), result["skipped_paths"])

    def test_reset_dev_state_does_not_reinitialize_without_active_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            (root / "profiles" / "active.json").unlink()
            settings = load_settings(root / "config.yaml", require_active_profile=False)
            StateStore(
                settings,
                scope=SessionScope(character_slug="keqing", relationship_slug="childhood-friends"),
            ).initialize()
            self.assertTrue(settings.storage.sqlite_path.exists())

            with patch("dropline.reset_dev_state.uninstall_scheduler", return_value=(False, False)):
                result = reset_dev_state(settings, task_name="com.example.dropline")

            self.assertFalse(result["reinitialized"])
            self.assertFalse(settings.storage.sqlite_path.exists())

    def test_reset_dev_state_command_reports_reinitialized_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            with patch("dropline.reset_dev_state.uninstall_scheduler", return_value=(False, False)):
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = reset_dev_state_command(str(root / "config.yaml"))

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("Reset complete. State reinitialized at", rendered)

    def test_reset_dev_state_command_repairs_session_removed_by_older_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            shutil.rmtree(root / "profiles" / "characters" / "keqing" / "sessions")
            self.assertIsNone(load_settings(root / "config.yaml", require_active_profile=False).active_profile)

            with patch("dropline.reset_dev_state.uninstall_scheduler", return_value=(False, False)):
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = reset_dev_state_command(str(root / "config.yaml"))

            self.assertEqual(exit_code, 0)
            self.assertIn("Reset complete. State reinitialized at", output.getvalue())
            session_meta = root / "profiles" / "characters" / "keqing" / "sessions" / "childhood-friends" / "meta.json"
            self.assertTrue(session_meta.exists())
            self.assertIsNotNone(load_settings(root / "config.yaml").active_profile)

    def test_reset_dev_state_command_reports_no_active_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            (root / "profiles" / "active.json").unlink()

            with patch("dropline.reset_dev_state.uninstall_scheduler", return_value=(False, False)):
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = reset_dev_state_command(str(root / "config.yaml"))

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("Reset complete. No active profile; state not reinitialized.", rendered)
            self.assertNotIn("State reinitialized at", rendered)
