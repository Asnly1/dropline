from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from dropline.models import NormalizedInboundMessage
from dropline.storage import SessionScope, StateStore

from tests.helpers import create_test_repo, load_test_settings


class StateStoreTests(unittest.TestCase):
    def test_initialize_creates_session_scoped_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()

            conn = sqlite3.connect(settings.storage.sqlite_path)
            kv_columns = {row[1] for row in conn.execute("PRAGMA table_info(kv_state)")}
            inbound_columns = {row[1] for row in conn.execute("PRAGMA table_info(inbound_messages)")}
            outbound_columns = {row[1] for row in conn.execute("PRAGMA table_info(outbound_messages)")}
            runs_columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
            assets_columns = {row[1] for row in conn.execute("PRAGMA table_info(assets)")}
            inbound_pk_columns = [
                row[1]
                for row in sorted(
                    conn.execute("PRAGMA table_info(inbound_messages)").fetchall(),
                    key=lambda row: row[5],
                )
                if row[5]
            ]
            conn.close()

            self.assertEqual(
                kv_columns,
                {"character_slug", "relationship_slug", "key", "value"},
            )
            self.assertTrue({"character_slug", "relationship_slug"}.issubset(inbound_columns))
            self.assertEqual(inbound_pk_columns, ["character_slug", "relationship_slug", "imap_uid"])
            self.assertTrue({"character_slug", "relationship_slug", "delivery_status"}.issubset(outbound_columns))
            self.assertTrue({"character_slug", "relationship_slug"}.issubset(runs_columns))
            self.assertTrue({"character_slug", "relationship_slug"}.issubset(assets_columns))

    def test_run_lock_rejects_second_lock_in_same_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()

            with store.acquire_lock():
                with self.assertRaisesRegex(RuntimeError, "Another pipeline run is already in progress"):
                    with store.acquire_lock():
                        pass

    def test_last_seen_imap_uid_does_not_fall_back_to_consumed_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            store.set_last_consumed_imap_uid(42)

            self.assertEqual(store.get_last_seen_imap_uid(), 0)

    def test_mark_stale_running_runs_failed_is_scoped_and_uses_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            settings = load_test_settings(root)
            store_a = StateStore(settings)
            store_a.initialize()
            store_b = StateStore(
                settings,
                scope=SessionScope(character_slug="keqing", relationship_slug="study-buddies"),
            )
            store_b.initialize()

            store_a.start_run("run-old", "2026-04-08T11:59:59+00:00")
            store_a.update_run_context("run-old", "reply", "batch-old")
            store_a.start_run("run-boundary", "2026-04-08T12:00:00+00:00")
            store_a.start_run("run-fresh", "2026-04-10T12:00:00+00:00")
            store_b.start_run("run-other-session", "2026-04-08T11:00:00+00:00")

            cutoff = datetime(2026, 4, 8, 12, 0, tzinfo=ZoneInfo("UTC"))
            finished_at = datetime(2026, 4, 15, 12, 0, tzinfo=ZoneInfo("UTC"))
            stale_runs = store_a.mark_stale_running_runs_failed(cutoff=cutoff, finished_at=finished_at)

            self.assertEqual([run.run_id for run in stale_runs], ["run-old"])
            with sqlite3.connect(settings.storage.sqlite_path) as conn:
                rows = {
                    row[0]: (row[1], row[2], row[3])
                    for row in conn.execute(
                        "SELECT run_id, status, finished_at, error_stage FROM runs ORDER BY run_id"
                    )
                }
            self.assertEqual(rows["run-old"], ("failed", "2026-04-15T12:00:00+00:00", "stale_running"))
            self.assertEqual(rows["run-boundary"], ("running", None, None))
            self.assertEqual(rows["run-fresh"], ("running", None, None))
            self.assertEqual(rows["run-other-session"], ("running", None, None))

    def test_session_scoped_state_does_not_leak_between_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            settings = load_test_settings(root)
            store_a = StateStore(settings)
            store_a.initialize()
            store_b = StateStore(
                settings,
                scope=SessionScope(character_slug="keqing", relationship_slug="study-buddies"),
            )

            store_a.set_last_consumed_imap_uid(11)
            store_a.set_last_seen_imap_uid(111)
            store_b.set_last_consumed_imap_uid(22)
            store_b.set_last_seen_imap_uid(222)

            inbound_a = NormalizedInboundMessage(
                message_id="<session-a@example.com>",
                imap_uid=101,
                from_email="user@example.com",
                subject="session-a",
                received_at="2026-04-01T12:00:00+00:00",
                body_text="a",
                body_html_present=False,
                attachments=[],
            )
            inbound_b = NormalizedInboundMessage(
                message_id="<session-b@example.com>",
                imap_uid=202,
                from_email="user@example.com",
                subject="session-b",
                received_at="2026-04-01T12:05:00+00:00",
                body_text="b",
                body_html_present=False,
                attachments=[],
            )
            duplicate_inbound_a = NormalizedInboundMessage(
                message_id="<session-a-duplicate@example.com>",
                imap_uid=101,
                from_email="user@example.com",
                subject="session-b-duplicate",
                received_at="2026-04-01T12:01:00+00:00",
                body_text="duplicate",
                body_html_present=False,
                attachments=[],
            )
            store_a.upsert_inbound_message(inbound_a, raw_path=root / "session-a.eml")
            store_b.upsert_inbound_message(duplicate_inbound_a, raw_path=root / "session-b-duplicate.eml")
            store_b.upsert_inbound_message(inbound_b, raw_path=root / "session-b.eml")

            store_a.record_outbound_message(
                message_id="<out-a@example.com>",
                sent_at="2026-04-01T13:00:00+00:00",
                to_email="user@example.com",
                subject="out-a",
                raw_path=root / "out-a.eml",
                image_path=None,
                run_id="run-a",
                batch_id="batch-a",
            )
            store_b.record_outbound_message(
                message_id="<out-b@example.com>",
                sent_at="2026-04-01T13:05:00+00:00",
                to_email="user@example.com",
                subject="out-b",
                raw_path=root / "out-b.eml",
                image_path=None,
                run_id="run-b",
                batch_id="batch-b",
            )

            store_a.start_run("run-a", "2026-04-01T13:00:00+00:00")
            store_a.finish_run("run-a", "success", "2026-04-01T13:01:00+00:00")
            store_b.start_run("run-b", "2026-04-01T13:05:00+00:00")
            store_b.finish_run("run-b", "failed", "2026-04-01T13:06:00+00:00", error_stage="stage2")

            store_a.record_asset(
                asset_id="asset-a",
                run_id="run-a",
                image_path=root / "asset-a.png",
                scene_card={"location": "A", "action": "alpha"},
                prompt_text="prompt-a",
                created_at="2026-04-01T13:00:30+00:00",
            )
            store_b.record_asset(
                asset_id="asset-b",
                run_id="run-b",
                image_path=root / "asset-b.png",
                scene_card={"location": "B", "action": "beta"},
                prompt_text="prompt-b",
                created_at="2026-04-01T13:05:30+00:00",
            )

            self.assertEqual(store_a.get_last_consumed_imap_uid(), 11)
            self.assertEqual(store_b.get_last_consumed_imap_uid(), 22)
            self.assertEqual(store_a.get_last_seen_imap_uid(), 111)
            self.assertEqual(store_b.get_last_seen_imap_uid(), 222)
            self.assertEqual([item.subject for item in store_a.list_unconsumed_inbound()], ["session-a"])
            self.assertEqual([item.subject for item in store_b.list_unconsumed_inbound()], ["session-b-duplicate", "session-b"])
            self.assertEqual(store_a.list_recent_outbound_messages(limit=1)[0].subject, "out-a")
            self.assertEqual(store_b.list_recent_outbound_messages(limit=1)[0].subject, "out-b")
            self.assertEqual(store_a.list_recent_runs(limit=1)[0].run_id, "run-a")
            self.assertEqual(store_b.list_recent_runs(limit=1)[0].run_id, "run-b")
            self.assertEqual(store_a.list_recent_assets(limit=1)[0].asset_id, "asset-a")
            self.assertEqual(store_b.list_recent_assets(limit=1)[0].asset_id, "asset-b")

    def test_pending_outbound_can_be_marked_sent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            raw_path = root / "pending.eml"
            raw_path.write_bytes(b"pending")

            store.record_pending_outbound_message(
                message_id="<pending@example.com>",
                sent_at="2026-04-01T13:00:00+00:00",
                to_email="user@example.com",
                subject="pending outbound",
                raw_path=raw_path,
                image_path=None,
                run_id="run-pending",
                batch_id="batch-pending",
            )

            pending = store.get_outbound_message_for_batch("batch-pending")
            self.assertIsNotNone(pending)
            self.assertEqual(pending.delivery_status, "pending")

            store.mark_outbound_message_sent(
                message_id="<pending@example.com>",
                sent_at="2026-04-01T13:01:00+00:00",
            )

            sent = store.get_outbound_message_for_batch("batch-pending")
            self.assertIsNotNone(sent)
            self.assertEqual(sent.delivery_status, "sent")
            self.assertEqual(sent.sent_at, "2026-04-01T13:01:00+00:00")

    def test_list_recent_scene_cards_returns_scoped_recent_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            settings = load_test_settings(root)
            store_a = StateStore(settings)
            store_a.initialize()
            store_b = StateStore(
                settings,
                scope=SessionScope(character_slug="keqing", relationship_slug="study-buddies"),
            )
            store_b.initialize()

            oldest_card = {"location": "玉京台案桌", "action": "夜里写信"}
            newer_card = {"location": "璃月港码头", "action": "查看货运记录"}
            scoped_out_card = {"location": "吃虎岩", "action": "买点心"}
            store_a.record_asset(
                asset_id="asset-oldest",
                run_id="run-oldest",
                image_path=root / "oldest.png",
                scene_card=oldest_card,
                prompt_text="oldest prompt",
                created_at="2026-04-01T10:00:00+00:00",
            )
            store_a.record_asset(
                asset_id="asset-newer",
                run_id="run-newer",
                image_path=root / "newer.png",
                scene_card=newer_card,
                prompt_text="newer prompt",
                created_at="2026-04-02T10:00:00+00:00",
            )
            store_b.record_asset(
                asset_id="asset-scoped-out",
                run_id="run-scoped-out",
                image_path=root / "scoped-out.png",
                scene_card=scoped_out_card,
                prompt_text="scoped out prompt",
                created_at="2026-04-03T10:00:00+00:00",
            )

            self.assertEqual(
                store_a.list_recent_scene_cards(limit=2),
                [
                    {"created_at": "2026-04-02T10:00:00+00:00", "scene_card": newer_card},
                    {"created_at": "2026-04-01T10:00:00+00:00", "scene_card": oldest_card},
                ],
            )
            self.assertEqual(
                store_b.list_recent_scene_cards(limit=1),
                [
                    {"created_at": "2026-04-03T10:00:00+00:00", "scene_card": scoped_out_card},
                ],
            )

    def test_delete_operations_are_session_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            settings = load_test_settings(root)
            store_a = StateStore(settings)
            store_a.initialize()
            store_b = StateStore(
                settings,
                scope=SessionScope(character_slug="keqing", relationship_slug="study-buddies"),
            )
            store_b.initialize()

            inbound_a_raw = settings.documents.session_archive_dir / "inbound-a.eml"
            inbound_a_raw.parent.mkdir(parents=True, exist_ok=True)
            inbound_a_raw.write_text("a", encoding="utf-8")
            inbound_a_normalized = settings.documents.session_archive_dir / "inbound-a.txt"
            inbound_a_normalized.write_text("a", encoding="utf-8")
            inbound_b_raw = root / "profiles" / "characters" / "keqing" / "sessions" / "study-buddies" / "archive" / "inbound-b.eml"
            inbound_b_raw.parent.mkdir(parents=True, exist_ok=True)
            inbound_b_raw.write_text("b", encoding="utf-8")
            store_a.upsert_inbound_message(
                NormalizedInboundMessage(
                    message_id="<in-a@example.com>",
                    imap_uid=10,
                    from_email="user@example.com",
                    subject="in-a",
                    received_at="2026-04-01T12:00:00+00:00",
                    body_text="a",
                    body_html_present=False,
                    attachments=[],
                ),
                raw_path=inbound_a_raw,
                normalized_text_path=inbound_a_normalized,
            )
            store_b.upsert_inbound_message(
                NormalizedInboundMessage(
                    message_id="<in-b@example.com>",
                    imap_uid=20,
                    from_email="user@example.com",
                    subject="in-b",
                    received_at="2026-04-01T12:05:00+00:00",
                    body_text="b",
                    body_html_present=False,
                    attachments=[],
                ),
                raw_path=inbound_b_raw,
            )

            outbound_a_raw = settings.documents.session_archive_dir / "out-a.eml"
            outbound_a_raw.write_text("out-a", encoding="utf-8")
            outbound_b_raw = root / "profiles" / "characters" / "keqing" / "sessions" / "study-buddies" / "archive" / "out-b.eml"
            outbound_b_raw.write_text("out-b", encoding="utf-8")
            store_a.record_outbound_message(
                message_id="<out-a@example.com>",
                sent_at="2026-04-01T13:00:00+00:00",
                to_email="user@example.com",
                subject="out-a",
                raw_path=outbound_a_raw,
                image_path=None,
                run_id="run-a",
                batch_id="batch-a",
            )
            store_b.record_outbound_message(
                message_id="<out-b@example.com>",
                sent_at="2026-04-01T13:05:00+00:00",
                to_email="user@example.com",
                subject="out-b",
                raw_path=outbound_b_raw,
                image_path=None,
                run_id="run-b",
                batch_id="batch-b",
            )

            self.assertTrue(store_a.delete_inbound_message(10))
            self.assertFalse(inbound_a_raw.exists())
            self.assertFalse(inbound_a_normalized.exists())
            self.assertEqual([item.imap_uid for item in store_a.list_unconsumed_inbound()], [])
            self.assertEqual([item.imap_uid for item in store_b.list_unconsumed_inbound()], [20])

            self.assertEqual(store_b.delete_all_inbound_messages(), 1)
            self.assertFalse(inbound_b_raw.exists())
            self.assertEqual(store_b.list_unconsumed_inbound(), [])

            self.assertTrue(store_a.delete_outbound_message("<out-a@example.com>"))
            self.assertFalse(outbound_a_raw.exists())
            self.assertEqual([item.message_id for item in store_a.list_recent_outbound_messages()], [])
            self.assertEqual([item.message_id for item in store_b.list_recent_outbound_messages()], ["<out-b@example.com>"])

            self.assertEqual(store_b.delete_all_outbound_messages(), 1)
            self.assertFalse(outbound_b_raw.exists())
            self.assertEqual(store_b.list_recent_outbound_messages(), [])

    def test_delete_inbound_refuses_path_outside_managed_roots_without_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(tmp_dir)
            outside_root = Path(outside_dir)
            create_test_repo(root)

            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()

            outside_raw = outside_root / "outside-inbound.eml"
            outside_raw.write_text("outside", encoding="utf-8")
            store.upsert_inbound_message(
                NormalizedInboundMessage(
                    message_id="<outside-inbound@example.com>",
                    imap_uid=900,
                    from_email="user@example.com",
                    subject="outside inbound",
                    received_at="2026-04-01T12:00:00+00:00",
                    body_text="hello",
                    body_html_present=False,
                    attachments=[],
                ),
                raw_path=outside_raw,
            )

            with self.assertRaisesRegex(RuntimeError, "Refusing to delete path outside managed roots"):
                store.delete_inbound_message(900)

            self.assertTrue(outside_raw.exists())
            self.assertEqual([item.imap_uid for item in store.list_unconsumed_inbound()], [900])

    def test_delete_session_records_removes_scoped_rows_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            store.set_kv("cursor", "1")
            store.set_last_consumed_imap_uid(33)

            inbound_raw = settings.documents.session_archive_dir / "session-inbound.eml"
            inbound_raw.parent.mkdir(parents=True, exist_ok=True)
            inbound_raw.write_text("inbound", encoding="utf-8")
            inbound_normalized = settings.documents.session_archive_dir / "session-inbound.txt"
            inbound_normalized.write_text("normalized", encoding="utf-8")
            store.upsert_inbound_message(
                NormalizedInboundMessage(
                    message_id="<session-in@example.com>",
                    imap_uid=33,
                    from_email="user@example.com",
                    subject="session inbound",
                    received_at="2026-04-01T12:00:00+00:00",
                    body_text="hello",
                    body_html_present=False,
                    attachments=[],
                ),
                raw_path=inbound_raw,
                normalized_text_path=inbound_normalized,
            )

            outbound_raw = settings.documents.session_archive_dir / "session-out.eml"
            outbound_raw.write_text("outbound", encoding="utf-8")
            store.record_outbound_message(
                message_id="<session-out@example.com>",
                sent_at="2026-04-01T13:00:00+00:00",
                to_email="user@example.com",
                subject="session outbound",
                raw_path=outbound_raw,
                image_path=None,
                run_id="run-33",
                batch_id="batch-33",
            )

            stage1_path = settings.documents.session_archive_dir / "stage1.json"
            stage1_path.write_text("{}", encoding="utf-8")
            stage2_path = settings.documents.session_archive_dir / "stage2.json"
            stage2_path.write_text("{}", encoding="utf-8")
            asset_path = settings.documents.session_archive_dir / "scene.png"
            asset_path.write_text("png", encoding="utf-8")
            store.start_run("run-33", "2026-04-01T13:00:00+00:00")
            store.finish_run(
                "run-33",
                "success",
                "2026-04-01T13:05:00+00:00",
                stage1_path=str(stage1_path),
                stage2_path=str(stage2_path),
            )
            store.record_asset(
                asset_id="asset-33",
                run_id="run-33",
                image_path=asset_path,
                scene_card={"visual_style": "二次元插画", "location": "璃月港", "action": "看夜景"},
                prompt_text="prompt",
                created_at="2026-04-01T13:02:00+00:00",
            )

            summary = store.delete_session_records()

            self.assertEqual(summary.kv_entries_deleted, 2)
            self.assertEqual(summary.inbound_messages_deleted, 1)
            self.assertEqual(summary.outbound_messages_deleted, 1)
            self.assertEqual(summary.runs_deleted, 1)
            self.assertEqual(summary.assets_deleted, 1)
            self.assertFalse(inbound_raw.exists())
            self.assertFalse(inbound_normalized.exists())
            self.assertFalse(outbound_raw.exists())
            self.assertFalse(stage1_path.exists())
            self.assertFalse(stage2_path.exists())
            self.assertFalse(asset_path.exists())
            self.assertEqual(store.list_unconsumed_inbound(), [])
            self.assertEqual(store.list_recent_outbound_messages(), [])
            self.assertEqual(store.list_recent_runs(), [])
            self.assertEqual(store.list_recent_assets(), [])
            self.assertIsNone(store.get_kv("cursor"))

    def test_delete_session_records_refuses_any_outside_path_before_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(tmp_dir)
            outside_root = Path(outside_dir)
            create_test_repo(root)

            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            store.set_kv("cursor", "1")

            safe_raw = settings.documents.session_archive_dir / "safe-inbound.eml"
            safe_raw.parent.mkdir(parents=True, exist_ok=True)
            safe_raw.write_text("safe", encoding="utf-8")
            store.upsert_inbound_message(
                NormalizedInboundMessage(
                    message_id="<safe-inbound@example.com>",
                    imap_uid=901,
                    from_email="user@example.com",
                    subject="safe inbound",
                    received_at="2026-04-01T12:00:00+00:00",
                    body_text="hello",
                    body_html_present=False,
                    attachments=[],
                ),
                raw_path=safe_raw,
            )

            outside_stage = outside_root / "outside-stage.json"
            outside_stage.write_text("{}", encoding="utf-8")
            store.start_run("run-outside", "2026-04-01T13:00:00+00:00")
            store.finish_run(
                "run-outside",
                "success",
                "2026-04-01T13:05:00+00:00",
                stage1_path=str(outside_stage),
            )

            with self.assertRaisesRegex(RuntimeError, "Refusing to delete path outside managed roots"):
                store.delete_session_records()

            self.assertTrue(safe_raw.exists())
            self.assertTrue(outside_stage.exists())
            self.assertEqual(store.get_kv("cursor"), "1")
            self.assertEqual([item.imap_uid for item in store.list_unconsumed_inbound()], [901])
            self.assertEqual([item.run_id for item in store.list_recent_runs()], ["run-outside"])

    def test_same_imap_uid_can_exist_in_multiple_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store_a = StateStore(settings)
            store_b = StateStore(settings, scope=SessionScope(character_slug="keqing", relationship_slug="study-buddies"))
            store_a.initialize()
            store_b.initialize()

            store_a.upsert_inbound_message(
                NormalizedInboundMessage(
                    message_id="<same-a@example.com>",
                    imap_uid=42,
                    from_email="user@example.com",
                    subject="session-a",
                    received_at="2026-04-01T12:00:00+00:00",
                    body_text="a",
                    body_html_present=False,
                    attachments=[],
                ),
                raw_path=root / "same-a.eml",
            )
            store_b.upsert_inbound_message(
                NormalizedInboundMessage(
                    message_id="<same-b@example.com>",
                    imap_uid=42,
                    from_email="user@example.com",
                    subject="session-b",
                    received_at="2026-04-01T12:05:00+00:00",
                    body_text="b",
                    body_html_present=False,
                    attachments=[],
                ),
                raw_path=root / "same-b.eml",
            )

            self.assertEqual([item.subject for item in store_a.list_unconsumed_inbound()], ["session-a"])
            self.assertEqual([item.subject for item in store_b.list_unconsumed_inbound()], ["session-b"])

    def test_upsert_inbound_updates_only_matching_session_and_uid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store_a = StateStore(settings)
            store_b = StateStore(settings, scope=SessionScope(character_slug="keqing", relationship_slug="study-buddies"))
            store_a.initialize()
            store_b.initialize()

            for store, subject in ((store_a, "session-a"), (store_b, "session-b")):
                store.upsert_inbound_message(
                    NormalizedInboundMessage(
                        message_id=f"<{subject}@example.com>",
                        imap_uid=42,
                        from_email="user@example.com",
                        subject=subject,
                        received_at="2026-04-01T12:00:00+00:00",
                        body_text=subject,
                        body_html_present=False,
                        attachments=[],
                    ),
                    raw_path=root / f"{subject}.eml",
                )

            store_b.upsert_inbound_message(
                NormalizedInboundMessage(
                    message_id="<session-b-updated@example.com>",
                    imap_uid=42,
                    from_email="user@example.com",
                    subject="session-b-updated",
                    received_at="2026-04-01T12:05:00+00:00",
                    body_text="updated",
                    body_html_present=False,
                    attachments=[],
                ),
                raw_path=root / "session-b-updated.eml",
            )

            self.assertEqual([item.subject for item in store_a.list_unconsumed_inbound()], ["session-a"])
            self.assertEqual([item.subject for item in store_b.list_unconsumed_inbound()], ["session-b-updated"])

    def test_connect_closes_connection_after_context_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()

            with store._connect() as conn:
                self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)

            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    def test_delete_paths_continues_after_individual_unlink_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()

            good_path = settings.documents.session_archive_dir / "good.txt"
            good_path.parent.mkdir(parents=True, exist_ok=True)
            good_path.write_text("good", encoding="utf-8")
            bad_path = settings.documents.session_archive_dir / "bad.txt"
            bad_path.write_text("bad", encoding="utf-8")

            original_unlink = Path.unlink

            def fake_unlink(self_path: Path, *args: object, **kwargs: object) -> None:
                if self_path == bad_path:
                    raise OSError("simulated unlink failure")
                return original_unlink(self_path, *args, **kwargs)

            with mock.patch.object(Path, "unlink", fake_unlink):
                store._delete_paths([bad_path, good_path])

            self.assertTrue(bad_path.exists())
            self.assertFalse(good_path.exists())

    def test_get_first_outbound_sent_at_uses_actual_chronological_order_across_dst(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()

            # "2026-11-01T01:30:00-07:00" (08:30 UTC) is lexically LARGER than
            # "2026-11-01T01:15:00-08:00" (09:15 UTC) but is actually the EARLIER instant
            # (US DST fall-back repeats the 1am hour). A plain string ORDER BY gets this
            # backwards.
            store.record_outbound_message(
                message_id="<pdt@example.com>",
                sent_at="2026-11-01T01:30:00-07:00",
                to_email="user@example.com",
                subject="pdt",
                raw_path=root / "pdt.eml",
                image_path=None,
                run_id="run-pdt",
                batch_id="batch-pdt",
            )
            store.record_outbound_message(
                message_id="<pst@example.com>",
                sent_at="2026-11-01T01:15:00-08:00",
                to_email="user@example.com",
                subject="pst",
                raw_path=root / "pst.eml",
                image_path=None,
                run_id="run-pst",
                batch_id="batch-pst",
            )

            first_sent_at = store.get_first_outbound_sent_at()
            self.assertEqual(first_sent_at, datetime.fromisoformat("2026-11-01T01:30:00-07:00"))

    def test_get_outbound_message_for_batch_uses_actual_chronological_order_across_dst(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()

            store.record_outbound_message(
                message_id="<batch-dst-pdt@example.com>",
                sent_at="2026-11-01T01:30:00-07:00",
                to_email="user@example.com",
                subject="pdt",
                raw_path=root / "pdt.eml",
                image_path=None,
                run_id="run-pdt",
                batch_id="batch-dst",
            )
            store.record_outbound_message(
                message_id="<batch-dst-pst@example.com>",
                sent_at="2026-11-01T01:15:00-08:00",
                to_email="user@example.com",
                subject="pst",
                raw_path=root / "pst.eml",
                image_path=None,
                run_id="run-pst",
                batch_id="batch-dst",
            )

            latest = store.get_outbound_message_for_batch("batch-dst")
            self.assertIsNotNone(latest)
            self.assertEqual(latest.message_id, "<batch-dst-pst@example.com>")

    def test_list_recent_runs_uses_actual_chronological_order_across_dst(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()

            store.start_run("run-pdt", "2026-11-01T01:30:00-07:00")
            store.start_run("run-pst", "2026-11-01T01:15:00-08:00")

            recent = store.list_recent_runs(limit=1)
            self.assertEqual([run.run_id for run in recent], ["run-pst"])

    def test_list_recent_outbound_messages_uses_actual_chronological_order_across_dst(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()

            store.record_outbound_message(
                message_id="<recent-pdt@example.com>",
                sent_at="2026-11-01T01:30:00-07:00",
                to_email="user@example.com",
                subject="pdt",
                raw_path=root / "recent-pdt.eml",
                image_path=None,
                run_id="run-pdt",
                batch_id="batch-recent-pdt",
            )
            store.record_outbound_message(
                message_id="<recent-pst@example.com>",
                sent_at="2026-11-01T01:15:00-08:00",
                to_email="user@example.com",
                subject="pst",
                raw_path=root / "recent-pst.eml",
                image_path=None,
                run_id="run-pst",
                batch_id="batch-recent-pst",
            )

            recent = store.list_recent_outbound_messages(limit=1)
            self.assertEqual([item.message_id for item in recent], ["<recent-pst@example.com>"])

    def test_list_recent_assets_uses_actual_chronological_order_across_dst(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)

            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()

            store.record_asset(
                asset_id="asset-pdt",
                run_id="run-pdt",
                image_path=root / "asset-pdt.png",
                scene_card={"location": "PDT"},
                prompt_text="prompt-pdt",
                created_at="2026-11-01T01:30:00-07:00",
            )
            store.record_asset(
                asset_id="asset-pst",
                run_id="run-pst",
                image_path=root / "asset-pst.png",
                scene_card={"location": "PST"},
                prompt_text="prompt-pst",
                created_at="2026-11-01T01:15:00-08:00",
            )

            recent = store.list_recent_assets(limit=1)
            self.assertEqual([item.asset_id for item in recent], ["asset-pst"])


if __name__ == "__main__":
    unittest.main()
