from __future__ import annotations

import html
import imaplib
import logging
import re
import smtplib
from datetime import date
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr, make_msgid, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Protocol

from .config import CharacterMailboxConfig, MailEndpointConfig, Settings, current_character_name
from .models import AttachmentMeta, NormalizedInboundMessage, RawInboundMessage
from .utils import retry_call


LOGGER = logging.getLogger(__name__)
FROM_HEADER_SECTION = "(BODY.PEEK[HEADER.FIELDS (FROM)])"
HTML_TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_STYLE_RE = re.compile(r"<(script|style).*?</\1>", re.IGNORECASE | re.DOTALL)
STOP_PATTERNS = (
    re.compile(r"^>"),
    re.compile(r"^On .+wrote:$"),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}$", re.IGNORECASE),
    re.compile(r"^From:\s", re.IGNORECASE),
)
SIGNATURE_PATTERNS = (
    re.compile(r"^--\s*$"),
    re.compile(r"^Sent from my ", re.IGNORECASE),
)


class InboundMailClient(Protocol):
    def fetch_since(
        self,
        since_uid: int,
        limit: int,
        allowlist_senders: tuple[str, ...] = (),
        received_since: date | None = None,
    ) -> tuple[list[RawInboundMessage], int]:
        ...


class OutboundMailClient(Protocol):
    def send(self, message: EmailMessage) -> str:
        ...


class IMAPInboundClient:
    def __init__(self, endpoint: MailEndpointConfig) -> None:
        self.endpoint = endpoint

    def fetch_since(
        self,
        since_uid: int,
        limit: int,
        allowlist_senders: tuple[str, ...] = (),
        received_since: date | None = None,
    ) -> tuple[list[RawInboundMessage], int]:
        return retry_call(
            lambda: self._fetch_since_once(
                since_uid=since_uid,
                limit=limit,
                allowlist_senders=allowlist_senders,
                received_since=received_since,
            ),
            operation_name="IMAP fetch",
            logger=LOGGER,
        )

    def _fetch_since_once(
        self,
        *,
        since_uid: int,
        limit: int,
        allowlist_senders: tuple[str, ...],
        received_since: date | None,
    ) -> tuple[list[RawInboundMessage], int]:
        if not self.endpoint.folder:
            raise RuntimeError("IMAP folder configuration is required.")
        client_cls = imaplib.IMAP4_SSL if self.endpoint.ssl else imaplib.IMAP4
        with client_cls(self.endpoint.host, self.endpoint.port) as client:
            client.login(self.endpoint.username, self.endpoint.password)
            client.select(self.endpoint.folder)
            base_criteria = self._build_search_criteria(since_uid=since_uid, received_since=received_since)
            uids = self._search_uids(client, base_criteria)
            selected_uids = uids[:limit] if limit > 0 else uids
            highest_seen_uid = max(selected_uids, default=0)
            LOGGER.info(
                "IMAP search complete: %s new UID(s) since %s.",
                len(selected_uids),
                since_uid,
            )
            if allowlist_senders:
                matched_uids = self._filter_allowlist_by_from_header(client, selected_uids, allowlist_senders)
                LOGGER.info(
                    "IMAP allowlist scan complete: %s/%s message(s) matched.",
                    len(matched_uids),
                    len(selected_uids),
                )
            else:
                matched_uids = selected_uids
            messages: list[RawInboundMessage] = []
            for uid in matched_uids:
                raw_bytes = self._fetch_section(client, uid, "BODY.PEEK[]")
                messages.append(RawInboundMessage(imap_uid=uid, raw_bytes=raw_bytes))
            return messages, highest_seen_uid

    def _filter_allowlist_by_from_header(
        self,
        client: imaplib.IMAP4,
        uids: list[int],
        allowlist_senders: tuple[str, ...],
    ) -> list[int]:
        allowlist = {sender.lower() for sender in allowlist_senders}
        matched_uids: list[int] = []
        for uid in uids:
            try:
                header_bytes = self._fetch_section(client, uid, FROM_HEADER_SECTION)
            except RuntimeError as exc:
                LOGGER.info("IMAP allowlist header scan skipped UID %s: %s", uid, exc)
                continue
            from_email = _parse_from_header(header_bytes)
            if from_email in allowlist:
                matched_uids.append(uid)
        return matched_uids

    def _fetch_section(self, client: imaplib.IMAP4, uid: int, section: str) -> bytes:
        status, fetch_data = client.uid("fetch", str(uid), section)
        if status != "OK" or not fetch_data:
            raise RuntimeError(f"Failed to fetch UID {uid}")
        for item in fetch_data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            payload = item[1]
            if isinstance(payload, bytes):
                return payload
        raise RuntimeError(f"No bytes payload returned when fetching UID {uid}")

    def _search_uids(self, client: imaplib.IMAP4, criteria: tuple[str, ...]) -> list[int]:
        status, data = client.uid("search", None, *criteria)
        if status != "OK":
            raise RuntimeError("Failed to search mailbox")
        return [int(item) for item in data[0].split()] if data and data[0] else []

    def _build_search_criteria(self, *, since_uid: int, received_since: date | None) -> tuple[str, ...]:
        criteria: list[str] = []
        if since_uid > 0:
            criteria.extend(["UID", f"{since_uid + 1}:*"])
        if received_since is not None:
            criteria.extend(["SINCE", received_since.strftime("%d-%b-%Y")])
        if not criteria:
            criteria.append("ALL")
        return tuple(criteria)


class SMTPOutboundClient:
    def __init__(self, mailbox: CharacterMailboxConfig) -> None:
        self.mailbox = mailbox

    def send(self, message: EmailMessage) -> str:
        message_id = message.get("Message-ID")
        if not message_id:
            message_id = make_msgid(domain=self.mailbox.address.split("@", 1)[-1])
            message["Message-ID"] = message_id
        return retry_call(
            lambda: self._send_once(message),
            operation_name="SMTP send",
            logger=LOGGER,
        )

    def _send_once(self, message: EmailMessage) -> str:
        endpoint = self.mailbox.smtp
        message_id = message.get("Message-ID")
        if not message_id:
            raise RuntimeError("Email Message-ID must be assigned before SMTP send.")
        smtp_cls = smtplib.SMTP_SSL if endpoint.security == "ssl" else smtplib.SMTP
        with smtp_cls(endpoint.host, endpoint.port) as client:
            if endpoint.security == "starttls":
                client.starttls()
            client.login(endpoint.username, endpoint.password)
            client.send_message(message)
        return message_id


def normalize_inbound_message(raw_message: RawInboundMessage) -> NormalizedInboundMessage:
    message = BytesParser(policy=policy.default).parsebytes(raw_message.raw_bytes)
    from_email = parseaddr(message.get("From", ""))[1].lower()
    subject = _decode_header_value(message.get("Subject", ""))
    received_at = _parse_received_at(message.get("Date"))
    plain_body, html_body = _extract_body(message)
    body_text = _cleanup_body(plain_body or html_body)
    attachments = _extract_attachments(message)
    thread_message_id = message.get("In-Reply-To") or None
    message_id = message.get("Message-ID") or f"<imap-uid-{raw_message.imap_uid}>"
    return NormalizedInboundMessage(
        message_id=message_id,
        imap_uid=raw_message.imap_uid,
        from_email=from_email,
        subject=subject,
        received_at=received_at,
        body_text=body_text,
        body_html_present=bool(html_body),
        attachments=attachments,
        thread_message_id=thread_message_id,
    )


def _parse_from_header(header_bytes: bytes) -> str:
    message = BytesParser(policy=policy.default).parsebytes(header_bytes)
    return parseaddr(message.get("From", ""))[1].lower()


def compose_email(
    settings: Settings,
    *,
    subject: str,
    body_text: str,
    image_path: Path | None,
    reply_to_message_id: str | None,
    mode: str,
    message_id: str | None = None,
) -> EmailMessage:
    mailbox = settings.mail.character_mailbox
    message = EmailMessage()
    message["From"] = formataddr((current_character_name(settings), mailbox.address))
    message["To"] = settings.mail.primary_user_recipient
    final_subject = subject
    if mode == "reply" and not final_subject.lower().startswith("re:"):
        final_subject = f"Re: {final_subject}"
    message["Subject"] = final_subject
    message["Message-ID"] = message_id or make_msgid(domain=mailbox.address.split("@", 1)[-1])
    if mode == "reply" and reply_to_message_id:
        message["In-Reply-To"] = reply_to_message_id
        message["References"] = reply_to_message_id
    message.set_content(body_text)
    message.add_alternative(plain_text_to_html(body_text), subtype="html")
    if image_path and image_path.exists():
        message.add_attachment(
            image_path.read_bytes(),
            maintype="image",
            subtype=_image_subtype(image_path),
            filename=image_path.name,
        )
    return message


def plain_text_to_html(body_text: str) -> str:
    escaped = html.escape(body_text)
    paragraphs = [segment.replace("\n", "<br/>") for segment in escaped.split("\n\n") if segment.strip()]
    return "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)


def _image_subtype(image_path: Path) -> str:
    suffix = image_path.suffix.lower().lstrip(".")
    if suffix == "jpg":
        return "jpeg"
    return suffix or "png"


def _decode_header_value(value: str) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def _parse_received_at(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
    except (AttributeError, IndexError, TypeError, ValueError):
        return ""
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=None).isoformat()
    return parsed.isoformat()


def _extract_body(message: EmailMessage) -> tuple[str, str]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain":
                plain_parts.append(part.get_content())
            elif content_type == "text/html":
                html_parts.append(_html_to_text(part.get_content()))
    else:
        payload = message.get_content()
        if message.get_content_type() == "text/html":
            html_parts.append(_html_to_text(payload))
        else:
            plain_parts.append(payload)
    return "\n".join(item.strip() for item in plain_parts if item).strip(), "\n".join(
        item.strip() for item in html_parts if item
    ).strip()


def _extract_attachments(message: EmailMessage) -> list[AttachmentMeta]:
    attachments: list[AttachmentMeta] = []
    for part in message.walk():
        if part.get_content_disposition() != "attachment":
            continue
        payload = part.get_payload(decode=True) or b""
        attachments.append(
            AttachmentMeta(
                filename=part.get_filename() or "",
                content_type=part.get_content_type(),
                size=len(payload),
            )
        )
    return attachments


def _html_to_text(value: str) -> str:
    cleaned = SCRIPT_STYLE_RE.sub("", value)
    cleaned = HTML_TAG_RE.sub("\n", cleaned)
    return html.unescape(cleaned)


def _cleanup_body(body_text: str) -> str:
    lines: list[str] = []
    for raw_line in body_text.splitlines():
        line = raw_line.rstrip()
        if any(pattern.search(line) for pattern in STOP_PATTERNS):
            break
        if any(pattern.search(line) for pattern in SIGNATURE_PATTERNS):
            break
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned
