from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from dropline.models import AttachmentMeta, NormalizedInboundMessage
from dropline.storage import RUNS_TODAY_KEY, SessionScope, StateStore

from tests.helpers import create_test_repo, load_test_settings


LEGACY_INBOUND_SCHEMA = """
CREATE TABLE kv_state (
    character_slug TEXT NOT NULL,
    relationship_slug TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY(character_slug, relationship_slug, key)
);
CREATE TABLE inbound_messages (
    imap_uid INTEGER NOT NULL,
    message_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    from_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    normalized_text_path TEXT NOT NULL,
    body_text TEXT NOT NULL,
    thread_message_id TEXT,
    consumed_batch_id TEXT,
    character_slug TEXT NOT NULL,
    relationship_slug TEXT NOT NULL,
    PRIMARY KEY(character_slug, relationship_slug, imap_uid)
);
CREATE INDEX idx_inbound_messages_scope_consumed
    ON inbound_messages(character_slug, relationship_slug, consumed_batch_id, imap_uid);
"""


def _inbound(message_id: str, imap_uid: int, *, uidvalidity: int = 0, subject: str = "", **overrides) -> NormalizedInboundMessage:
    fields = {
        "message_id": message_id,
        "imap_uid": imap_uid,
        "from_email": "user@example.com",
        "subject": subject or message_id,
        "received_at": "2026-04-01T12:00:00+00:00",
        "body_text": "hello",
        "body_html_present": False,
        "attachments": [],
        "uidvalidity": uidvalidity,
    }
    fields.update(overrides)
    return NormalizedInboundMessage(**fields)


def _primary_key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = sorted(conn.execute(f"PRAGMA table_info({table})").fetchall(), key=lambda row: row[5])
    return [row[1] for row in rows if row[5]]


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
            self.assertTrue(
                {
                    "uidvalidity",
                    "body_html_present",
                    "attachments_json",
                    "attempt_count",
                    "last_error",
                    "last_attempt_at",
                    "quarantined_at",
                }.issubset(inbound_columns)
            )
            self.assertEqual(inbound_pk_columns, ["character_slug", "relationship_slug", "uidvalidity", "imap_uid"])
            self.assertTrue({"character_slug", "relationship_slug", "delivery_status"}.issubset(outbound_columns))
            self.assertTrue({"character_slug", "relationship_slug"}.issubset(runs_columns))
            self.assertTrue({"character_slug", "relationship_slug"}.issubset(assets_columns))

    def test_initialize_migrates_legacy_inbound_schema_keeping_rows_and_extra_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)

            conn = sqlite3.connect(settings.storage.sqlite_path)
            conn.executescript(LEGACY_INBOUND_SCHEMA)
            # Columns another migration may have added with ALTER TABLE, plus one this code knows nothing about.
            conn.executescript(
                """
                ALTER TABLE inbound_messages ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE inbound_messages ADD COLUMN last_error TEXT;
                ALTER TABLE inbound_messages ADD COLUMN last_attempt_at TEXT;
                ALTER TABLE inbound_messages ADD COLUMN quarantined_at TEXT;
                ALTER TABLE inbound_messages ADD COLUMN operator_note TEXT NOT NULL DEFAULT 'none';
                """
            )
            legacy_rows = [
                (10, "<a@example.com>", "batch-a", 2, "boom", "keep", "keqing", "childhood-friends"),
                (11, "<b@example.com>", None, 0, None, "none", "keqing", "childhood-friends"),
                # The same letter stored again after being moved out of and back into INBOX (new UID).
                (12, "<b@example.com>", None, 1, "retry", "dup", "keqing", "childhood-friends"),
                (10, "<a@example.com>", None, 0, None, "none", "keqing", "study-buddies"),
            ]
            conn.executemany(
                """
                INSERT INTO inbound_messages(
                    imap_uid, message_id, received_at, from_email, subject, raw_path, normalized_text_path,
                    body_text, thread_message_id, consumed_batch_id, attempt_count, last_error, operator_note,
                    character_slug, relationship_slug
                )
                VALUES(?, ?, '2026-04-01T12:00:00+00:00', 'user@example.com', ?, '/tmp/raw.eml', '', 'body', NULL,
                       ?, ?, ?, ?, ?, ?)
                """,
                [
                    (uid, message_id, f"subject-{uid}", consumed, attempts, error, note, character, relationship)
                    for uid, message_id, consumed, attempts, error, note, character, relationship in legacy_rows
                ],
            )
            conn.execute(
                "INSERT INTO kv_state(character_slug, relationship_slug, key, value) "
                "VALUES('keqing', 'childhood-friends', 'last_seen_imap_uid', '12')"
            )
            conn.commit()
            conn.close()

            store = StateStore(settings)
            with self.assertLogs("dropline.storage", level="WARNING") as logs:
                store.initialize()
            store.initialize()  # idempotent: a second run must not migrate or drop anything again

            self.assertTrue(any("Migrating inbound_messages" in line for line in logs.output))
            self.assertTrue(any("duplicate inbound row" in line for line in logs.output))
            conn = sqlite3.connect(settings.storage.sqlite_path)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(inbound_messages)")}
            self.assertTrue(
                {"uidvalidity", "body_html_present", "attachments_json", "attempt_count", "last_error", "operator_note"}.issubset(
                    columns
                )
            )
            self.assertEqual(
                _primary_key_columns(conn, "inbound_messages"),
                ["character_slug", "relationship_slug", "uidvalidity", "imap_uid"],
            )
            self.assertEqual(
                conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'inbound_messages%'").fetchall(),
                [("inbound_messages",)],
            )
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_inbound_messages_scope_message_id'"
                ).fetchone()
            )
            rows = conn.execute(
                """
                SELECT relationship_slug, imap_uid, uidvalidity, message_id, consumed_batch_id, attempt_count,
                       last_error, operator_note, attachments_json, body_html_present
                FROM inbound_messages
                ORDER BY relationship_slug, imap_uid
                """
            ).fetchall()
            conn.close()
            self.assertEqual(
                rows,
                [
                    ("childhood-friends", 10, 0, "<a@example.com>", "batch-a", 2, "boom", "keep", "[]", 0),
                    ("childhood-friends", 11, 0, "<b@example.com>", None, 0, None, "none", "[]", 0),
                    ("study-buddies", 10, 0, "<a@example.com>", None, 0, None, "none", "[]", 0),
                ],
            )
            self.assertEqual(store.get_last_seen_imap_uid(), 12)
            unconsumed = store.list_unconsumed_inbound()
            self.assertEqual([(item.imap_uid, item.uidvalidity, item.attachments) for item in unconsumed], [(11, 0, [])])
            self.assertFalse(unconsumed[0].body_html_present)

            # The new primary key keeps the same UID apart across UIDVALIDITY epochs ...
            store.upsert_inbound_message(_inbound("<c@example.com>", 11, uidvalidity=7), raw_path=root / "c.eml")
            self.assertEqual(
                [(item.uidvalidity, item.imap_uid) for item in store.list_unconsumed_inbound()],
                [(0, 11), (7, 11)],
            )
            store.mark_inbound_consumed([(7, 11)], batch_id="batch-c")
            self.assertEqual([(item.uidvalidity, item.imap_uid) for item in store.list_unconsumed_inbound()], [(0, 11)])
            # ... while a Message-ID can only be stored once per session.
            with self.assertRaises(sqlite3.IntegrityError):
                store.upsert_inbound_message(_inbound("<b@example.com>", 99, uidvalidity=7), raw_path=root / "b2.eml")

    def test_upsert_inbound_round_trips_attachment_metadata_and_html_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            attachments = [
                AttachmentMeta(filename="notes.pdf", content_type="application/pdf", size=12),
                AttachmentMeta(filename="图.png", content_type="image/png", size=5, inline=True),
            ]

            store.upsert_inbound_message(
                _inbound("<with-files@example.com>", 3, uidvalidity=9, body_html_present=True, attachments=attachments),
                raw_path=root / "with-files.eml",
            )

            stored = store.list_unconsumed_inbound()
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0].attachments, attachments)
            self.assertTrue(stored[0].body_html_present)
            self.assertEqual(stored[0].uidvalidity, 9)
            self.assertTrue(store.has_inbound_message_id("<with-files@example.com>"))

            store.upsert_inbound_message(
                _inbound("<with-files@example.com>", 3, uidvalidity=9, body_text="updated"),
                raw_path=root / "with-files-2.eml",
            )

            stored = store.list_unconsumed_inbound()
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0].attachments, [])
            self.assertFalse(stored[0].body_html_present)
            self.assertEqual(stored[0].body_text, "updated")

    def test_has_inbound_message_id_is_session_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store_a = StateStore(settings)
            store_b = StateStore(settings, scope=SessionScope(character_slug="keqing", relationship_slug="study-buddies"))

            self.assertFalse(store_a.has_inbound_message_id("<shared@example.com>"))

            store_a.initialize()
            store_a.upsert_inbound_message(_inbound("<shared@example.com>", 1), raw_path=root / "shared.eml")

            self.assertTrue(store_a.has_inbound_message_id("<shared@example.com>"))
            self.assertFalse(store_a.has_inbound_message_id("<other@example.com>"))
            self.assertFalse(store_b.has_inbound_message_id("<shared@example.com>"))
            # Another session may legitimately store the same letter.
            store_b.upsert_inbound_message(_inbound("<shared@example.com>", 1), raw_path=root / "shared-b.eml")
            self.assertTrue(store_b.has_inbound_message_id("<shared@example.com>"))

    def test_inbound_uid_operations_are_scoped_by_uidvalidity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            old_raw = settings.documents.session_archive_dir / "old-epoch.eml"
            old_raw.parent.mkdir(parents=True, exist_ok=True)
            old_raw.write_text("old", encoding="utf-8")
            new_raw = settings.documents.session_archive_dir / "new-epoch.eml"
            new_raw.write_text("new", encoding="utf-8")
            store.upsert_inbound_message(_inbound("<old-epoch@example.com>", 5, uidvalidity=100), raw_path=old_raw)
            store.upsert_inbound_message(_inbound("<new-epoch@example.com>", 5, uidvalidity=200), raw_path=new_raw)
            store.upsert_inbound_message(_inbound("<old-later@example.com>", 9, uidvalidity=100), raw_path=root / "x.eml")

            # Oldest epoch first, then UID order inside the epoch.
            self.assertEqual(
                [(item.uidvalidity, item.imap_uid) for item in store.list_unconsumed_inbound()],
                [(100, 5), (100, 9), (200, 5)],
            )

            store.mark_inbound_consumed([(100, 9), (100, 404)], batch_id="batch-old")
            self.assertEqual(
                [(item.uidvalidity, item.imap_uid) for item in store.list_unconsumed_inbound()],
                [(100, 5), (200, 5)],
            )

            self.assertEqual(store.get_imap_uidvalidity(), 0)
            self.assertFalse(store.delete_inbound_message(5))  # no row in epoch 0
            store.set_imap_uidvalidity(200)
            self.assertEqual(store.get_imap_uidvalidity(), 200)
            self.assertTrue(store.delete_inbound_message(5))
            self.assertFalse(new_raw.exists())
            self.assertTrue(old_raw.exists())
            self.assertEqual([(item.uidvalidity, item.imap_uid) for item in store.list_unconsumed_inbound()], [(100, 5)])
            self.assertTrue(store.delete_inbound_message(5, uidvalidity=100))
            self.assertFalse(old_raw.exists())
            self.assertEqual(store.list_unconsumed_inbound(), [])

    def test_imap_uidvalidity_kv_is_session_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store_a = StateStore(settings)
            store_a.initialize()
            store_b = StateStore(settings, scope=SessionScope(character_slug="keqing", relationship_slug="study-buddies"))

            self.assertEqual(store_a.get_imap_uidvalidity(), 0)
            store_a.set_imap_uidvalidity(1700000000)
            self.assertEqual(store_a.get_imap_uidvalidity(), 1700000000)
            self.assertEqual(store_b.get_imap_uidvalidity(), 0)
            self.assertEqual(store_a.get_kv("imap_uidvalidity"), "1700000000")

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

    def _inbound(self, uid: int, subject: str) -> NormalizedInboundMessage:
        return NormalizedInboundMessage(
            message_id=f"<uid-{uid}@example.com>",
            imap_uid=uid,
            from_email="user@example.com",
            subject=subject,
            received_at="2026-04-01T12:00:00+00:00",
            body_text=subject,
            body_html_present=False,
            attachments=[],
        )

    def test_initialize_migrates_old_schema_in_place_and_keeps_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)

            conn = sqlite3.connect(settings.storage.sqlite_path)
            conn.executescript(
                """
                CREATE TABLE inbound_messages (
                    imap_uid INTEGER NOT NULL,
                    message_id TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    from_email TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    raw_path TEXT NOT NULL,
                    normalized_text_path TEXT NOT NULL,
                    body_text TEXT NOT NULL,
                    thread_message_id TEXT,
                    consumed_batch_id TEXT,
                    character_slug TEXT NOT NULL,
                    relationship_slug TEXT NOT NULL,
                    PRIMARY KEY(character_slug, relationship_slug, imap_uid)
                );
                CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY,
                    batch_id TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_stage TEXT,
                    stage1_path TEXT,
                    stage2_path TEXT,
                    character_slug TEXT NOT NULL,
                    relationship_slug TEXT NOT NULL
                );
                INSERT INTO inbound_messages VALUES (
                    5, '<old@example.com>', '2026-04-01T12:00:00+00:00', 'user@example.com', 'old subject',
                    '/tmp/old.eml', '', 'old body', NULL, NULL, 'keqing', 'childhood-friends'
                );
                INSERT INTO runs VALUES (
                    'run-old', 'batch-old', '2026-04-01T13:00:00+00:00', '2026-04-01T13:01:00+00:00', 'reply',
                    'failed', 'stage2', NULL, NULL, 'keqing', 'childhood-friends'
                );
                """
            )
            conn.commit()
            conn.close()

            store = StateStore(settings)
            store.initialize()
            store.initialize()

            conn = sqlite3.connect(settings.storage.sqlite_path)
            inbound_columns = {row[1] for row in conn.execute("PRAGMA table_info(inbound_messages)")}
            runs_columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
            conn.close()

            self.assertTrue({"attempt_count", "last_error", "last_attempt_at", "quarantined_at"}.issubset(inbound_columns))
            self.assertIn("error_message", runs_columns)
            messages = store.list_unconsumed_inbound()
            self.assertEqual([item.imap_uid for item in messages], [5])
            self.assertEqual(messages[0].subject, "old subject")
            self.assertEqual(messages[0].attempt_count, 0)
            self.assertIsNone(messages[0].last_error)
            self.assertIsNone(messages[0].quarantined_at)
            old_run = store.get_run("run-old")
            self.assertEqual(old_run.error_stage, "stage2")
            self.assertIsNone(old_run.error_message)

    def test_inbound_attempt_failures_quarantine_after_max_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            store.upsert_inbound_message(self._inbound(1, "first"), raw_path=root / "first.eml")
            store.upsert_inbound_message(self._inbound(2, "second"), raw_path=root / "second.eml")

            first_quarantined = store.record_inbound_attempt_failure(
                [1, 2],
                error_stage="stage2",
                message="boom",
                attempted_at="2026-04-01T20:00:00+00:00",
                max_attempts=2,
            )

            self.assertEqual(first_quarantined, [])
            self.assertEqual(
                [
                    (item.imap_uid, item.attempt_count, item.last_error, item.last_attempt_at, item.quarantined_at)
                    for item in store.list_unconsumed_inbound()
                ],
                [
                    (1, 1, "stage2: boom", "2026-04-01T20:00:00+00:00", None),
                    (2, 1, "stage2: boom", "2026-04-01T20:00:00+00:00", None),
                ],
            )

            second_quarantined = store.record_inbound_attempt_failure(
                [1],
                error_stage="validation",
                message="x" * 2000,
                attempted_at="2026-04-01T21:00:00+00:00",
                max_attempts=2,
            )

            self.assertEqual(second_quarantined, [1])
            self.assertEqual([item.imap_uid for item in store.list_unconsumed_inbound()], [2])
            self.assertEqual([item.imap_uid for item in store.list_unconsumed_inbound(include_quarantined=True)], [1, 2])
            self.assertEqual([item.imap_uid for item in store.list_quarantined_inbound()], [1])
            self.assertEqual(store.count_unconsumed_inbound(), 1)
            self.assertEqual(store.count_quarantined_inbound(), 1)
            quarantined = store.list_quarantined_inbound()[0]
            self.assertEqual(quarantined.attempt_count, 2)
            self.assertEqual(quarantined.quarantined_at, "2026-04-01T21:00:00+00:00")
            self.assertEqual(quarantined.last_attempt_at, "2026-04-01T21:00:00+00:00")
            self.assertTrue(quarantined.last_error.startswith("validation: xxx"))
            self.assertEqual(len(quarantined.last_error), 1000)

            self.assertEqual(
                store.record_inbound_attempt_failure(
                    [1],
                    error_stage="stage2",
                    message="again",
                    attempted_at="2026-04-01T22:00:00+00:00",
                    max_attempts=2,
                ),
                [],
            )
            self.assertEqual(store.list_quarantined_inbound()[0].quarantined_at, "2026-04-01T21:00:00+00:00")
            self.assertEqual(
                store.record_inbound_attempt_failure(
                    [],
                    error_stage="stage2",
                    message="noop",
                    attempted_at="2026-04-01T22:00:00+00:00",
                    max_attempts=2,
                ),
                [],
            )

    def test_requeue_inbound_message_clears_quarantine_and_counters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            store.upsert_inbound_message(self._inbound(1, "first"), raw_path=root / "first.eml")
            store.record_inbound_attempt_failure(
                [1],
                error_stage="stage2",
                message="boom",
                attempted_at="2026-04-01T20:00:00+00:00",
                max_attempts=1,
            )
            self.assertEqual(store.list_unconsumed_inbound(), [])

            self.assertTrue(store.requeue_inbound_message(1))
            self.assertFalse(store.requeue_inbound_message(99))

            messages = store.list_unconsumed_inbound()
            self.assertEqual([item.imap_uid for item in messages], [1])
            self.assertEqual(messages[0].attempt_count, 0)
            self.assertIsNone(messages[0].last_error)
            self.assertIsNone(messages[0].last_attempt_at)
            self.assertIsNone(messages[0].quarantined_at)
            self.assertEqual(store.count_quarantined_inbound(), 0)

    def test_delete_inbound_message_removes_quarantined_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            raw_path = settings.documents.session_archive_dir / "quarantined.eml"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text("raw", encoding="utf-8")
            store.upsert_inbound_message(self._inbound(1, "first"), raw_path=raw_path)
            store.record_inbound_attempt_failure(
                [1],
                error_stage="image",
                message="boom",
                attempted_at="2026-04-01T20:00:00+00:00",
                max_attempts=1,
            )
            self.assertEqual(store.count_quarantined_inbound(), 1)

            self.assertTrue(store.delete_inbound_message(1))

            self.assertFalse(raw_path.exists())
            self.assertEqual(store.count_quarantined_inbound(), 0)
            self.assertEqual(store.list_unconsumed_inbound(include_quarantined=True), [])

    def test_finish_run_stores_error_message_and_latest_run_lookups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            store.start_run("run-1", "2026-04-01T13:00:00+00:00")
            store.update_run_context("run-1", "reply", "batch-x")
            store.finish_run("run-1", "success", "2026-04-01T13:01:00+00:00")
            store.start_run("run-2", "2026-04-02T13:00:00+00:00")
            store.update_run_context("run-2", "reply", "batch-x")
            store.finish_run(
                "run-2",
                "failed",
                "2026-04-02T13:01:00+00:00",
                error_stage="stage2",
                error_message="boom",
                stage1_path="/archive/run-2/stage1.json",
            )
            store.start_run("run-3", "2026-04-03T13:00:00+00:00")
            store.update_run_context("run-3", "initiative", "batch-y")
            store.finish_run("run-3", "failed", "2026-04-03T13:01:00+00:00", error_stage="image")
            store.start_run("run-4", "2026-04-04T13:00:00+00:00")

            self.assertEqual(store.get_run("run-2").error_message, "boom")
            self.assertIsNone(store.get_run("run-1").error_message)
            self.assertEqual(store.get_latest_run(status="success").run_id, "run-1")
            self.assertEqual(store.get_latest_run(status="failed").run_id, "run-3")
            self.assertEqual(store.get_latest_run(status="running").run_id, "run-4")
            self.assertIsNone(store.get_latest_run(status="cancelled"))
            latest_for_batch = store.get_latest_failed_run_for_batch("batch-x")
            self.assertEqual(latest_for_batch.run_id, "run-2")
            self.assertEqual(latest_for_batch.stage1_path, "/archive/run-2/stage1.json")
            self.assertIsNone(store.get_latest_failed_run_for_batch("batch-none"))

            store.start_run("run-pdt", "2026-11-01T01:30:00-07:00")
            store.update_run_context("run-pdt", "reply", "batch-dst")
            store.finish_run("run-pdt", "failed", "2026-11-01T01:31:00-07:00", error_stage="stage2")
            store.start_run("run-pst", "2026-11-01T01:15:00-08:00")
            store.update_run_context("run-pst", "reply", "batch-dst")
            store.finish_run("run-pst", "failed", "2026-11-01T01:16:00-08:00", error_stage="stage2")

            self.assertEqual(store.get_latest_failed_run_for_batch("batch-dst").run_id, "run-pst")

    def test_mark_stale_running_runs_failed_records_error_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            store.start_run("run-stale", "2026-04-08T11:00:00+00:00")

            store.mark_stale_running_runs_failed(
                cutoff=datetime(2026, 4, 8, 12, 0, tzinfo=ZoneInfo("UTC")),
                finished_at=datetime(2026, 4, 8, 18, 0, tzinfo=ZoneInfo("UTC")),
            )

            run = store.get_run("run-stale")
            self.assertEqual(run.status, "failed")
            self.assertEqual(run.error_stage, "stale_running")
            self.assertIn("stale cutoff 2026-04-08T12:00:00+00:00", run.error_message)

    def test_failure_counters_and_runs_today_bookkeeping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()

            self.assertEqual(store.get_consecutive_failures(), 0)
            self.assertIsNone(store.get_last_failed_at())
            self.assertEqual(store.record_run_failure(failed_at="2026-04-05T20:00:00-07:00"), 1)
            self.assertEqual(store.record_run_failure(failed_at="2026-04-05T21:00:00-07:00"), 2)
            self.assertEqual(store.get_consecutive_failures(), 2)
            self.assertEqual(store.get_last_failed_at(), "2026-04-05T21:00:00-07:00")
            store.reset_run_failures()
            self.assertEqual(store.get_consecutive_failures(), 0)
            self.assertIsNone(store.get_last_failed_at())

            today = date(2026, 4, 5)
            self.assertEqual(store.get_runs_today(today), 0)
            self.assertEqual(store.increment_runs_today(today), 1)
            self.assertEqual(store.increment_runs_today(today), 2)
            self.assertEqual(store.get_kv(RUNS_TODAY_KEY), "2026-04-05:2")
            self.assertEqual(store.get_runs_today(date(2026, 4, 6)), 0)
            self.assertEqual(store.increment_runs_today(date(2026, 4, 6)), 1)
            self.assertEqual(store.get_runs_today(today), 0)
            store.set_kv(RUNS_TODAY_KEY, "garbage")
            self.assertEqual(store.get_runs_today(today), 0)

    def test_count_pending_outbound_counts_only_pending_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            store = StateStore(settings)
            store.initialize()
            store.record_pending_outbound_message(
                message_id="<pending@example.com>",
                sent_at="2026-04-01T13:00:00+00:00",
                to_email="user@example.com",
                subject="pending",
                raw_path=root / "pending.eml",
                image_path=None,
                run_id="run-pending",
                batch_id="batch-pending",
            )
            store.record_outbound_message(
                message_id="<sent@example.com>",
                sent_at="2026-04-01T13:05:00+00:00",
                to_email="user@example.com",
                subject="sent",
                raw_path=root / "sent.eml",
                image_path=None,
                run_id="run-sent",
                batch_id="batch-sent",
            )

            self.assertEqual(store.count_pending_outbound(), 1)
            store.mark_outbound_message_sent(message_id="<pending@example.com>", sent_at="2026-04-01T13:06:00+00:00")
            self.assertEqual(store.count_pending_outbound(), 0)


if __name__ == "__main__":
    unittest.main()
