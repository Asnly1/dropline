from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings, ensure_active_profile
from .models import (
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
        stage1_path: str | None = None,
        stage2_path: str | None = None,
    ) -> None:
        scope = self._require_scope()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET finished_at = ?, status = ?, error_stage = ?, stage1_path = COALESCE(?, stage1_path),
                    stage2_path = COALESCE(?, stage2_path)
                WHERE run_id = ? AND character_slug = ? AND relationship_slug = ?
                """,
                (
                    finished_at,
                    status,
                    error_stage,
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
                SELECT run_id, batch_id, started_at, finished_at, mode, status, error_stage, stage1_path, stage2_path,
                       character_slug, relationship_slug
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
        with self._connect() as conn:
            conn.execute(
                (
                    "UPDATE runs SET status = 'failed', finished_at = ?, error_stage = 'stale_running' "
                    f"WHERE character_slug = ? AND relationship_slug = ? AND status = 'running' "
                    f"AND run_id IN ({placeholders})"
                ),
                (finished_at.isoformat(), scope.character_slug, scope.relationship_slug, *run_ids),
            )
        return stale_runs

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
                    imap_uid, message_id, received_at, from_email, subject, raw_path,
                    normalized_text_path, body_text, thread_message_id, consumed_batch_id,
                    character_slug, relationship_slug
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(character_slug, relationship_slug, imap_uid) DO UPDATE SET
                    message_id = excluded.message_id,
                    received_at = excluded.received_at,
                    from_email = excluded.from_email,
                    subject = excluded.subject,
                    raw_path = excluded.raw_path,
                    normalized_text_path = excluded.normalized_text_path,
                    body_text = excluded.body_text,
                    thread_message_id = excluded.thread_message_id
                """,
                (
                    message.imap_uid,
                    message.message_id,
                    message.received_at,
                    message.from_email,
                    message.subject,
                    str(raw_path),
                    str(normalized_text_path) if normalized_text_path else "",
                    message.body_text,
                    message.thread_message_id,
                    scope.character_slug,
                    scope.relationship_slug,
                ),
            )

    def list_unconsumed_inbound(self, *, limit: int | None = None) -> list[StoredInboundMessage]:
        with self._connect() as conn:
            if not self._table_exists(conn, "inbound_messages"):
                return []
            scope = self._require_scope()
            query = """
                SELECT imap_uid, message_id, received_at, from_email, subject, raw_path,
                       normalized_text_path, body_text, character_slug, relationship_slug,
                       consumed_batch_id, thread_message_id
                FROM inbound_messages
                WHERE character_slug = ? AND relationship_slug = ? AND consumed_batch_id IS NULL
                ORDER BY imap_uid ASC
                """
            params: list[object] = [scope.character_slug, scope.relationship_slug]
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)
            rows = conn.execute(query, params).fetchall()
        return [self._build_stored_inbound_message(row) for row in rows]

    def count_unconsumed_inbound(self) -> int:
        with self._connect() as conn:
            if not self._table_exists(conn, "inbound_messages"):
                return 0
            scope = self._require_scope()
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM inbound_messages
                WHERE character_slug = ? AND relationship_slug = ? AND consumed_batch_id IS NULL
                """,
                (scope.character_slug, scope.relationship_slug),
            ).fetchone()
            return int(row["count"]) if row else 0

    def mark_inbound_consumed(self, imap_uids: list[int], batch_id: str) -> None:
        if not imap_uids:
            return
        scope = self._require_scope()
        placeholders = ",".join("?" for _ in imap_uids)
        with self._connect() as conn:
            conn.execute(
                (
                    f"UPDATE inbound_messages SET consumed_batch_id = ? "
                    f"WHERE character_slug = ? AND relationship_slug = ? AND imap_uid IN ({placeholders})"
                ),
                (batch_id, scope.character_slug, scope.relationship_slug, *imap_uids),
            )

    def delete_inbound_message(self, imap_uid: int) -> bool:
        scope = self._require_scope()
        with self._connect() as conn:
            if not self._table_exists(conn, "inbound_messages"):
                return False
            row = conn.execute(
                """
                SELECT raw_path, normalized_text_path
                FROM inbound_messages
                WHERE character_slug = ? AND relationship_slug = ? AND imap_uid = ?
                """,
                (scope.character_slug, scope.relationship_slug, imap_uid),
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
                WHERE character_slug = ? AND relationship_slug = ? AND imap_uid = ?
                """,
                (scope.character_slug, scope.relationship_slug, imap_uid),
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
                SELECT run_id, batch_id, started_at, finished_at, mode, status, error_stage, stage1_path, stage2_path,
                       character_slug, relationship_slug
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
                SELECT run_id, batch_id, started_at, finished_at, mode, status, error_stage, stage1_path, stage2_path,
                       character_slug, relationship_slug
                FROM runs
                WHERE run_id = ? AND character_slug = ? AND relationship_slug = ?
                LIMIT 1
                """,
                (run_id, scope.character_slug, scope.relationship_slug),
            ).fetchone()
        return self._build_stored_run(row) if row else None

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
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv_state (
                character_slug TEXT NOT NULL,
                relationship_slug TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY(character_slug, relationship_slug, key)
            );

            CREATE TABLE IF NOT EXISTS inbound_messages (
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
        self._ensure_indexes(conn)

    def _ensure_indexes(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_inbound_messages_scope_consumed
                ON inbound_messages(character_slug, relationship_slug, consumed_batch_id, imap_uid);
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


def _parse_run_started_at(value: str, cutoff: datetime) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None and cutoff.tzinfo is not None:
        return parsed.replace(tzinfo=cutoff.tzinfo)
    if parsed.tzinfo is not None and cutoff.tzinfo is None:
        return parsed.replace(tzinfo=None)
    return parsed
