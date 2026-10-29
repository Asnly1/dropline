from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dropline.launchd import build_launchd_paths, build_launchd_plist
from dropline.models import RunOutcome
from dropline.power import PowerContext
from dropline.scheduler import (
    SCHEDULED_FOR_DATE_KEY,
    SENT_TIME_KEY,
    TARGET_PICKED_AT_KEY,
    TARGET_SEND_TIME_KEY,
    pick_target_send_time,
    run_scheduler_tick,
)
from dropline.scheduler_tick import scheduler_tick_command
from dropline.storage import StateStore
from tests.helpers import create_test_repo, load_test_settings


class _FakePipeline:
    def __init__(self, store: StateStore, outcome: RunOutcome) -> None:
        self.dependencies = SimpleNamespace(state_store=store)
        self._outcome = outcome
        self.run_count = 0

    def run_once(self) -> RunOutcome:
        self.run_count += 1
        return self._outcome


def _write_schedule_window(root: Path, *, window_start: str, window_end: str) -> None:
    config_path = root / "config.yaml"
    payload = config_path.read_text(encoding="utf-8")
    payload = payload.replace('  window_start: "19:00"', f'  window_start: "{window_start}"')
    payload = payload.replace('  window_end: "23:59"', f'  window_end: "{window_end}"')
    config_path.write_text(payload, encoding="utf-8")


class SchedulerTests(unittest.TestCase):
    def test_scheduler_tick_command_logs_started_and_finished_for_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            pipeline = _FakePipeline(
                store,
                RunOutcome(success=True, run_id="run-skip", mode="initiative", batch_id="batch-skip"),
            )
            started_at = datetime(2026, 4, 5, 20, 8, tzinfo=ZoneInfo("America/Los_Angeles"))
            finished_at = started_at + timedelta(seconds=2)

            with patch(
                "dropline.scheduler_tick.load_settings",
                return_value=settings,
            ), patch(
                "dropline.scheduler_tick.PipelineService",
                return_value=pipeline,
            ), patch(
                "dropline.scheduler_tick.run_scheduler_tick",
                return_value=None,
            ), patch(
                "dropline.scheduler_tick.now_in_timezone",
                side_effect=[started_at, finished_at],
            ), patch(
                "dropline.scheduler_tick.time.perf_counter",
                side_effect=[10.0, 12.0],
            ), self.assertLogs("dropline.scheduler_tick", level="INFO") as captured:
                exit_code = scheduler_tick_command(str(root / "config.yaml"))

            self.assertEqual(exit_code, 0)
            output = "\n".join(captured.output)
            self.assertIn("tick_started", output)
            self.assertIn("tick_finished", output)
            self.assertIn("event=skipped", output)
            self.assertIn("duration_ms=2000", output)

    def test_scheduler_tick_command_logs_run_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            started_at = datetime(2026, 4, 5, 22, 31, tzinfo=ZoneInfo("America/Los_Angeles"))
            store.set_kv(SCHEDULED_FOR_DATE_KEY, started_at.date().isoformat())
            store.set_kv(TARGET_PICKED_AT_KEY, started_at.replace(hour=0, minute=5).isoformat())
            store.set_kv(TARGET_SEND_TIME_KEY, started_at.replace(hour=22, minute=30).isoformat())
            pipeline = _FakePipeline(
                store,
                RunOutcome(success=True, run_id="run-logged", mode="initiative", batch_id="batch-logged"),
            )

            def fake_tick(*args, **kwargs):
                store.set_kv(SENT_TIME_KEY, started_at.isoformat())
                return pipeline._outcome

            finished_at = started_at + timedelta(seconds=3)

            with patch(
                "dropline.scheduler_tick.load_settings",
                return_value=settings,
            ), patch(
                "dropline.scheduler_tick.PipelineService",
                return_value=pipeline,
            ), patch(
                "dropline.scheduler_tick.run_scheduler_tick",
                side_effect=fake_tick,
            ), patch(
                "dropline.scheduler_tick.now_in_timezone",
                side_effect=[started_at, finished_at],
            ), patch(
                "dropline.scheduler_tick.time.perf_counter",
                side_effect=[20.0, 23.0],
            ), self.assertLogs("dropline.scheduler_tick", level="INFO") as captured:
                exit_code = scheduler_tick_command(str(root / "config.yaml"))

            self.assertEqual(exit_code, 0)
            output = "\n".join(captured.output)
            self.assertIn("tick_started", output)
            self.assertIn("tick_finished", output)
            self.assertIn("event=run_succeeded", output)
            self.assertIn("run_id=run-logged", output)
            self.assertIn("mode=initiative", output)
            self.assertIn("batch_id=batch-logged", output)
            self.assertIn("duration_ms=3000", output)

    def test_scheduler_tick_command_skips_pipeline_during_darkwake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            started_at = datetime(2026, 4, 5, 22, 31, tzinfo=ZoneInfo("America/Los_Angeles"))
            finished_at = started_at + timedelta(seconds=1)

            with patch(
                "dropline.scheduler_tick.load_settings",
                return_value=settings,
            ), patch(
                "dropline.scheduler_tick.read_power_context",
                return_value=PowerContext(
                    clamshell_closed=True,
                    user_is_active=False,
                    user_triggered_full_wake=False,
                    wake_type="Network",
                ),
            ), patch(
                "dropline.scheduler_tick.PipelineService",
            ) as pipeline_service, patch(
                "dropline.scheduler_tick.run_scheduler_tick",
            ) as run_tick, patch(
                "dropline.scheduler_tick.now_in_timezone",
                side_effect=[started_at, finished_at],
            ), patch(
                "dropline.scheduler_tick.time.perf_counter",
                side_effect=[30.0, 31.0],
            ), self.assertLogs("dropline.scheduler_tick", level="INFO") as captured:
                exit_code = scheduler_tick_command(str(root / "config.yaml"))

            self.assertEqual(exit_code, 0)
            self.assertFalse(run_tick.called)
            pipeline_service.assert_not_called()
            output = "\n".join(captured.output)
            self.assertIn("event=skipped_darkwake", output)
            self.assertIn("power_state=clamshell_closed=yes user_is_active=no", output)

    def test_run_scheduler_tick_skips_before_daily_pick_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            pipeline = _FakePipeline(
                store,
                RunOutcome(success=True, run_id="run-1", mode="initiative", batch_id="batch-1"),
            )
            now = datetime(2026, 4, 2, 0, 4, tzinfo=ZoneInfo("America/Los_Angeles"))

            with patch("dropline.scheduler.pick_target_send_time", return_value=now.replace(hour=22, minute=0)):
                outcome = run_scheduler_tick(settings, pipeline, now=now)

            self.assertIsNone(outcome)
            self.assertEqual(pipeline.run_count, 0)
            self.assertFalse(store.get_kv(SCHEDULED_FOR_DATE_KEY))
            self.assertFalse(store.get_kv(TARGET_PICKED_AT_KEY))
            self.assertFalse(store.get_kv(TARGET_SEND_TIME_KEY))

    def test_run_scheduler_tick_picks_today_and_skips_until_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            pipeline = _FakePipeline(
                store,
                RunOutcome(success=True, run_id="run-2", mode="reply", batch_id="batch-2"),
            )
            now = datetime(2026, 4, 2, 21, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

            with patch("dropline.scheduler.pick_target_send_time", return_value=now.replace(hour=22, minute=30)):
                outcome = run_scheduler_tick(settings, pipeline, now=now)

            self.assertIsNone(outcome)
            self.assertEqual(pipeline.run_count, 0)
            self.assertEqual(store.get_kv(SCHEDULED_FOR_DATE_KEY), "2026-04-02")
            self.assertEqual(store.get_kv(TARGET_PICKED_AT_KEY), now.isoformat())
            self.assertEqual(store.get_kv(TARGET_SEND_TIME_KEY), now.replace(hour=22, minute=30).isoformat())
            self.assertFalse(store.get_kv(SENT_TIME_KEY))

    def test_run_scheduler_tick_runs_once_when_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            now = datetime(2026, 4, 2, 22, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
            store.set_kv(SCHEDULED_FOR_DATE_KEY, now.date().isoformat())
            store.set_kv(TARGET_PICKED_AT_KEY, now.replace(hour=0, minute=5).isoformat())
            store.set_kv(TARGET_SEND_TIME_KEY, now.replace(hour=21, minute=15).isoformat())
            pipeline = _FakePipeline(
                store,
                RunOutcome(success=True, run_id="run-3", mode="initiative", batch_id="batch-3"),
            )

            outcome = run_scheduler_tick(settings, pipeline, now=now)

            self.assertEqual(outcome, pipeline._outcome)
            self.assertEqual(pipeline.run_count, 1)
            self.assertEqual(store.get_kv(SENT_TIME_KEY), now.isoformat())

    def test_run_scheduler_tick_skips_after_send_time_if_already_sent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            now = datetime(2026, 4, 2, 22, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
            store.set_kv(SCHEDULED_FOR_DATE_KEY, now.date().isoformat())
            store.set_kv(TARGET_PICKED_AT_KEY, now.replace(hour=0, minute=5).isoformat())
            store.set_kv(TARGET_SEND_TIME_KEY, now.replace(hour=20, minute=0).isoformat())
            store.set_kv(SENT_TIME_KEY, now.replace(hour=21, minute=15).isoformat())
            pipeline = _FakePipeline(
                store,
                RunOutcome(success=True, run_id="run-4", mode="initiative", batch_id="batch-4"),
            )

            outcome = run_scheduler_tick(settings, pipeline, now=now)

            self.assertIsNone(outcome)
            self.assertEqual(pipeline.run_count, 0)

    def test_run_scheduler_tick_catches_up_yesterday_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            now = datetime(2026, 4, 5, 1, 3, tzinfo=ZoneInfo("America/Los_Angeles"))
            yesterday = now.date() - timedelta(days=1)
            store.set_kv(SCHEDULED_FOR_DATE_KEY, yesterday.isoformat())
            store.set_kv(TARGET_PICKED_AT_KEY, datetime(2026, 4, 4, 0, 28, tzinfo=now.tzinfo).isoformat())
            store.set_kv(TARGET_SEND_TIME_KEY, datetime(2026, 4, 4, 22, 32, tzinfo=now.tzinfo).isoformat())
            pipeline = _FakePipeline(
                store,
                RunOutcome(success=True, run_id="run-5", mode="initiative", batch_id="batch-5"),
            )

            outcome = run_scheduler_tick(settings, pipeline, now=now)

            self.assertEqual(outcome, pipeline._outcome)
            self.assertEqual(pipeline.run_count, 1)
            self.assertEqual(store.get_kv(SCHEDULED_FOR_DATE_KEY), yesterday.isoformat())
            self.assertEqual(store.get_kv(SENT_TIME_KEY), now.isoformat())

    def test_pick_target_send_time_supports_cross_midnight_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            _write_schedule_window(root, window_start="22:00", window_end="01:00")
            settings = load_test_settings(root)
            now = datetime(2026, 4, 2, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

            target = pick_target_send_time(settings, now)

        self.assertGreaterEqual(target, datetime(2026, 4, 2, 22, 0, tzinfo=now.tzinfo))
        self.assertLessEqual(target, datetime(2026, 4, 3, 1, 0, tzinfo=now.tzinfo))

    def test_run_scheduler_tick_runs_cross_midnight_previous_day_window_when_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            _write_schedule_window(root, window_start="22:00", window_end="01:00")
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            now = datetime(2026, 4, 3, 0, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
            scheduled_for_date = now.date() - timedelta(days=1)
            store.set_kv(SCHEDULED_FOR_DATE_KEY, scheduled_for_date.isoformat())
            store.set_kv(TARGET_PICKED_AT_KEY, datetime(2026, 4, 2, 0, 5, tzinfo=now.tzinfo).isoformat())
            store.set_kv(TARGET_SEND_TIME_KEY, now.isoformat())
            pipeline = _FakePipeline(
                store,
                RunOutcome(success=True, run_id="run-cross-midnight", mode="initiative", batch_id="batch-cross"),
            )

            outcome = run_scheduler_tick(settings, pipeline, now=now)

            self.assertEqual(outcome, pipeline._outcome)
            self.assertEqual(pipeline.run_count, 1)
            self.assertEqual(store.get_kv(SENT_TIME_KEY), now.isoformat())

    def test_run_scheduler_tick_skips_cross_midnight_previous_day_window_after_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            _write_schedule_window(root, window_start="22:00", window_end="01:00")
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            now = datetime(2026, 4, 3, 1, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
            scheduled_for_date = now.date() - timedelta(days=1)
            store.set_kv(SCHEDULED_FOR_DATE_KEY, scheduled_for_date.isoformat())
            store.set_kv(TARGET_PICKED_AT_KEY, datetime(2026, 4, 2, 0, 5, tzinfo=now.tzinfo).isoformat())
            store.set_kv(TARGET_SEND_TIME_KEY, datetime(2026, 4, 3, 0, 30, tzinfo=now.tzinfo).isoformat())
            pipeline = _FakePipeline(
                store,
                RunOutcome(success=True, run_id="run-too-late", mode="initiative", batch_id="batch-too-late"),
            )

            outcome = run_scheduler_tick(settings, pipeline, now=now)

            self.assertIsNone(outcome)
            self.assertEqual(pipeline.run_count, 0)
            self.assertFalse(store.get_kv(SENT_TIME_KEY))

    def test_run_scheduler_tick_picks_after_first_wakeup_past_pick_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            pipeline = _FakePipeline(
                store,
                RunOutcome(success=True, run_id="run-6", mode="initiative", batch_id="batch-6"),
            )
            now = datetime(2026, 4, 2, 21, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

            with patch("dropline.scheduler.pick_target_send_time", return_value=now.replace(hour=22, minute=30)):
                outcome = run_scheduler_tick(settings, pipeline, now=now)

            self.assertIsNone(outcome)
            self.assertEqual(pipeline.run_count, 0)
            self.assertEqual(store.get_kv(SCHEDULED_FOR_DATE_KEY), now.date().isoformat())
            self.assertEqual(store.get_kv(TARGET_PICKED_AT_KEY), now.isoformat())
            self.assertEqual(store.get_kv(TARGET_SEND_TIME_KEY), now.replace(hour=22, minute=30).isoformat())

    def test_build_launchd_plist_uses_absolute_paths(self) -> None:
        repo_root = Path("/tmp/dropline")
        paths = build_launchd_paths(repo_root, "com.example.dropline")
        payload = build_launchd_plist(
            label="com.example.dropline",
            python_executable="/usr/bin/python3",
            repo_root=repo_root,
            config_path=repo_root / "config.yaml",
            interval_seconds=1800,
            stdout_log_path=paths.stdout_log_path,
            stderr_log_path=paths.stderr_log_path,
        )

        self.assertEqual(payload["Label"], "com.example.dropline")
        self.assertEqual(
            payload["ProgramArguments"],
            [
                "/usr/bin/python3",
                "-m",
                "dropline",
                "scheduler-tick",
                "--config",
                "/tmp/dropline/config.yaml",
            ],
        )
        self.assertEqual(payload["WorkingDirectory"], "/tmp/dropline")
        self.assertEqual(payload["StartInterval"], 1800)
        self.assertTrue(str(payload["StandardOutPath"]).endswith("runtime_logs/com.example.dropline.out.log"))
        self.assertTrue(str(payload["StandardErrorPath"]).endswith("runtime_logs/com.example.dropline.err.log"))
