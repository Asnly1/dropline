from __future__ import annotations

import logging
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .config import Settings
from .llm_contracts import ENGLISH_OUTPUT_LANGUAGE
from .mail import OutboundMailClient, SMTPOutboundClient
from .storage import LAST_ALERT_AT_KEY, StateStore

LOGGER = logging.getLogger(__name__)
ALERT_SENDER_NAME = "dropline"
ALERT_SUBJECT_PREFIX = "[dropline] "


def build_alert_email(settings: Settings, *, subject: str, body: str) -> EmailMessage:
    """Compose a plain-text system notification sent from the character mailbox.

    The display name is always ``dropline`` (never the character name) so the
    recipient can tell a system notice apart from a letter written in character.
    """
    mailbox = settings.mail.character_mailbox
    message = EmailMessage()
    message["From"] = formataddr((ALERT_SENDER_NAME, mailbox.address))
    message["To"] = settings.mail.alerts.recipient
    message["Subject"] = subject if subject.startswith(ALERT_SUBJECT_PREFIX) else f"{ALERT_SUBJECT_PREFIX}{subject}"
    message["Message-ID"] = make_msgid(domain=mailbox.address.split("@", 1)[-1])
    message.set_content(body)
    return message


def alert_already_sent_today(store: StateStore, now: datetime) -> bool:
    raw_value = (store.get_kv(LAST_ALERT_AT_KEY) or "").strip()
    if not raw_value:
        return False
    try:
        last_alert_at = datetime.fromisoformat(raw_value)
    except ValueError:
        return False
    if last_alert_at.tzinfo is None:
        last_alert_at = last_alert_at.replace(tzinfo=now.tzinfo)
    if now.tzinfo is not None:
        last_alert_at = last_alert_at.astimezone(now.tzinfo)
    return last_alert_at.date() == now.date()


def send_alert(
    settings: Settings,
    store: StateStore,
    *,
    subject: str,
    body: str,
    now: datetime,
    outbound_client: OutboundMailClient | None = None,
) -> bool:
    """Send one alert email, at most once per day. Never raises: alert failures only get logged."""
    alerts = settings.mail.alerts
    if not alerts.enabled:
        LOGGER.info("Alert suppressed: mail.alerts.enabled=false (%s)", subject)
        return False
    if alert_already_sent_today(store, now):
        LOGGER.info("Alert suppressed: an alert was already sent today (%s)", subject)
        return False
    try:
        client = outbound_client or SMTPOutboundClient(settings.mail.character_mailbox)
        message = build_alert_email(settings, subject=subject, body=body)
        client.send(message)
        store.set_kv(LAST_ALERT_AT_KEY, now.isoformat())
    except Exception:
        LOGGER.exception("Failed to send alert email to %s; continuing without the alert", alerts.recipient)
        return False
    LOGGER.warning("Alert email sent to %s: %s", alerts.recipient, message["Subject"])
    return True


def notify_run_failure(
    settings: Settings,
    store: StateStore,
    *,
    run_id: str,
    mode: str,
    batch_id: str,
    error_stage: str,
    message: str,
    consecutive_failures: int,
    quarantined_uids: list[int],
    now: datetime,
    outbound_client: OutboundMailClient | None = None,
) -> bool:
    """Alert when consecutive failures reach the threshold or inbound mail got quarantined."""
    threshold_reached = consecutive_failures >= settings.mail.alerts.after_failures
    if not threshold_reached and not quarantined_uids:
        return False
    english = settings.llm.language == ENGLISH_OUTPUT_LANGUAGE
    subject = _format_subject(
        error_stage=error_stage,
        consecutive_failures=consecutive_failures,
        quarantined_uids=quarantined_uids,
        english=english,
    )
    body = _format_body(
        settings,
        store,
        run_id=run_id,
        mode=mode,
        batch_id=batch_id,
        error_stage=error_stage,
        message=message,
        consecutive_failures=consecutive_failures,
        quarantined_uids=quarantined_uids,
        now=now,
        english=english,
    )
    return send_alert(settings, store, subject=subject, body=body, now=now, outbound_client=outbound_client)


def _format_uids(quarantined_uids: list[int]) -> str:
    return ", ".join(str(uid) for uid in quarantined_uids)


def _format_subject(
    *,
    error_stage: str,
    consecutive_failures: int,
    quarantined_uids: list[int],
    english: bool,
) -> str:
    if quarantined_uids:
        if english:
            return f"{ALERT_SUBJECT_PREFIX}Inbound mail quarantined: uid {_format_uids(quarantined_uids)} (stage {error_stage})"
        return f"{ALERT_SUBJECT_PREFIX}来信已被隔离：uid {_format_uids(quarantined_uids)}（阶段 {error_stage}）"
    if english:
        return f"{ALERT_SUBJECT_PREFIX}{consecutive_failures} consecutive run failures (stage {error_stage})"
    return f"{ALERT_SUBJECT_PREFIX}已连续失败 {consecutive_failures} 次（阶段 {error_stage}）"


def _session_label(store: StateStore) -> str:
    scope = store.scope
    if scope is None:
        return "-"
    return f"{scope.character_slug} / {scope.relationship_slug}"


def _format_body(
    settings: Settings,
    store: StateStore,
    *,
    run_id: str,
    mode: str,
    batch_id: str,
    error_stage: str,
    message: str,
    consecutive_failures: int,
    quarantined_uids: list[int],
    now: datetime,
    english: bool,
) -> str:
    threshold = settings.mail.alerts.after_failures
    session_label = _session_label(store)
    if english:
        lines = [
            "This is an automatic system notification from dropline. It is not a letter from the character.",
            "",
            f"Session: {session_label}",
            f"Run: {run_id} (mode {mode or '-'}, batch {batch_id or '-'})",
            f"Time: {now.isoformat()}",
            f"Failed stage: {error_stage}",
            f"Error: {message or '-'}",
            f"Consecutive failures: {consecutive_failures} (alert threshold {threshold})",
        ]
        if quarantined_uids:
            lines.extend(
                [
                    f"Quarantined inbound UID(s): {_format_uids(quarantined_uids)}",
                    "Quarantined mail is no longer processed automatically; the next scheduled run falls back to an initiative letter.",
                ]
            )
        lines.extend(
            [
                "",
                "Suggested next steps:",
                f"1. Check the logs under runtime_logs/ for run_id={run_id}.",
                "2. Run `dropline status` to inspect the current session state.",
                "3. After fixing the cause, run `dropline requeue-inbound --uid <UID>` to put quarantined mail back into the queue.",
            ]
        )
        return "\n".join(lines) + "\n"
    lines = [
        "这是 dropline 自动发送的系统通知，不是角色来信。",
        "",
        f"会话：{session_label}",
        f"运行：{run_id}（模式 {mode or '-'}，批次 {batch_id or '-'}）",
        f"时间：{now.isoformat()}",
        f"失败阶段：{error_stage}",
        f"错误信息：{message or '-'}",
        f"连续失败次数：{consecutive_failures}（告警阈值 {threshold}）",
    ]
    if quarantined_uids:
        lines.extend(
            [
                f"已隔离的来信 UID：{_format_uids(quarantined_uids)}",
                "被隔离的来信不会再被自动处理；下一次调度会恢复为主动来信模式。",
            ]
        )
    lines.extend(
        [
            "",
            "建议的下一步：",
            f"1. 查看 runtime_logs/ 下的日志，搜索 run_id={run_id}。",
            "2. 运行 `dropline status` 查看当前会话状态。",
            "3. 排查并修复问题后，运行 `dropline requeue-inbound --uid <UID>` 让被隔离的来信重新进入队列。",
        ]
    )
    return "\n".join(lines) + "\n"
