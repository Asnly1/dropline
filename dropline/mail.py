from __future__ import annotations

import html
import imaplib
import logging
import re
import smtplib
import ssl
from collections.abc import Collection
from datetime import date
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr, make_msgid, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Protocol

from .config import SUPPORTED_SMTP_SECURITY, CharacterMailboxConfig, MailEndpointConfig, Settings, current_character_name
from .models import AttachmentMeta, NormalizedInboundMessage, RawInboundMessage
from .utils import retry_call


LOGGER = logging.getLogger(__name__)
FROM_HEADER_SECTION = "(BODY.PEEK[HEADER.FIELDS (FROM)])"
MAIL_SOCKET_TIMEOUT_SECONDS = 60.0
UIDVALIDITY_RESPONSE_CODE = "UIDVALIDITY"
# Tags are matched quote-aware so a ">" inside a quoted attribute value does not end the tag; the plain
# "<...>" alternative keeps the old behaviour for a tag with an unbalanced quote.
HTML_TAG_RE = re.compile(r"""<(?:"[^"]*"|'[^']*'|[^>"'])*>|<[^>]+>""")
SCRIPT_STYLE_RE = re.compile(r"<(script|style).*?</\1>", re.IGNORECASE | re.DOTALL)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
HTML_ELEMENT_TAG_RE = re.compile(
    r"""<(/?)([A-Za-z][A-Za-z0-9-]*)(\s(?:"[^"]*"|'[^']*'|[^>"'])*|\s[^>]*|)>""",
    re.DOTALL,
)
HTML_CLASS_ID_ATTR_RE = re.compile(r"""(?<![\w-])(?:class|id)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.IGNORECASE)
HTML_QUOTE_CONTAINER_MARKER = "quote"
# Elements that never have content or a closing tag: they neither open a quote container nor change
# the nesting depth while one is being skipped.
HTML_VOID_ELEMENTS = frozenset(
    {"img", "br", "hr", "input", "meta", "link", "source", "wbr", "area", "base", "col", "embed", "param", "track"}
)
STOP_PATTERNS = (
    re.compile(r"^>"),
    re.compile(r"^On .+wrote:$"),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}$", re.IGNORECASE),
    re.compile(r"^From:\s", re.IGNORECASE),
    # QQ 邮箱 / Foxmail: ------------------ 原始邮件 ------------------ ; Outlook: -----原始邮件-----
    re.compile(r"^-{2,}\s*原始邮件\s*-{2,}$"),
    # 中文 Outlook / QQ 邮箱引用头: 发件人: ...
    re.compile(r"^发件人[:：]"),
    # 163 / Apple Mail / Thunderbird: 在 2026-03-31 21:42:10，"刻晴" <keqing@163.com> 写道：
    # Every such header carries a clock time or an address; a user's own "在 2026-03-31 写道：" has neither.
    re.compile(r"^在\s?.*(?:\d{1,2}:\d{2}|@).*写道[:：]$"),
    # 中文 Gmail: 刻晴 <keqing@gmail.com> 于2026年3月31日周二 21:42写道：
    # Gmail always names the author as <address>; a user's own "我于2024年写道：" never does.
    re.compile(r"^(?=.*<[^<>\s]+@[^<>\s]+>).+于.*写道[:：]$"),
)
# Outlook / Exchange separates the quoted letter with a line of underscores, but users draw such
# dividers too: the line only ends the body when a quote header follows it and is dropped otherwise.
OUTLOOK_SEPARATOR_RE = re.compile(r"^_{10,}$")
QUOTE_HEADER_AFTER_SEPARATOR_RE = re.compile(r"^(发件人|寄件者|From|Von|De)[:：]")
# Shorter paragraphs (greetings, sign-offs, one-line replies) are too generic to identify quoted prose.
KNOWN_PARAGRAPH_MIN_LENGTH = 20
PARAGRAPH_BREAK_RE = re.compile(r"\n[ \t]*\n")
SIGNATURE_PATTERNS = (
    re.compile(r"^--\s*$"),
    re.compile(r"^Sent from my ", re.IGNORECASE),
)


def build_tls_context() -> ssl.SSLContext:
    """Return a TLS context that verifies server certificates.

    The standard library's IMAP4_SSL / SMTP_SSL / starttls() default to an *unverified*
    context when none is passed, so every TLS connection must go through this helper.
    Certificates are checked against the system trust store; when the Python build has no
    system CAs (python.org installers on macOS are a common case) the certifi bundle that
    the LLM SDKs already depend on is loaded instead.
    """
    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca", 0) == 0:
        try:
            import certifi
        except ImportError:  # pragma: no cover - certifi is pulled in by httpx/requests
            return context
        context.load_verify_locations(cafile=certifi.where())
    return context


def _log_certificate_failure(protocol: str, host: str, port: int, exc: BaseException) -> None:
    LOGGER.error(
        "%s TLS certificate verification failed for %s:%s: %s. dropline verifies server certificates against "
        "the system trust store (falling back to the certifi bundle). A self-signed mail server needs its CA "
        "added to the system trust store, and the host must be the name on the certificate, not an IP address.",
        protocol,
        host,
        port,
        exc,
    )


class InboundMailClient(Protocol):
    def fetch_since(
        self,
        since_uid: int,
        limit: int,
        allowlist_senders: tuple[str, ...] = (),
        received_since: date | None = None,
    ) -> tuple[list[RawInboundMessage], int, int]:
        """Return (messages, highest_seen_uid, uidvalidity); uidvalidity is 0 when the server does not report it."""
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
    ) -> tuple[list[RawInboundMessage], int, int]:
        try:
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
        except ssl.SSLCertVerificationError as exc:
            _log_certificate_failure("IMAP", self.endpoint.host, self.endpoint.port, exc)
            raise

    def _fetch_since_once(
        self,
        *,
        since_uid: int,
        limit: int,
        allowlist_senders: tuple[str, ...],
        received_since: date | None,
    ) -> tuple[list[RawInboundMessage], int, int]:
        if not self.endpoint.folder:
            raise RuntimeError("IMAP folder configuration is required.")
        with self._connect() as client:
            client.login(self.endpoint.username, self.endpoint.password)
            client.select(self.endpoint.folder)
            uidvalidity = self._read_uidvalidity(client)
            base_criteria = self._build_search_criteria(since_uid=since_uid, received_since=received_since)
            # RFC 3501: "UID n:*" always matches the highest UID in the mailbox even when it is below n,
            # so the already-seen cursor message must be dropped here rather than re-fetched every run.
            uids = [uid for uid in self._search_uids(client, base_criteria) if uid > since_uid]
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
            return messages, highest_seen_uid, uidvalidity

    def _read_uidvalidity(self, client: imaplib.IMAP4) -> int:
        """Return the UIDVALIDITY reported by the last SELECT, or 0 when the server omitted it."""
        _, data = client.response(UIDVALIDITY_RESPONSE_CODE)
        for item in data or []:
            if isinstance(item, (bytes, bytearray)):
                item = item.decode("ascii", errors="ignore")
            if isinstance(item, str) and item.strip().isdigit():
                return int(item.strip())
        LOGGER.warning(
            "IMAP server did not report UIDVALIDITY for folder %s; UID cursor changes cannot be detected.",
            self.endpoint.folder,
        )
        return 0

    def _connect(self) -> imaplib.IMAP4:
        if self.endpoint.ssl:
            return imaplib.IMAP4_SSL(
                self.endpoint.host,
                self.endpoint.port,
                ssl_context=build_tls_context(),
                timeout=MAIL_SOCKET_TIMEOUT_SECONDS,
            )
        client = imaplib.IMAP4(self.endpoint.host, self.endpoint.port, timeout=MAIL_SOCKET_TIMEOUT_SECONDS)
        LOGGER.warning(
            "IMAP connection to %s:%s is unencrypted (imap.ssl=false); credentials and mail are sent in cleartext.",
            self.endpoint.host,
            self.endpoint.port,
        )
        return client

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
        try:
            return retry_call(
                lambda: self._send_once(message),
                operation_name="SMTP send",
                logger=LOGGER,
            )
        except ssl.SSLCertVerificationError as exc:
            _log_certificate_failure("SMTP", self.mailbox.smtp.host, self.mailbox.smtp.port, exc)
            raise

    def _send_once(self, message: EmailMessage) -> str:
        endpoint = self.mailbox.smtp
        message_id = message.get("Message-ID")
        if endpoint.security not in SUPPORTED_SMTP_SECURITY:
            raise ValueError(f"Unsupported smtp.security value: {endpoint.security!r}")
        if endpoint.security == "ssl":
            client = smtplib.SMTP_SSL(
                endpoint.host,
                endpoint.port,
                timeout=MAIL_SOCKET_TIMEOUT_SECONDS,
                context=build_tls_context(),
            )
        else:
            client = smtplib.SMTP(endpoint.host, endpoint.port, timeout=MAIL_SOCKET_TIMEOUT_SECONDS)
        try:
            if endpoint.security == "starttls":
                client.starttls(context=build_tls_context())
            else:
                LOGGER.warning(
                    "SMTP connection to %s:%s is unencrypted (smtp.security=none); credentials are sent in cleartext.",
                    endpoint.host,
                    endpoint.port,
                )
            client.login(endpoint.username, endpoint.password)
            client.send_message(message)
        except Exception:
            client.close()
            raise
        try:
            client.quit()
        except Exception as exc:
            LOGGER.warning(
                "SMTP QUIT failed after message was already sent successfully; treating send as successful: %s",
                exc,
            )
            client.close()
        return message_id


def normalize_inbound_message(raw_message: RawInboundMessage, *, uidvalidity: int = 0) -> NormalizedInboundMessage:
    message = BytesParser(policy=policy.default).parsebytes(raw_message.raw_bytes)
    from_email = parseaddr(message.get("From", ""))[1].lower()
    subject = _decode_header_value(message.get("Subject", ""))
    received_at = _parse_received_at(message.get("Date"))
    plain_body, html_body = _extract_body(message)
    body_text = _cleanup_body(plain_body or html_body)
    attachments = _extract_attachments(message)
    thread_message_id = message.get("In-Reply-To") or None
    # A synthesized id must carry the UIDVALIDITY epoch: after a mailbox rebuild a different letter can
    # arrive under the same UID, and it must not look like one that is already stored.
    message_id = message.get("Message-ID") or f"<imap-{uidvalidity}-{raw_message.imap_uid}@dropline.invalid>"
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
        uidvalidity=uidvalidity,
    )


def extract_plain_body(raw_bytes: bytes) -> str:
    """Return the text body of an archived .eml (plain part preferred) without quote stripping."""
    message = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    plain_body, html_body = _extract_body(message)
    return plain_body or html_body


def paragraph_keys(text: str) -> set[str]:
    """Normalized keys for every paragraph of ``text`` that is long enough to identify quoted prose."""
    keys: set[str] = set()
    for paragraph in _split_paragraphs(text):
        key = _paragraph_key(paragraph)
        if len(key) >= KNOWN_PARAGRAPH_MIN_LENGTH:
            keys.add(key)
    return keys


def strip_known_paragraphs(body_text: str, known_keys: Collection[str]) -> str:
    """Drop paragraphs of ``body_text`` that repeat a known paragraph verbatim (whitespace-insensitive).

    Mail clients that quote the previous letter without any recognizable marker leave the
    character's own prose inside the user's reply; comparing paragraphs against the character's
    recent outbound letters removes it so it is never attributed to the user.
    """
    if not body_text or not known_keys:
        return body_text
    paragraphs = _split_paragraphs(body_text)
    kept = [paragraph for paragraph in paragraphs if _paragraph_key(paragraph) not in known_keys]
    if len(kept) == len(paragraphs):
        return body_text
    return "\n\n".join(kept).strip()


def _split_paragraphs(text: str) -> list[str]:
    return [segment.strip() for segment in PARAGRAPH_BREAK_RE.split(text) if segment.strip()]


def _paragraph_key(paragraph: str) -> str:
    return "".join(paragraph.split()).lower()


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
            if _attachment_inline_flag(part) is not None:
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain":
                plain_parts.append(_get_part_text(part))
            elif content_type == "text/html":
                html_parts.append(_html_to_text(_get_part_text(part)))
    elif _attachment_inline_flag(message) is None:
        payload = _get_part_text(message)
        if message.get_content_type() == "text/html":
            html_parts.append(_html_to_text(payload))
        else:
            plain_parts.append(payload)
    return "\n".join(item.strip() for item in plain_parts if item).strip(), "\n".join(
        item.strip() for item in html_parts if item
    ).strip()


def _get_part_text(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except LookupError:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        return payload.decode("utf-8", errors="replace")


def _extract_attachments(message: EmailMessage) -> list[AttachmentMeta]:
    """Collect attachment metadata only; payloads are measured for size and never kept."""
    attachments: list[AttachmentMeta] = []
    for part in message.walk():
        inline = _attachment_inline_flag(part)
        if inline is None:
            continue
        payload = part.get_payload(decode=True) or b""
        attachments.append(
            AttachmentMeta(
                filename=part.get_filename() or "",
                content_type=part.get_content_type(),
                size=len(payload),
                inline=inline,
            )
        )
    return attachments


def _attachment_inline_flag(part: EmailMessage) -> bool | None:
    """Return the ``inline`` flag of a part that counts as an attachment, or ``None`` for a body part.

    ``Content-Disposition: attachment`` always wins. Every ``image/*`` leaf is an attachment whatever its
    disposition (inline when it has a Content-ID or is declared inline). Any other inline part that
    carries a file name (a .txt, .ics or .pdf shown "inline") is an inline attachment, so a text file
    never leaks into the letter's body.
    """
    if part.is_multipart():
        return None
    disposition = part.get_content_disposition()
    if disposition == "attachment":
        return False
    if part.get_content_maintype() == "image":
        return disposition == "inline" or part.get("Content-ID") is not None
    if disposition == "inline" and part.get_filename():
        return True
    return None


def _html_to_text(value: str) -> str:
    cleaned = SCRIPT_STYLE_RE.sub("", value)
    cleaned = _strip_quoted_html(cleaned)
    cleaned = HTML_TAG_RE.sub("\n", cleaned)
    return html.unescape(cleaned)


def _strip_quoted_html(value: str) -> str:
    """Remove quoted-reply containers (<blockquote>, gmail_quote, any *quote* id/class) as whole elements.

    Comments are dropped first so commented-out markup cannot open or close a container. Nested
    containers are tracked by tag name so a quote that itself contains quotes is removed in one piece;
    void elements never count, and an unterminated container swallows the rest of the document, which
    is where quotes live.
    """
    value = HTML_COMMENT_RE.sub("", value)
    kept: list[str] = []
    cursor = 0
    depth = 0
    container_tag = ""
    for match in HTML_ELEMENT_TAG_RE.finditer(value):
        closing = match.group(1) == "/"
        tag_name = match.group(2).lower()
        if tag_name in HTML_VOID_ELEMENTS:
            continue
        attributes = match.group(3)
        self_closing = attributes.rstrip().endswith("/")
        if depth == 0:
            if closing or self_closing or not _is_quoted_html_container(tag_name, attributes):
                continue
            kept.append(value[cursor:match.start()])
            container_tag = tag_name
            depth = 1
            continue
        if tag_name != container_tag or self_closing:
            continue
        depth += -1 if closing else 1
        if depth == 0:
            cursor = match.end()
    if depth == 0:
        kept.append(value[cursor:])
    return "".join(kept)


def _is_quoted_html_container(tag_name: str, attributes: str) -> bool:
    if tag_name == "blockquote":
        return True
    for match in HTML_CLASS_ID_ATTR_RE.finditer(attributes):
        attribute_value = next(group for group in match.groups() if group is not None)
        if HTML_QUOTE_CONTAINER_MARKER in attribute_value.lower():
            return True
    return False


def _cleanup_body(body_text: str) -> str:
    lines: list[str] = []
    raw_lines = [raw_line.rstrip() for raw_line in body_text.splitlines()]
    for index, line in enumerate(raw_lines):
        if OUTLOOK_SEPARATOR_RE.match(line):
            if _quote_header_follows(raw_lines, index + 1):
                break
            continue
        if any(pattern.search(line) for pattern in STOP_PATTERNS):
            break
        if any(pattern.search(line) for pattern in SIGNATURE_PATTERNS):
            break
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def _quote_header_follows(lines: list[str], start: int) -> bool:
    """True when the first non-blank line at or after ``start`` is a quote header (发件人: / From: ...)."""
    for line in lines[start:]:
        if line.strip():
            return QUOTE_HEADER_AFTER_SEPARATOR_RE.match(line.strip()) is not None
    return False
