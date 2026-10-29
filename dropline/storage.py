from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date as dt_date
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings, ensure_active_profile
from .models import (
    AttachmentMeta,
    GeneratedImage,
    NormalizedInboundMessage,
    StoredAsset,
    StoredInboundMessage,
    StoredOutboundMessage,
    StoredRun,
)
from .profile_manager import session_archive_dir, session_profile_dir
from .utils import ensure_parent, resolve_path_within_roots


OUTBOUND_PENDING = "pending"
OUTBOUND_SENT = "sent"
INBOUND_MESSAGE_ID_INDEX = "idx_inbound_messages_scope_message_id"
# Shared by the live schema and the legacy-table rebuild so both always agree on the column set.
# attempt_count / last_error / last_attempt_at / quarantined_at are owned by the failure-handling
# code path; they are declared here so a rebuild never drops them and ALTER TABLE never re-adds them.
INBOUND_MESSAGES_COLUMNS_SQL = """
                imap_uid INTEGER NOT NULL,
                uidvalidity INTEGER NOT NULL DEFAULT 0,
                message_id TEXT NOT NULL,
                received_at TEXT NOT NULL,
                from_email TEXT NOT NULL,
                subject TEXT NOT NULL,
                raw_path TEXT NOT NULL,
                normalized_text_path TEXT NOT NULL,
                body_text TEXT NOT NULL,
                body_html_present INTEGER NOT NULL DEFAULT 0,
                attachments_json TEXT NOT NULL DEFAULT '[]',
                thread_message_id TEXT,
                consumed_batch_id TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                last_attempt_at TEXT,
                quarantined_at TEXT,
                character_slug TEXT NOT NULL,
                relationship_slug TEXT NOT NULL,
                PRIMARY KEY(character_slug, relationship_slug, uidvalidity, imap_uid)
"""

CONSECUTIVE_FAILURES_KEY = "consecutive_failures"
LAST_FAILED_AT_KEY = "last_failed_at"
RUNS_TODAY_KEY = "runs_today"
LAST_ALERT_AT_KEY = "last_alert_at"
INBOUND_LAST_ERROR_MAX_CHARS = 1000
# Columns added after the first release; applied with ALTER TABLE so existing databases keep their rows.
INBOUND_MESSAGE_MIGRATION_COLUMNS = {
    "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "last_error": "TEXT",
    "last_attempt_at": "TEXT",
    "quarantined_at": "TEXT",
}
RUN_MIGRATION_COLUMNS = {
    "error_message": "TEXT",
}

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RunLock(AbstractContextManager["RunLock"]):
    lock_path: Path
    _handle: Any | None = None

    def __enter__(self) -> "RunLock":
        ensure_parent(self.lock_path)
        self._handle = self.lock_path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"0")
            self._handle.flush()
        self._handle.seek(0)
        try:
            _lock_file(self._handle)
        except (BlockingIOError, OSError) as exc:
            self._handle.close()
            self._handle = None
            raise RuntimeError("Another pipeline run is already in progress.") from exc
        return self

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        if self._handle is not None:
            _unlock_file(self._handle)
            self._handle.close()
            self._handle = None


def _lock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True, slots=True)
class SessionScope:
    character_slug: str
    relationship_slug: str


@dataclass(frozen=True, slots=True)
class SessionDeletionSummary:
    kv_entries_deleted: int
    inbound_messages_deleted: int
    outbound_messages_deleted: int
    runs_deleted: int
    assets_deleted: int


class StateStore:
    def __init__(self, settings: Settings, *, scope: SessionScope | None = None) -> None:
        self.settings = settings
        self.scope = scope or self._scope_from_settings()

    def initialize(self) -> None:
        self._require_scope()
        for path in (
            self.settings.documents.session_root,
            self.settings.documents.session_archive_dir,
            self.settings.documents.user_episode_dir,
            self.settings.documents.character_episode_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            self._ensure_schema(conn)

    def acquire_lock(self) -> RunLock:
        return RunLock(self.settings.repo_root / "pipeline.lock")

    def get_kv(self, key: str) -> str | None:
        with self._connect() as conn:
            if not self._table_exists(conn, "kv_state"):
                return None
            scope = self._require_scope()
            row = conn.execute(
                """
                SELECT value
                FROM kv_state
                WHERE character_slug = ? AND relationship_slug = ? AND key = ?
                """,
                (scope.character_slug, scope.relationship_slug, key),
            ).fetchone()
            return str(row["value"]) if row else None

    def set_kv(self, key: str, value: str) -> None:
        scope = self._require_scope()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO kv_state(character_slug, relationship_slug, key, value)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(character_slug, relationship_slug, key) DO UPDATE SET value = excluded.value
                """,
                (scope.character_slug, scope.relationship_slug, key, value),
            )

    def get_last_consumed_imap_uid(self) -> int:
        value = self.get_kv("last_consumed_imap_uid")
        return int(value) if value else 0

    def set_last_consumed_imap_uid(self, imap_uid: int) -> None:
        self.set_kv("last_consumed_imap_uid", str(imap_uid))

    def get_last_seen_imap_uid(self) -> int:
        value = self.get_kv("last_seen_imap_uid")
        return int(value) if value else 0

    def set_last_seen_imap_uid(self, imap_uid: int) -> None:
        self.set_kv("last_seen_imap_uid", str(imap_uid))

    def get_imap_uidvalidity(self) -> int:
        value = self.get_kv("imap_uidvalidity")
        return int(value) if value else 0

    def set_imap_uidvalidity(self, uidvalidity: int) -> None:
        self.set_kv("imap_uidvalidity", str(uidvalidity))

    def has_outbound_messages(self) -> bool:
        with self._connect() as conn:
            if not self._table_exists(conn, "outbound_messages"):
                return False
            scope = self._require_scope()
            row = conn.execute(
                """
                SELECT 1
                FROM outbound_messages
                WHERE character_slug = ? AND relationship_slug = ?
                LIMIT 1
                """,
                (scope.character_slug, scope.relationship_slug),
            ).fetchone()
            return row is not None

    def get_first_outbound_sent_at(self) -> datetime | None:
        with self._connect() as conn:
            if not self._table_exists(conn, "outbound_messages"):
                return None
            scope = self._require_scope()
            rows = conn.execute(
                """
                SELECT sent_at
                FROM outbound_messages
                WHERE character_slug = ? AND relationship_slug = ?
                """,
                (scope.character_slug, scope.relationship_slug),
            ).fetchall()
        if not rows:
            return None
        return min(datetime.fromisoformat(str(row["sent_at"])) for row in rows)

    def start_run(self, run_id: str, started_at: str) -> None:
        scope = self._require_scope()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs(
                    run_id, batch_id, started_at, finished_at, mode, status, error_stage, stage1_path, stage2_path,
                    character_slug, relationship_slug
                )
                VALUES(?, '', ?, NULL, 'pending', 'running', NULL, NULL, NULL, ?, ?)
                """,
                (run_id, started_at, scope.character_slug, scope.relationship_slug),
            )

    def update_run_context(self, run_id: str, mode: str, batch_id: str) -> None:
        scope = self._require_scope()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET mode = ?, batch_id = ?
                WHERE run_id = ? AND character_slug = ? AND relationship_slug = ?
                """,
                (mode, batch_id, run_id, scope.character_slug, scope.relationship_slug),
            )

    def finish_run(
        self,
        run_id: str,
        status: str,
        finished_at: str,
        *,
        error_stage: str | None = None,
        error_message: str | None = None,
        stage1_path: str | None = None,
        stage2_path: str | None = None,
    ) -> None:
        scope = self._require_scope()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET finished_at = ?, status = ?, error_stage = ?, error_message = ?,
                    stage1_path = COALESCE(?, stage1_path), stage2_path = COALESCE(?, stage2_path)
                WHERE run_id = ? AND character_slug = ? AND relationship_slug = ?
                """,
                (
                    finished_at,
                    status,
                    error_stage,
                    error_message,
                    stage1_path,
                    stage2_path,
                    run_id,
                    scope.character_slug,
                    scope.relationship_slug,
                ),
            )

    def list_stale_running_runs(self, *, cutoff: datetime) -> list[StoredRun]:
        with self._connect() as conn:
            if not self._table_exists(conn, "runs"):
                return []
            scope = self._require_scope()
            rows = conn.execute(
                """
                SELECT run_id, batch_id, started_at, finished_at, mode, status, error_stage, error_message,
                       stage1_path, stage2_path, character_slug, relationship_slug
                FROM runs
                WHERE character_slug = ? AND relationship_slug = ? AND status = 'running'
                ORDER BY started_at ASC
                """,
                (scope.character_slug, scope.relationship_slug),
            ).fetchall()
        return [
            self._build_stored_run(row)
            for row in rows
            if _parse_run_started_at(str(row["started_at"]), cutoff) < cutoff
        ]

    def mark_stale_running_runs_failed(self, *, cutoff: datetime, finished_at: datetime) -> list[StoredRun]:
        stale_runs = self.list_stale_running_runs(cutoff=cutoff)
        if not stale_runs:
            return []
        scope = self._require_scope()
        run_ids = [run.run_id for run in stale_runs]
        placeholders = ",".join("?" for _ in run_ids)
        error_message = f"Run was still 'running' after the stale cutoff {cutoff.isoformat()}; marked failed."
        with self._connect() as conn:
            conn.execute(
                (
                    "UPDATE runs SET status = 'failed', finished_at = ?, error_stage = 'stale_running', "
                    "error_message = ? "
                    f"WHERE character_slug = ? AND relationship_slug = ? AND status = 'running' "
                    f"AND run_id IN ({placeholders})"
                ),
                (finished_at.isoformat(), error_message, scope.character_slug, scope.relationship_slug, *run_ids),
            )
        return stale_runs

    def get_consecutive_failures(self) -> int:
        value = self.get_kv(CONSECUTIVE_FAILURES_KEY)
        return int(value) if value else 0

    def get_last_failed_at(self) -> str | None:
        return self.get_kv(LAST_FAILED_AT_KEY) or None

    def record_run_failure(self, *, failed_at: str) -> int:
        consecutive_failures = self.get_consecutive_failures() + 1
        self.set_kv(CONSECUTIVE_FAILURES_KEY, str(consecutive_failures))
        self.set_kv(LAST_FAILED_AT_KEY, failed_at)
        return consecutive_failures

    def reset_run_failures(self) -> None:
        self.set_kv(CONSECUTIVE_FAILURES_KEY, "0")
        self.set_kv(LAST_FAILED_AT_KEY, "")

    def get_runs_today(self, today: dt_date) -> int:
        day, _, count = (self.get_kv(RUNS_TODAY_KEY) or "").partition(":")
        if day != today.isoformat() or not count.isdigit():
            return 0
        return int(count)

    def increment_runs_today(self, today: dt_date) -> int:
        runs_today = self.get_runs_today(today) + 1
        self.set_kv(RUNS_TODAY_KEY, f"{today.isoformat()}:{runs_today}")
        return runs_today

    def upsert_inbound_message(
        self,
        message: NormalizedInboundMessage,
        *,
        raw_path: Path,
        normalized_text_path: Path | None = None,
    ) -> None:
        scope = self._require_scope()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inbound_messages(
                    imap_uid, uidvalidity, message_id, received_at, from_email, subject, raw_path,
                    normalized_text_path, body_text, body_html_present, attachments_json,
                    thread_message_id, consumed_batch_id, character_slug, relationship_slug
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(character_slug, relationship_slug, uidvalidity, imap_uid) DO UPDATE SET
                    message_id = excluded.message_id,
                    received_at = excluded.received_at,
                    from_email = excluded.from_email,
                    subject = excluded.subject,
                    raw_path = excluded.raw_path,
                    normalized_text_path = excluded.normalized_text_path,
                    body_text = excluded.body_text,
                    body_html_present = excluded.body_html_present,
                    attachments_json = excluded.attachments_json,
                    thread_message_id = excluded.thread_message_id
                """,
                (
                    message.imap_uid,
                    message.uidvalidity,
                    message.message_id,
                    message.received_at,
                    message.from_email,
                    message.subject,
                    str(raw_path),
                    str(normalized_text_path) if normalized_text_path else "",
                    message.body_text,
                    1 if message.body_html_present else 0,
                    json.dumps([item.to_dict() for item in message.attachments], ensure_ascii=False),
                    message.thread_message_id,
                    scope.character_slug,
                    scope.relationship_slug,
                ),
            )

    def has_inbound_message_id(self, message_id: str) -> bool:
        with self._connect() as conn:
            if not self._table_exists(conn, "inbound_messages"):
                return False
            scope = self._require_scope()
            row = conn.execute(
                """
                SELECT 1
                FROM inbound_messages
                WHERE character_slug = ? AND relationship_slug = ? AND message_id = ?
                LIMIT 1
                """,
                (scope.character_slug, scope.relationship_slug, message_id),
            ).fetchone()
            return row is not None

    def list_unconsumed_inbound(
        self,
        *,
        limit: int | None = None,
        include_quarantined: bool = False,
    ) -> list[StoredInboundMessage]:
        """List inbound mail that still needs a reply.

        Quarantined messages (repeated pipeline failures) are excluded unless
        ``include_quarantined`` is set, so the next run can fall back to initiative mode.
        """
        condition = "consumed_batch_id IS NULL"
        if not include_quarantined:
            condition += " AND quarantined_at IS NULL"
        return self._list_inbound_where(condition, limit=limit)

    def list_quarantined_inbound(self, *, limit: int | None = None) -> list[StoredInboundMessage]:
        return self._list_inbound_where("quarantined_at IS NOT NULL", limit=limit)

    def _list_inbound_where(self, condition: str, *, limit: int | None = None) -> list[StoredInboundMessage]:
        with self._connect() as conn:
            if not self._table_exists(conn, "inbound_messages"):
                return []
            scope = self._require_scope()
            query = (
                "SELECT imap_uid, uidvalidity, message_id, received_at, from_email, subject, raw_path, "
                "normalized_text_path, body_text, body_html_present, attachments_json, "
                "character_slug, relationship_slug, consumed_batch_id, thread_message_id, "
                "attempt_count, last_error, last_attempt_at, quarantined_at "
                "FROM inbound_messages "
                f"WHERE character_slug = ? AND relationship_slug = ? AND {condition} "
                "ORDER BY uidvalidity ASC, imap_uid ASC"
            )
            params: list[object] = [scope.character_slug, scope.relationship_slug]
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)
            rows = conn.execute(query, params).fetchall()
        return [self._build_stored_inbound_message(row) for row in rows]

    def count_unconsumed_inbound(self) -> int:
        return self._count_inbound_where("consumed_batch_id IS NULL AND quarantined_at IS NULL")

    def count_quarantined_inbound(self) -> int:
        return self._count_inbound_where("quarantined_at IS NOT NULL")

    def _count_inbound_where(self, condition: str) -> int:
        with self._connect() as conn:
            if not self._table_exists(conn, "inbound_messages"):
                return 0
            scope = self._require_scope()
            row = conn.execute(
                (
                    "SELECT COUNT(*) AS count FROM inbound_messages "
                    f"WHERE character_slug = ? AND relationship_slug = ? AND {condition}"
                ),
                (scope.character_slug, scope.relationship_slug),
            ).fetchone()
            return int(row["count"]) if row else 0

    def record_inbound_attempt_failure(
        self,
        imap_uids: list[int],
        *,
        error_stage: str,
        message: str,
        attempted_at: str,
        max_attempts: int,
    ) -> list[int]:
        """Count one failed processing attempt for a batch; quarantine messages that reached ``max_attempts``.

        Returns the UIDs that were newly quarantined by this call.
        """
        if not imap_uids:
            return []
        scope = self._require_scope()
        placeholders = ",".join("?" for _ in imap_uids)
        last_error = f"{error_stage}: {message}"[:INBOUND_LAST_ERROR_MAX_CHARS]
        with self._connect() as conn:
            conn.execute(
                (
                    "UPDATE inbound_messages "
                    "SET attempt_count = attempt_count + 1, last_error = ?, last_attempt_at = ? "
                    f"WHERE character_slug = ? AND relationship_slug = ? AND imap_uid IN ({placeholders})"
                ),
                (last_error, attempted_at, scope.character_slug, scope.relationship_slug, *imap_uids),
            )
            rows = conn.execute(
                (
                    "SELECT imap_uid FROM inbound_messages "
                    f"WHERE character_slug = ? AND relationship_slug = ? AND imap_uid IN ({placeholders}) "
                    "AND quarantined_at IS NULL AND attempt_count >= ? ORDER BY imap_uid ASC"
                ),
                (scope.character_slug, scope.relationship_slug, *imap_uids, max_attempts),
            ).fetchall()
            quarantined_uids = [int(row["imap_uid"]) for row in rows]
            if quarantined_uids:
                quarantine_placeholders = ",".join("?" for _ in quarantined_uids)
                conn.execute(
                    (
                        "UPDATE inbound_messages SET quarantined_at = ? "
                        f"WHERE character_slug = ? AND relationship_slug = ? AND imap_uid IN ({quarantine_placeholders})"
                    ),
                    (attempted_at, scope.character_slug, scope.relationship_slug, *quarantined_uids),
                )
        return quarantined_uids

    def requeue_inbound_message(self, imap_uid: int) -> bool:
        """Clear quarantine and attempt bookkeeping so the message is processed again."""
        scope = self._require_scope()
        with self._connect() as conn:
            if not self._table_exists(conn, "inbound_messages"):
                return False
            updated = conn.execute(
                """
                UPDATE inbound_messages
                SET attempt_count = 0, last_error = NULL, last_attempt_at = NULL, quarantined_at = NULL
                WHERE character_slug = ? AND relationship_slug = ? AND imap_uid = ?
                """,
                (scope.character_slug, scope.relationship_slug, imap_uid),
            ).rowcount
        return updated > 0

    def mark_inbound_consumed(self, message_keys: Sequence[tuple[int, int]], batch_id: str) -> None:
        """Mark inbound rows consumed; each key is ``(uidvalidity, imap_uid)`` as stored on the message."""
        if not message_keys:
            return
        scope = self._require_scope()
        with self._connect() as conn:
            conn.executemany(
                """
                UPDATE inbound_messages SET consumed_batch_id = ?
                WHERE character_slug = ? AND relationship_slug = ? AND uidvalidity = ? AND imap_uid = ?
                """,
                [
                    (batch_id, scope.character_slug, scope.relationship_slug, uidvalidity, imap_uid)
                    for uidvalidity, imap_uid in message_keys
                ],
            )

    def delete_inbound_message(self, imap_uid: int, *, uidvalidity: int | None = None) -> bool:
        """Delete one inbound row by UID within a UIDVALIDITY epoch (the current mailbox epoch by default)."""
        scope = self._require_scope()
        if uidvalidity is None:
            uidvalidity = self.get_imap_uidvalidity()
        with self._connect() as conn:
            if not self._table_exists(conn, "inbound_messages"):
                return False
            row = conn.execute(
                """
                SELECT raw_path, normalized_text_path
                FROM inbound_messages
                WHERE character_slug = ? AND relationship_slug = ? AND uidvalidity = ? AND imap_uid = ?
                """,
                (scope.character_slug, scope.relationship_slug, uidvalidity, imap_uid),
            ).fetchone()
            if row is None:
                return False
            paths = self._resolve_deletable_paths(
                [
                    str(row["raw_path"]),
                    str(row["normalized_text_path"]) if row["normalized_text_path"] else None,
                ]
            )
            conn.execute(
                """
                DELETE FROM inbound_messages
                WHERE character_slug = ? AND relationship_slug = ? AND uidvalidity = ? AND imap_uid = ?
                """,
                (scope.character_slug, scope.relationship_slug, uidvalidity, imap_uid),
            )
        self._delete_paths(paths)
        return True

    def delete_all_inbound_messages(self) -> int:
        scope = self._require_scope()
        with self._connect() as conn:
            if not self._table_exists(conn, "inbound_messages"):
                return 0
            rows = conn.execute(
                """
                SELECT raw_path, normalized_text_path
                FROM inbound_messages
                WHERE character_slug = ? AND relationship_slug = ?
                """,
                (scope.character_slug, scope.relationship_slug),
            ).fetchall()
            if not rows:
                return 0
            paths = self._resolve_deletable_paths(
                [
                    str(value)
                    for row in rows
                    for value in (row["raw_path"], row["normalized_text_path"])
                    if value
                ]
            )
            deleted_count = conn.execute(
                """
                DELETE FROM inbound_messages
                WHERE character_slug = ? AND relationship_slug = ?
                """,
                (scope.character_slug, scope.relationship_slug),
            ).rowcount
        self._delete_paths(paths)
        return deleted_count

    def record_outbound_message(
        self,
        *,
        message_id: str,
        sent_at: str,
        to_email: str,
        subject: str,
        raw_path: Path,
        image_path: Path | None,
        run_id: str,
        batch_id: str,
    ) -> None:
        self._record_outbound_message(
            message_id=message_id,
            sent_at=sent_at,
            to_email=to_email,
            subject=subject,
            raw_path=raw_path,
            image_path=image_path,
            run_id=run_id,
            batch_id=batch_id,
            delivery_status=OUTBOUND_SENT,
        )

    def record_pending_outbound_message(
        self,
        *,
        message_id: str,
        sent_at: str,
        to_email: str,
        subject: str,
        raw_path: Path,
        image_path: Path | None,
        run_id: str,
        batch_id: str,
    ) -> None:
        self._record_outbound_message(
            message_id=message_id,
            sent_at=sent_at,
            to_email=to_email,
            subject=subject,
            raw_path=raw_path,
            image_path=image_path,
            run_id=run_id,
            batch_id=batch_id,
            delivery_status=OUTBOUND_PENDING,
        )

    def _record_outbound_message(
        self,
        *,
        message_id: str,
        sent_at: str,
        to_email: str,
        subject: str,
        raw_path: Path,
        image_path: Path | None,
        run_id: str,
        batch_id: str,
        delivery_status: str,
    ) -> None:
        if delivery_status not in {OUTBOUND_PENDING, OUTBOUND_SENT}:
            raise ValueError(f"Unsupported outbound delivery status: {delivery_status}")
        scope = self._require_scope()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO outbound_messages(
                    message_id, sent_at, to_email, subject, raw_path, image_path, run_id, batch_id,
                    character_slug, relationship_slug, delivery_status
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    sent_at,
                    to_email,
                    subject,
                    str(raw_path),
                    str(image_path) if image_path else None,
                    run_id,
                    batch_id,
                    scope.character_slug,
                    scope.relationship_slug,
                    delivery_status,
                ),
            )

    def mark_outbound_message_sent(self, *, message_id: str, sent_at: str) -> None:
        scope = self._require_scope()
        with self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE outbound_messages
                SET delivery_status = ?, sent_at = ?
                WHERE character_slug = ? AND relationship_slug = ? AND message_id = ?
                """,
                (OUTBOUND_SENT, sent_at, scope.character_slug, scope.relationship_slug, message_id),
            ).rowcount
        if updated == 0:
            raise RuntimeError(f"Outbound message not found: {message_id}")

    def get_outbound_message_for_batch(self, batch_id: str) -> StoredOutboundMessage | None:
        with self._connect() as conn:
            if not self._table_exists(conn, "outbound_messages"):
                return None
            scope = self._require_scope()
            rows = conn.execute(
                """
                SELECT message_id, sent_at, to_email, subject, raw_path, image_path, run_id, batch_id,
                       character_slug, relationship_slug, delivery_status
                FROM outbound_messages
                WHERE character_slug = ? AND relationship_slug = ? AND batch_id = ?
                """,
                (scope.character_slug, scope.relationship_slug, batch_id),
            ).fetchall()
        if not rows:
            return None
        latest_row = max(rows, key=lambda row: datetime.fromisoformat(str(row["sent_at"])))
        return self._build_stored_outbound_message(latest_row)

    def count_pending_outbound(self) -> int:
        with self._connect() as conn:
            if not self._table_exists(conn, "outbound_messages"):
                return 0
            scope = self._require_scope()
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM outbound_messages
                WHERE character_slug = ? AND relationship_slug = ? AND delivery_status = ?
                """,
                (scope.character_slug, scope.relationship_slug, OUTBOUND_PENDING),
            ).fetchone()
            return int(row["count"]) if row else 0

    def delete_outbound_message(self, message_id: str) -> bool:
        scope = self._require_scope()
        with self._connect() as conn:
            if not self._table_exists(conn, "outbound_messages"):
                return False
            row = conn.execute(
                """
                SELECT raw_path
                FROM outbound_messages
                WHERE character_slug = ? AND relationship_slug = ? AND message_id = ?
                """,
                (scope.character_slug, scope.relationship_slug, message_id),
            ).fetchone()
            if row is None:
                return False
            paths = self._resolve_deletable_paths([str(row["raw_path"])])
            conn.execute(
                """
                DELETE FROM outbound_messages
                WHERE character_slug = ? AND relationship_slug = ? AND message_id = ?
                """,
                (scope.character_slug, scope.relationship_slug, message_id),
            )
        self._delete_paths(paths)
        return True

    def delete_all_outbound_messages(self) -> int:
        scope = self._require_scope()
        with self._connect() as conn:
            if not self._table_exists(conn, "outbound_messages"):
                return 0
            rows = conn.execute(
                """
                SELECT raw_path
                FROM outbound_messages
                WHERE character_slug = ? AND relationship_slug = ?
                """,
                (scope.character_slug, scope.relationship_slug),
            ).fetchall()
            if not rows:
                return 0
            paths = self._resolve_deletable_paths([str(row["raw_path"]) for row in rows if row["raw_path"]])
            deleted_count = conn.execute(
                """
                DELETE FROM outbound_messages
                WHERE character_slug = ? AND relationship_slug = ?
                """,
                (scope.character_slug, scope.relationship_slug),
            ).rowcount
        self._delete_paths(paths)
        return deleted_count

    def record_asset(
        self,
        *,
        asset_id: str,
        run_id: str,
        image_path: Path,
        scene_card: dict[str, Any],
        prompt_text: str,
        created_at: str,
    ) -> None:
        scope = self._require_scope()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO assets(
                    asset_id, run_id, image_path, scene_card_json, prompt_text, created_at,
                    character_slug, relationship_slug
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    run_id,
                    str(image_path),
                    json.dumps(scene_card, ensure_ascii=False),
                    prompt_text,
                    created_at,
                    scope.character_slug,
                    scope.relationship_slug,
                ),
            )

    def list_recent_runs(self, *, limit: int = 5) -> list[StoredRun]:
        with self._connect() as conn:
            if not self._table_exists(conn, "runs"):
                return []
            scope = self._require_scope()
            rows = conn.execute(
                """
                SELECT run_id, batch_id, started_at, finished_at, mode, status, error_stage, error_message,
                       stage1_path, stage2_path, character_slug, relationship_slug
                FROM runs
                WHERE character_slug = ? AND relationship_slug = ?
                """,
                (scope.character_slug, scope.relationship_slug),
            ).fetchall()
        rows = sorted(rows, key=lambda row: datetime.fromisoformat(str(row["started_at"])), reverse=True)[:limit]
        return [self._build_stored_run(row) for row in rows]

    def get_run(self, run_id: str) -> StoredRun | None:
        with self._connect() as conn:
            if not self._table_exists(conn, "runs"):
                return None
            scope = self._require_scope()
            row = conn.execute(
                """
                SELECT run_id, batch_id, started_at, finished_at, mode, status, error_stage, error_message,
                       stage1_path, stage2_path, character_slug, relationship_slug
                FROM runs
                WHERE run_id = ? AND character_slug = ? AND relationship_slug = ?
                LIMIT 1
                """,
                (run_id, scope.character_slug, scope.relationship_slug),
            ).fetchone()
        return self._build_stored_run(row) if row else None

    def get_latest_run(self, *, status: str) -> StoredRun | None:
        runs = self._list_runs_where("status = ?", (status,))
        return runs[0] if runs else None

    def get_latest_failed_run_for_batch(self, batch_id: str) -> StoredRun | None:
        runs = self._list_runs_where("status = 'failed' AND batch_id = ?", (batch_id,))
        return runs[0] if runs else None

    def _list_runs_where(self, condition: str, params: tuple[object, ...]) -> list[StoredRun]:
        """Scoped runs matching ``condition``, newest ``started_at`` first (sorted as real instants, not strings)."""
        with self._connect() as conn:
            if not self._table_exists(conn, "runs"):
                return []
            scope = self._require_scope()
            rows = conn.execute(
                (
                    "SELECT run_id, batch_id, started_at, finished_at, mode, status, error_stage, error_message, "
                    "stage1_path, stage2_path, character_slug, relationship_slug "
                    f"FROM runs WHERE character_slug = ? AND relationship_slug = ? AND {condition}"
                ),
                (scope.character_slug, scope.relationship_slug, *params),
            ).fetchall()
        rows = sorted(rows, key=lambda row: datetime.fromisoformat(str(row["started_at"])), reverse=True)
        return [self._build_stored_run(row) for row in rows]

    def list_recent_outbound_messages(self, *, limit: int = 5) -> list[StoredOutboundMessage]:
        with self._connect() as conn:
            if not self._table_exists(conn, "outbound_messages"):
                return []
            scope = self._require_scope()
            rows = conn.execute(
                """
                SELECT message_id, sent_at, to_email, subject, raw_path, image_path, run_id, batch_id,
                       character_slug, relationship_slug, delivery_status
                FROM outbound_messages
                WHERE character_slug = ? AND relationship_slug = ?
                """,
                (scope.character_slug, scope.relationship_slug),
            ).fetchall()
        rows = sorted(rows, key=lambda row: datetime.fromisoformat(str(row["sent_at"])), reverse=True)[:limit]
        return [self._build_stored_outbound_message(row) for row in rows]

    def list_recent_assets(self, *, limit: int = 5) -> list[StoredAsset]:
        with self._connect() as conn:
            if not self._table_exists(conn, "assets"):
                return []
            scope = self._require_scope()
            rows = conn.execute(
                """
                SELECT asset_id, run_id, image_path, scene_card_json, prompt_text, created_at,
                       character_slug, relationship_slug
                FROM assets
                WHERE character_slug = ? AND relationship_slug = ?
                """,
                (scope.character_slug, scope.relationship_slug),
            ).fetchall()
        rows = sorted(rows, key=lambda row: datetime.fromisoformat(str(row["created_at"])), reverse=True)[:limit]
        return [self._build_stored_asset(row) for row in rows]

    def list_recent_scene_cards(self, *, limit: int = 7) -> list[dict[str, Any]]:
        return [
            {
                "created_at": asset.created_at,
                "scene_card": asset.scene_card,
            }
            for asset in self.list_recent_assets(limit=limit)
        ]

    def delete_session_records(self) -> SessionDeletionSummary:
        scope = self._require_scope()
        file_paths: list[str | None] = []
        with self._connect() as conn:
            inbound_deleted = 0
            outbound_deleted = 0
            runs_deleted = 0
            assets_deleted = 0
            kv_deleted = 0
            has_inbound = self._table_exists(conn, "inbound_messages")
            has_outbound = self._table_exists(conn, "outbound_messages")
            has_runs = self._table_exists(conn, "runs")
            has_assets = self._table_exists(conn, "assets")
            has_kv = self._table_exists(conn, "kv_state")

            if has_inbound:
                inbound_rows = conn.execute(
                    """
                    SELECT raw_path, normalized_text_path
                    FROM inbound_messages
                    WHERE character_slug = ? AND relationship_slug = ?
                    """,
                    (scope.character_slug, scope.relationship_slug),
                ).fetchall()
                file_paths.extend(
                    value
                    for row in inbound_rows
                    for value in (row["raw_path"], row["normalized_text_path"])
                )

            if has_outbound:
                outbound_rows = conn.execute(
                    """
                    SELECT raw_path, image_path
                    FROM outbound_messages
                    WHERE character_slug = ? AND relationship_slug = ?
                    """,
                    (scope.character_slug, scope.relationship_slug),
                ).fetchall()
                file_paths.extend(value for row in outbound_rows for value in (row["raw_path"], row["image_path"]))

            if has_runs:
                run_rows = conn.execute(
                    """
                    SELECT stage1_path, stage2_path
                    FROM runs
                    WHERE character_slug = ? AND relationship_slug = ?
                    """,
                    (scope.character_slug, scope.relationship_slug),
                ).fetchall()
                file_paths.extend(value for row in run_rows for value in (row["stage1_path"], row["stage2_path"]))

            if has_assets:
                asset_rows = conn.execute(
                    """
                    SELECT image_path
                    FROM assets
                    WHERE character_slug = ? AND relationship_slug = ?
                    """,
                    (scope.character_slug, scope.relationship_slug),
                ).fetchall()
                file_paths.extend(row["image_path"] for row in asset_rows)
            paths = self._resolve_deletable_paths(file_paths)

            if has_inbound:
                inbound_deleted = conn.execute(
                    """
                    DELETE FROM inbound_messages
                    WHERE character_slug = ? AND relationship_slug = ?
                    """,
                    (scope.character_slug, scope.relationship_slug),
                ).rowcount
            if has_outbound:
                outbound_deleted = conn.execute(
                    """
                    DELETE FROM outbound_messages
                    WHERE character_slug = ? AND relationship_slug = ?
                    """,
                    (scope.character_slug, scope.relationship_slug),
                ).rowcount
            if has_runs:
                runs_deleted = conn.execute(
                    """
                    DELETE FROM runs
                    WHERE character_slug = ? AND relationship_slug = ?
                    """,
                    (scope.character_slug, scope.relationship_slug),
                ).rowcount
            if has_assets:
                assets_deleted = conn.execute(
                    """
                    DELETE FROM assets
                    WHERE character_slug = ? AND relationship_slug = ?
                    """,
                    (scope.character_slug, scope.relationship_slug),
                ).rowcount

            if has_kv:
                kv_deleted = conn.execute(
                    """
                    DELETE FROM kv_state
                    WHERE character_slug = ? AND relationship_slug = ?
                    """,
                    (scope.character_slug, scope.relationship_slug),
                ).rowcount

        self._delete_paths(paths)
        return SessionDeletionSummary(
            kv_entries_deleted=kv_deleted,
            inbound_messages_deleted=inbound_deleted,
            outbound_messages_deleted=outbound_deleted,
            runs_deleted=runs_deleted,
            assets_deleted=assets_deleted,
        )

    def run_archive_dir(self, run_id: str, run_at: datetime) -> Path:
        path = self.settings.documents.session_archive_dir / run_at.date().isoformat() / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_inbound_artifacts(
        self,
        run_id: str,
        run_at: datetime,
        message: NormalizedInboundMessage,
        raw_bytes: bytes,
    ) -> Path:
        raw_path = self.run_archive_dir(run_id, run_at) / "inbox_raw" / f"uid-{message.imap_uid}.eml"
        ensure_parent(raw_path)
        raw_path.write_bytes(raw_bytes)
        return raw_path

    def save_normalized_inbound_messages(
        self,
        run_id: str,
        run_at: datetime,
        messages: list[NormalizedInboundMessage],
    ) -> Path:
        path = self.run_archive_dir(run_id, run_at) / "normalized_inbound_messages.json"
        ensure_parent(path)
        path.write_text(
            json.dumps([message.to_dict() for message in messages], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def save_outbound_raw(self, run_id: str, raw_bytes: bytes, sent_at: datetime) -> Path:
        raw_path = self.run_archive_dir(run_id, sent_at) / "outbox_raw.eml"
        ensure_parent(raw_path)
        raw_path.write_bytes(raw_bytes)
        return raw_path

    def save_stage_json(self, run_id: str, run_at: datetime, filename: str, payload: dict[str, Any]) -> Path:
        path = self.run_archive_dir(run_id, run_at) / filename
        ensure_parent(path)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def save_generated_image(self, run_id: str, run_at: datetime, image: GeneratedImage) -> Path:
        path = self.run_archive_dir(run_id, run_at) / f"scene.{image.file_extension}"
        ensure_parent(path)
        path.write_bytes(image.image_bytes)
        return path

    def _scope_from_settings(self) -> SessionScope | None:
        active = self.settings.active_profile
        if active is None:
            return None
        return SessionScope(
            character_slug=active.character.slug,
            relationship_slug=active.relationship.slug,
        )

    def _require_scope(self) -> SessionScope:
        if self.scope is not None:
            return self.scope
        active = ensure_active_profile(self.settings)
        self.scope = SessionScope(
            character_slug=active.character.slug,
            relationship_slug=active.relationship.slug,
        )
        return self.scope

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        self._migrate_legacy_inbound_messages(conn)
        conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS kv_state (
                character_slug TEXT NOT NULL,
                relationship_slug TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY(character_slug, relationship_slug, key)
            );

            CREATE TABLE IF NOT EXISTS inbound_messages ({INBOUND_MESSAGES_COLUMNS_SQL});

            CREATE TABLE IF NOT EXISTS outbound_messages (
                message_id TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL,
                to_email TEXT NOT NULL,
                subject TEXT NOT NULL,
                raw_path TEXT NOT NULL,
                image_path TEXT,
                run_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                character_slug TEXT NOT NULL,
                relationship_slug TEXT NOT NULL,
                delivery_status TEXT NOT NULL DEFAULT 'sent'
            );

            CREATE TABLE IF NOT EXISTS runs (
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

            CREATE TABLE IF NOT EXISTS assets (
                asset_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                image_path TEXT NOT NULL,
                scene_card_json TEXT NOT NULL,
                prompt_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                character_slug TEXT NOT NULL,
                relationship_slug TEXT NOT NULL
            );
            """
        )
        self._ensure_columns(conn, "inbound_messages", INBOUND_MESSAGE_MIGRATION_COLUMNS)
        self._ensure_columns(conn, "runs", RUN_MIGRATION_COLUMNS)
        self._ensure_indexes(conn)

    def _ensure_columns(self, conn: sqlite3.Connection, table_name: str, columns: dict[str, str]) -> None:
        """Add missing columns in place (ALTER TABLE ADD COLUMN) without rebuilding the table."""
        existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table_name})")}
        for column_name, definition in columns.items():
            if column_name in existing:
                continue
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
            LOGGER.info("Migrated table %s: added column %s", table_name, column_name)

    def _migrate_legacy_inbound_messages(self, conn: sqlite3.Connection) -> None:
        """Rebuild a pre-UIDVALIDITY inbound_messages table in place.

        SQLite cannot change a primary key with ALTER TABLE, so the legacy table (keyed by
        session + imap_uid) is copied into a fresh table keyed by session + uidvalidity + imap_uid.
        Legacy rows get uidvalidity 0. Every legacy column is carried over, including columns added
        by other migrations (attempt_count & co. are part of the new definition; anything else is
        re-added from PRAGMA table_info), so no concurrent ALTER TABLE migration loses data.
        """
        if not self._table_exists(conn, "inbound_messages"):
            return
        legacy_columns = conn.execute("PRAGMA table_info(inbound_messages)").fetchall()
        legacy_names = [str(column["name"]) for column in legacy_columns]
        if "uidvalidity" in legacy_names:
            return
        row_count = int(conn.execute("SELECT COUNT(*) AS count FROM inbound_messages").fetchone()["count"])
        LOGGER.warning(
            "Migrating inbound_messages to the UIDVALIDITY-scoped schema; %s existing row(s) keep uidvalidity=0.",
            row_count,
        )
        if not conn.in_transaction:
            conn.execute("BEGIN")
        conn.execute("DROP TABLE IF EXISTS inbound_messages__new")
        conn.execute(f"CREATE TABLE inbound_messages__new ({INBOUND_MESSAGES_COLUMNS_SQL})")
        new_names = {str(column["name"]) for column in conn.execute("PRAGMA table_info(inbound_messages__new)")}
        for column in legacy_columns:
            if str(column["name"]) in new_names:
                continue
            conn.execute(f"ALTER TABLE inbound_messages__new ADD COLUMN {_legacy_column_definition(column)}")
        copied = ", ".join(_quote_identifier(name) for name in legacy_names)
        conn.execute(f"INSERT INTO inbound_messages__new ({copied}) SELECT {copied} FROM inbound_messages")
        conn.execute("DROP TABLE inbound_messages")
        conn.execute("ALTER TABLE inbound_messages__new RENAME TO inbound_messages")
        conn.commit()

    def _dedupe_inbound_message_ids(self, conn: sqlite3.Connection) -> None:
        """Keep one row per (session, Message-ID) before the unique index is created.

        Legacy databases could store the same letter twice when it was moved out of and back into
        INBOX (new UID). The consumed copy wins so the letter is not answered again; otherwise the
        lowest UID wins.
        """
        rows = conn.execute(
            """
            SELECT rowid, character_slug, relationship_slug, message_id
            FROM inbound_messages
            ORDER BY character_slug, relationship_slug, message_id,
                     (consumed_batch_id IS NULL) ASC, uidvalidity ASC, imap_uid ASC
            """
        ).fetchall()
        seen: set[tuple[str, str, str]] = set()
        duplicate_rowids: list[tuple[int]] = []
        for row in rows:
            key = (str(row["character_slug"]), str(row["relationship_slug"]), str(row["message_id"]))
            if key in seen:
                duplicate_rowids.append((int(row["rowid"]),))
                continue
            seen.add(key)
        if not duplicate_rowids:
            return
        LOGGER.warning(
            "Removing %s duplicate inbound row(s) sharing a Message-ID before enforcing uniqueness.",
            len(duplicate_rowids),
        )
        conn.executemany("DELETE FROM inbound_messages WHERE rowid = ?", duplicate_rowids)

    def _ensure_indexes(self, conn: sqlite3.Connection) -> None:
        if not self._index_exists(conn, INBOUND_MESSAGE_ID_INDEX):
            self._dedupe_inbound_message_ids(conn)
        conn.executescript(
            f"""
            CREATE INDEX IF NOT EXISTS idx_inbound_messages_scope_consumed
                ON inbound_messages(character_slug, relationship_slug, consumed_batch_id, imap_uid);
            CREATE UNIQUE INDEX IF NOT EXISTS {INBOUND_MESSAGE_ID_INDEX}
                ON inbound_messages(character_slug, relationship_slug, message_id);
            CREATE INDEX IF NOT EXISTS idx_outbound_messages_scope_sent_at
                ON outbound_messages(character_slug, relationship_slug, sent_at);
            CREATE INDEX IF NOT EXISTS idx_runs_scope_started_at
                ON runs(character_slug, relationship_slug, started_at);
            CREATE INDEX IF NOT EXISTS idx_assets_scope_created_at
                ON assets(character_slug, relationship_slug, created_at);
            """
        )

    def _table_exists(self, conn: sqlite3.Connection, table_name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    def _index_exists(self, conn: sqlite3.Connection, index_name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchone()
        return row is not None

    def _managed_delete_roots(self) -> tuple[Path, Path]:
        scope = self._require_scope()
        return (
            session_profile_dir(self.settings.repo_root, scope.character_slug, scope.relationship_slug),
            session_archive_dir(self.settings.repo_root, scope.character_slug, scope.relationship_slug),
        )

    def _resolve_deletable_paths(self, path_values: list[str | None]) -> list[Path]:
        roots = self._managed_delete_roots()
        resolved_paths: list[Path] = []
        seen: set[Path] = set()
        for path_value in path_values:
            if not path_value:
                continue
            path = resolve_path_within_roots(path_value, roots)
            if path in seen:
                continue
            seen.add(path)
            resolved_paths.append(path)
        return resolved_paths

    def _delete_paths(self, paths: list[Path]) -> None:
        for path in paths:
            try:
                if not path.exists():
                    continue
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as exc:
                LOGGER.warning("Failed to delete path %s: %s", path, exc)

    def _build_stored_inbound_message(self, row: sqlite3.Row) -> StoredInboundMessage:
        return StoredInboundMessage(
            imap_uid=int(row["imap_uid"]),
            message_id=str(row["message_id"]),
            received_at=str(row["received_at"]),
            from_email=str(row["from_email"]),
            subject=str(row["subject"]),
            raw_path=str(row["raw_path"]),
            normalized_text_path=str(row["normalized_text_path"]),
            body_text=str(row["body_text"]),
            character_slug=str(row["character_slug"]),
            relationship_slug=str(row["relationship_slug"]),
            consumed_batch_id=str(row["consumed_batch_id"]) if row["consumed_batch_id"] else None,
            thread_message_id=str(row["thread_message_id"]) if row["thread_message_id"] else None,
            uidvalidity=int(row["uidvalidity"] or 0),
            body_html_present=bool(row["body_html_present"]),
            attachments=_parse_attachments_json(row["attachments_json"]),
            attempt_count=int(_optional_row_text(row, "attempt_count") or 0),
            last_error=_optional_row_text(row, "last_error"),
            last_attempt_at=_optional_row_text(row, "last_attempt_at"),
            quarantined_at=_optional_row_text(row, "quarantined_at"),
        )

    def _build_stored_outbound_message(self, row: sqlite3.Row) -> StoredOutboundMessage:
        return StoredOutboundMessage(
            message_id=str(row["message_id"]),
            sent_at=str(row["sent_at"]),
            to_email=str(row["to_email"]),
            subject=str(row["subject"]),
            raw_path=str(row["raw_path"]),
            image_path=str(row["image_path"]) if row["image_path"] else None,
            run_id=str(row["run_id"]),
            batch_id=str(row["batch_id"]),
            character_slug=str(row["character_slug"]),
            relationship_slug=str(row["relationship_slug"]),
            delivery_status=str(row["delivery_status"]),
        )

    def _build_stored_run(self, row: sqlite3.Row) -> StoredRun:
        return StoredRun(
            run_id=str(row["run_id"]),
            batch_id=str(row["batch_id"]),
            started_at=str(row["started_at"]),
            finished_at=str(row["finished_at"]) if row["finished_at"] else None,
            mode=str(row["mode"]),
            status=str(row["status"]),
            error_stage=str(row["error_stage"]) if row["error_stage"] else None,
            stage1_path=str(row["stage1_path"]) if row["stage1_path"] else None,
            stage2_path=str(row["stage2_path"]) if row["stage2_path"] else None,
            character_slug=str(row["character_slug"]),
            relationship_slug=str(row["relationship_slug"]),
            error_message=_optional_row_text(row, "error_message"),
        )

    def _build_stored_asset(self, row: sqlite3.Row) -> StoredAsset:
        return StoredAsset(
            asset_id=str(row["asset_id"]),
            run_id=str(row["run_id"]),
            image_path=str(row["image_path"]),
            scene_card=json.loads(str(row["scene_card_json"])),
            prompt_text=str(row["prompt_text"]),
            created_at=str(row["created_at"]),
            character_slug=str(row["character_slug"]),
            relationship_slug=str(row["relationship_slug"]),
        )

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        ensure_parent(self.settings.storage.sqlite_path)
        conn = sqlite3.connect(self.settings.storage.sqlite_path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()


def _optional_row_text(row: sqlite3.Row, column: str) -> str | None:
    """Read a nullable column that older databases or narrower SELECTs may not carry."""
    columns = row.keys()
    if column not in columns:
        return None
    value = row[column]
    return str(value) if value is not None and value != "" else None


def _parse_attachments_json(value: object) -> list[AttachmentMeta]:
    if not value:
        return []
    try:
        payload = json.loads(str(value))
    except ValueError:
        LOGGER.warning("Ignoring malformed attachments_json on inbound row: %r", value)
        return []
    if not isinstance(payload, list):
        return []
    return [AttachmentMeta.from_dict(item) for item in payload if isinstance(item, dict)]


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _legacy_column_definition(column: sqlite3.Row) -> str:
    """Rebuild an ``ADD COLUMN`` clause from a ``PRAGMA table_info`` row of the legacy table."""
    definition = f"{_quote_identifier(str(column['name']))} {column['type'] or ''}".rstrip()
    default_value = column["dflt_value"]
    if default_value is not None:
        definition += f" DEFAULT {default_value}"
        if column["notnull"]:
            definition += " NOT NULL"
    return definition


def _parse_run_started_at(value: str, cutoff: datetime) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None and cutoff.tzinfo is not None:
        return parsed.replace(tzinfo=cutoff.tzinfo)
    if parsed.tzinfo is not None and cutoff.tzinfo is None:
        return parsed.replace(tzinfo=None)
    return parsed
