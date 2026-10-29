from __future__ import annotations

import tempfile
import unittest
from collections.abc import Callable
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import yaml

from dropline.alerts import (
    ALERT_KIND_FAILURES,
    ALERT_KIND_QUARANTINE,
    ALERT_SUBJECT_PREFIX,
    alert_already_sent_today,
    build_alert_email,
    last_alert_at_key,
    notify_run_failure,
    send_alert,
)
from dropline.config import Settings, load_settings
from dropline.storage import LAST_ALERT_AT_KEY, StateStore
from tests.helpers import create_test_repo, load_test_settings

FAILURES_ALERT_KEY = last_alert_at_key(ALERT_KIND_FAILURES)
QUARANTINE_ALERT_KEY = last_alert_at_key(ALERT_KIND_QUARANTINE)


class _RecordingOutboundClient:
    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.sent_messages: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> str:
        if self.should_fail:
            raise RuntimeError("SMTP down")
        self.sent_messages.append(message)
        return message["Message-ID"]


def _update_config(root: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    config_path = root / "config.yaml"
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    mutate(payload)
    config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _disable_alerts(payload: dict[str, Any]) -> None:
    payload["mail"]["alerts"] = {"enabled": False}


def _use_english(payload: dict[str, Any]) -> None:
    payload["llm"]["language"] = "English"


class AlertTests(unittest.TestCase):
    fixed_now = datetime(2026, 4, 5, 22, 30, tzinfo=ZoneInfo("America/Los_Angeles"))

    def _build(self, root: Path) -> tuple[Settings, StateStore]:
        settings = load_test_settings(root)
        store = StateStore(settings)
        store.initialize()
        return settings, store

    def _notify(
        self,
        settings: Settings,
        store: StateStore,
        client: _RecordingOutboundClient,
        *,
        consecutive_failures: int,
        quarantined_uids: list[int],
        error_stage: str = "stage2",
    ) -> bool:
        return notify_run_failure(
            settings,
            store,
            run_id="run-alert",
            mode="reply",
            batch_id="batch-alert",
            error_stage=error_stage,
            message="boom",
            consecutive_failures=consecutive_failures,
            quarantined_uids=quarantined_uids,
            now=self.fixed_now,
            outbound_client=client,
        )

    def test_build_alert_email_uses_dropline_sender_name_and_subject_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings, _store = self._build(root)

            message = build_alert_email(settings, subject="something broke", body="plain body")
            prefixed = build_alert_email(settings, subject=f"{ALERT_SUBJECT_PREFIX}already", body="plain body")

        self.assertEqual(message["From"], "dropline <character@example.com>")
        self.assertEqual(message["To"], "user@example.com")
        self.assertEqual(message["Subject"], "[dropline] something broke")
        self.assertTrue(message["Message-ID"])
        self.assertEqual(message.get_content_type(), "text/plain")
        self.assertIn("plain body", message.get_content())
        self.assertEqual(prefixed["Subject"], "[dropline] already")

    def test_send_alert_records_last_alert_and_limits_to_one_per_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings, store = self._build(root)
            client = _RecordingOutboundClient()

            first = send_alert(settings, store, subject="first", body="b", now=self.fixed_now, outbound_client=client)
            self.assertTrue(first)
            self.assertEqual(store.get_kv(FAILURES_ALERT_KEY), self.fixed_now.isoformat())
            self.assertIsNone(store.get_kv(QUARANTINE_ALERT_KEY))

            same_day = send_alert(
                settings,
                store,
                subject="second",
                body="b",
                now=self.fixed_now + timedelta(hours=1),
                outbound_client=client,
            )
            self.assertFalse(same_day)
            self.assertEqual(len(client.sent_messages), 1)

            # The throttle is per kind: a quarantine notice still goes out on a day that saw a failure notice.
            other_kind = send_alert(
                settings,
                store,
                subject="quarantine",
                body="b",
                now=self.fixed_now + timedelta(hours=1),
                outbound_client=client,
                kind=ALERT_KIND_QUARANTINE,
            )
            self.assertTrue(other_kind)
            self.assertEqual(store.get_kv(QUARANTINE_ALERT_KEY), (self.fixed_now + timedelta(hours=1)).isoformat())
            self.assertEqual(store.get_kv(FAILURES_ALERT_KEY), self.fixed_now.isoformat())

            next_day = send_alert(
                settings,
                store,
                subject="third",
                body="b",
                now=self.fixed_now + timedelta(days=1),
                outbound_client=client,
            )
            self.assertTrue(next_day)
            self.assertEqual(
                [item["Subject"] for item in client.sent_messages],
                ["[dropline] first", "[dropline] quarantine", "[dropline] third"],
            )

    def test_send_alert_respects_disabled_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            _update_config(root, _disable_alerts)
            settings = load_settings(root / "config.yaml")
            store = StateStore(settings)
            store.initialize()
            client = _RecordingOutboundClient()

            sent = send_alert(settings, store, subject="x", body="b", now=self.fixed_now, outbound_client=client)

            self.assertFalse(sent)
            self.assertEqual(client.sent_messages, [])
            self.assertIsNone(store.get_kv(FAILURES_ALERT_KEY))

    def test_send_alert_failure_is_logged_and_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings, store = self._build(root)
            client = _RecordingOutboundClient(should_fail=True)

            with self.assertLogs("dropline.alerts", level="ERROR") as captured:
                sent = send_alert(settings, store, subject="x", body="b", now=self.fixed_now, outbound_client=client)

            self.assertFalse(sent)
            self.assertIsNone(store.get_kv(FAILURES_ALERT_KEY))
            self.assertIn("Failed to send alert email", "\n".join(captured.output))

    def test_send_alert_defaults_to_smtp_client_of_character_mailbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings, store = self._build(root)

            with patch("dropline.alerts.SMTPOutboundClient") as smtp_client_cls:
                smtp_client_cls.return_value.send.return_value = "<alert@example.com>"
                sent = send_alert(settings, store, subject="x", body="b", now=self.fixed_now)

            self.assertTrue(sent)
            smtp_client_cls.assert_called_once_with(settings.mail.character_mailbox)
            message = smtp_client_cls.return_value.send.call_args.args[0]
            self.assertEqual(message["From"], "dropline <character@example.com>")
            self.assertEqual(message["Subject"], "[dropline] x")

    def test_notify_run_failure_below_threshold_without_quarantine_sends_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings, store = self._build(root)
            client = _RecordingOutboundClient()

            sent = self._notify(settings, store, client, consecutive_failures=1, quarantined_uids=[])

            self.assertFalse(sent)
            self.assertEqual(client.sent_messages, [])
            self.assertIsNone(store.get_kv(FAILURES_ALERT_KEY))

    def test_notify_run_failure_at_threshold_sends_chinese_system_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings, store = self._build(root)
            client = _RecordingOutboundClient()

            sent = self._notify(settings, store, client, consecutive_failures=2, quarantined_uids=[])

            self.assertTrue(sent)
            self.assertEqual(len(client.sent_messages), 1)
            message = client.sent_messages[0]
            body = message.get_content()
            self.assertEqual(message["From"], "dropline <character@example.com>")
            self.assertEqual(message["Subject"], "[dropline] 已连续失败 2 次（阶段 stage2）")
            self.assertIn("系统通知，不是角色来信", body)
            self.assertIn("失败阶段：stage2", body)
            self.assertIn("错误信息：boom", body)
            self.assertIn("连续失败次数：2（告警阈值 2）", body)
            self.assertIn("runtime_logs/", body)
            self.assertIn("dropline status", body)
            self.assertIn("requeue-inbound", body)
            self.assertNotIn("已隔离", body)

    def test_notify_run_failure_with_quarantine_sends_english_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            _update_config(root, _use_english)
            settings = load_settings(root / "config.yaml")
            store = StateStore(settings)
            store.initialize()
            client = _RecordingOutboundClient()

            sent = self._notify(
                settings,
                store,
                client,
                consecutive_failures=1,
                quarantined_uids=[7, 9],
                error_stage="validation",
            )

            self.assertTrue(sent)
            message = client.sent_messages[0]
            body = message.get_content()
            self.assertEqual(message["Subject"], "[dropline] Inbound mail quarantined: uid 7, 9 (stage validation)")
            self.assertIn("automatic system notification from dropline", body)
            self.assertIn("It is not a letter from the character.", body)
            self.assertIn("Session: keqing / childhood-friends", body)
            self.assertIn("Failed stage: validation", body)
            self.assertIn("Quarantined inbound UID(s): 7, 9", body)
            self.assertIn("requeue-inbound --uid", body)
            self.assertIn("dropline status", body)

    def test_alert_already_sent_today_handles_blank_malformed_and_foreign_timezones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            _settings, store = self._build(root)

            self.assertFalse(alert_already_sent_today(store, self.fixed_now))
            store.set_kv(FAILURES_ALERT_KEY, "")
            self.assertFalse(alert_already_sent_today(store, self.fixed_now))
            store.set_kv(FAILURES_ALERT_KEY, "not-a-timestamp")
            self.assertFalse(alert_already_sent_today(store, self.fixed_now))
            store.set_kv(FAILURES_ALERT_KEY, "2026-04-05T08:00:00")
            self.assertTrue(alert_already_sent_today(store, self.fixed_now))
            self.assertTrue(alert_already_sent_today(store, self.fixed_now, kind=ALERT_KIND_FAILURES))
            self.assertFalse(alert_already_sent_today(store, self.fixed_now, kind=ALERT_KIND_QUARANTINE))
            # 2026-04-06T05:00:00+00:00 is still 2026-04-05 22:00 in Los Angeles.
            store.set_kv(FAILURES_ALERT_KEY, "2026-04-06T05:00:00+00:00")
            self.assertTrue(alert_already_sent_today(store, self.fixed_now))
            store.set_kv(FAILURES_ALERT_KEY, "2026-04-04T23:59:00-07:00")
            self.assertFalse(alert_already_sent_today(store, self.fixed_now))

    def test_last_alert_at_key_is_per_kind_and_rejects_unknown_kinds(self) -> None:
        self.assertEqual(FAILURES_ALERT_KEY, f"{LAST_ALERT_AT_KEY}:failures")
        self.assertEqual(QUARANTINE_ALERT_KEY, f"{LAST_ALERT_AT_KEY}:quarantine")
        with self.assertRaises(ValueError):
            last_alert_at_key("other")

    def test_quarantine_alert_is_not_throttled_by_same_day_failure_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings, store = self._build(root)
            client = _RecordingOutboundClient()

            self.assertTrue(self._notify(settings, store, client, consecutive_failures=2, quarantined_uids=[]))
            self.assertTrue(self._notify(settings, store, client, consecutive_failures=3, quarantined_uids=[1]))
            # One more of each kind on the same day is throttled.
            self.assertFalse(self._notify(settings, store, client, consecutive_failures=4, quarantined_uids=[]))
            self.assertFalse(self._notify(settings, store, client, consecutive_failures=5, quarantined_uids=[2]))

            self.assertEqual(
                [item["Subject"] for item in client.sent_messages],
                ["[dropline] 已连续失败 2 次（阶段 stage2）", "[dropline] 来信已被隔离：uid 1（阶段 stage2）"],
            )
            self.assertEqual(store.get_kv(FAILURES_ALERT_KEY), self.fixed_now.isoformat())
            self.assertEqual(store.get_kv(QUARANTINE_ALERT_KEY), self.fixed_now.isoformat())


if __name__ == "__main__":
    unittest.main()
