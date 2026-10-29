from __future__ import annotations

import logging

from .alerts import ALERT_KINDS, last_alert_at_key
from .config import load_settings
from .scheduler import SCHEDULED_FOR_DATE_KEY, SENT_TIME_KEY, TARGET_SEND_TIME_KEY
from .storage import StateStore
from .utils import now_in_timezone

IMAP_UIDVALIDITY_KEY = "imap_uidvalidity"
QUARANTINE_LIST_LIMIT = 10


def _session_label(store: StateStore) -> str:
    scope = store.scope
    if scope is None:
        return "-"
    return f"{scope.character_slug} / {scope.relationship_slug}"


def status_command(config_path: str = "config.yaml") -> int:
    settings = load_settings(config_path)
    logging.basicConfig(
        level=getattr(logging, settings.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    store = StateStore(settings)
    store.initialize()
    now = now_in_timezone(settings.project.timezone)

    print(f"Session: {_session_label(store)}")
    print(f"Now: {now.isoformat()}")

    last_success = store.get_latest_run(status="success")
    if last_success is None:
        print("Last successful run: none")
    else:
        print(
            f"Last successful run: run_id={last_success.run_id} mode={last_success.mode} "
            f"batch_id={last_success.batch_id} finished_at={last_success.finished_at or '-'}"
        )
    last_failure = store.get_latest_run(status="failed")
    if last_failure is None:
        print("Last failed run: none")
    else:
        print(
            f"Last failed run: run_id={last_failure.run_id} error_stage={last_failure.error_stage or '-'} "
            f"finished_at={last_failure.finished_at or '-'} error={last_failure.error_message or '-'}"
        )
    print(
        f"Consecutive failures: {store.get_consecutive_failures()} "
        f"last_failed_at={store.get_last_failed_at() or '-'}"
    )

    print(f"Inbound: unconsumed={store.count_unconsumed_inbound()} quarantined={store.count_quarantined_inbound()}")
    for message in store.list_quarantined_inbound(limit=QUARANTINE_LIST_LIMIT):
        print(
            f"  - quarantined uid={message.imap_uid} attempts={message.attempt_count} "
            f"quarantined_at={message.quarantined_at or '-'} last_error={message.last_error or '-'}"
        )
    print(f"Pending outbound: {store.count_pending_outbound()}")

    imap_line = (
        f"IMAP: last_seen_uid={store.get_last_seen_imap_uid()} "
        f"last_consumed_uid={store.get_last_consumed_imap_uid()}"
    )
    uidvalidity = store.get_kv(IMAP_UIDVALIDITY_KEY)
    if uidvalidity:
        imap_line += f" uidvalidity={uidvalidity}"
    print(imap_line)

    print(
        f"Schedule: scheduled_for_date={store.get_kv(SCHEDULED_FOR_DATE_KEY) or '-'} "
        f"target_send_time={store.get_kv(TARGET_SEND_TIME_KEY) or '-'} "
        f"sent_time={store.get_kv(SENT_TIME_KEY) or '-'} "
        f"runs_today={store.get_runs_today(now.date())}"
    )
    last_alerts = " ".join(f"{kind}={store.get_kv(last_alert_at_key(kind)) or 'none'}" for kind in ALERT_KINDS)
    print(f"Last alert: {last_alerts}")
    return 0


def requeue_inbound_command(config_path: str = "config.yaml", *, imap_uid: int) -> int:
    settings = load_settings(config_path)
    logging.basicConfig(
        level=getattr(logging, settings.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    store = StateStore(settings)
    store.initialize()
    requeued = store.requeue_inbound_message(imap_uid)
    if requeued is not None:
        print(f"Requeued inbound message: uid={imap_uid} uidvalidity={requeued.uidvalidity}")
        print(f"Session: {_session_label(store)}")
        print("Quarantine cleared and attempt count reset; the next run will pick it up again.")
        return 0
    existing = store.get_inbound_message(imap_uid)
    if existing is None:
        print(f"Inbound message not found: uid={imap_uid}")
    elif existing.consumed_batch_id:
        print(
            f"Inbound message is not quarantined: uid={imap_uid} uidvalidity={existing.uidvalidity} "
            f"was already answered (batch {existing.consumed_batch_id}); nothing to requeue."
        )
    else:
        print(
            f"Inbound message is not quarantined: uid={imap_uid} uidvalidity={existing.uidvalidity} "
            f"is still waiting in the queue (attempts={existing.attempt_count}); nothing to requeue."
        )
    print(f"Session: {_session_label(store)}")
    return 1
