from __future__ import annotations

from datetime import date
from email.message import EmailMessage
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import patch

import dropline.mail as mail_module
import dropline.config as config_module
from dropline.config import CharacterMailboxConfig, MailEndpointConfig
from dropline.mail import IMAPInboundClient, SMTPOutboundClient, normalize_inbound_message
from dropline.models import RawInboundMessage, Stage2Result
from tests.helpers import create_test_repo, load_test_settings


RAW_EMAIL = """From: User <user@example.com>
To: keqing@example.com
Subject: =?utf-8?b?5LuK5aSp5pyJ54K557Sv?=
Date: Tue, 31 Mar 2026 21:42:10 +0800
Message-ID: <msg-1@example.com>
Content-Type: multipart/alternative; boundary="BOUNDARY"

--BOUNDARY
Content-Type: text/plain; charset="utf-8"

今天写了一天作业，有点累。
On Mon, someone wrote:
> old quote
-- 
signature

--BOUNDARY
Content-Type: text/html; charset="utf-8"

<p>今天写了一天作业，有点累。</p>
--BOUNDARY--
""".encode("utf-8")


class MailNormalizationTests(unittest.TestCase):
    def test_normalize_inbound_message_prefers_plain_text_and_strips_quotes(self) -> None:
        normalized = normalize_inbound_message(RawInboundMessage(imap_uid=1, raw_bytes=RAW_EMAIL))
        self.assertEqual(normalized.message_id, "<msg-1@example.com>")
        self.assertEqual(normalized.from_email, "user@example.com")
        self.assertEqual(normalized.subject, "今天有点累")
        self.assertEqual(normalized.body_text, "今天写了一天作业，有点累。")
        self.assertTrue(normalized.body_html_present)

    def test_normalize_inbound_message_ignores_malformed_date(self) -> None:
        raw_email = """From: User <user@example.com>
To: keqing@example.com
Subject: bad date
Date: definitely not a date
Message-ID: <bad-date@example.com>
Content-Type: text/plain; charset="utf-8"

今天写了一天作业。
""".encode("utf-8")

        normalized = normalize_inbound_message(RawInboundMessage(imap_uid=2, raw_bytes=raw_email))

        self.assertEqual(normalized.received_at, "")

    def test_plain_text_to_html_does_not_render_markdown(self) -> None:
        rendered = mail_module.plain_text_to_html("第一段 **不渲染**\n下一行\n\n<script>x</script>")

        self.assertIn("<p>第一段 **不渲染**<br/>下一行</p>", rendered)
        self.assertIn("<p>&lt;script&gt;x&lt;/script&gt;</p>", rendered)
        self.assertNotIn("<strong>", rendered)
        self.assertNotIn("<script>", rendered)

    def test_stage2_literal_newlines_render_as_email_paragraphs(self) -> None:
        stage2 = Stage2Result.from_dict(
            {
                "subject": "测试",
                "body": "第一段\\n\\n第二段",
                "character_episode": {
                    "summary": "测试摘要",
                    "continuity_value": "测试连续性",
                },
                "active_arc_update_suggestion": {
                    "action": "none",
                    "content": "",
                },
            }
        )

        rendered = mail_module.plain_text_to_html(stage2.body)

        self.assertEqual(rendered, "<p>第一段</p><p>第二段</p>")

    def test_compose_email_maps_jpg_attachment_to_jpeg_subtype(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            image_path = settings.documents.session_archive_dir / "scene.jpg"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(b"not-a-real-jpeg")

            message = mail_module.compose_email(
                settings,
                subject="测试",
                body_text="这是一封纯文本邮件。",
                image_path=image_path,
                reply_to_message_id=None,
                mode="initiative",
            )

        attachments = list(message.iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_content_type(), "image/jpeg")

    def test_imap_fetch_since_uses_oldest_uids_within_limit(self) -> None:
        fake_client = _FakeIMAPClient()
        fake_client.search_results = {
            ("ALL",): b"1 2 3 4 5",
        }
        fake_client.header_payloads = {
            1: b"From: Keqing <keqing@qq.com>\r\n\r\n",
            2: b"From: Keqing <keqing@qq.com>\r\n\r\n",
        }
        fake_client.fetch_payloads = {1: b"message-1", 2: b"message-2"}
        endpoint = MailEndpointConfig(
            host="imap.qq.com",
            port=993,
            ssl=True,
            username="keqing@qq.com",
            password="secret",
            folder="INBOX",
        )
        with patch("dropline.mail.imaplib.IMAP4_SSL", return_value=fake_client):
            messages, highest_seen_uid = IMAPInboundClient(endpoint).fetch_since(
                0,
                2,
                ("keqing@qq.com",),
            )

        self.assertEqual([item.imap_uid for item in messages], [1, 2])
        self.assertEqual(highest_seen_uid, 2)
        self.assertEqual(fake_client.header_fetch_uids, [1, 2])
        self.assertEqual(fake_client.full_fetch_sections, ["BODY.PEEK[]", "BODY.PEEK[]"])

    def test_imap_fetch_section_skips_non_payload_response_items(self) -> None:
        fake_client = _FakeIMAPClient()
        fake_client.search_results = {
            ("ALL",): b"1",
        }
        fake_client.header_payloads = {1: b"From: Keqing <keqing@qq.com>\r\n\r\n"}
        fake_client.raw_fetch_data_by_uid = {
            1: [b"ignored", (b"FLAGS (\\Seen)", None), (b"BODY[]", b"message-1"), b")"],
        }
        endpoint = MailEndpointConfig(
            host="imap.qq.com",
            port=993,
            ssl=True,
            username="keqing@qq.com",
            password="secret",
            folder="INBOX",
        )
        with patch("dropline.mail.imaplib.IMAP4_SSL", return_value=fake_client):
            messages, highest_seen_uid = IMAPInboundClient(endpoint).fetch_since(0, 0, ("keqing@qq.com",))

        self.assertEqual([item.raw_bytes for item in messages], [b"message-1"])
        self.assertEqual(highest_seen_uid, 1)

    def test_imap_fetch_section_raises_when_response_has_no_bytes_payload(self) -> None:
        fake_client = _FakeIMAPClient()
        fake_client.search_results = {
            ("ALL",): b"1",
        }
        fake_client.header_payloads = {1: b"From: Keqing <keqing@qq.com>\r\n\r\n"}
        fake_client.raw_fetch_data_by_uid = {
            1: [b"ignored", (b"FLAGS (\\Seen)", None), b")"],
        }
        endpoint = MailEndpointConfig(
            host="imap.qq.com",
            port=993,
            ssl=True,
            username="keqing@qq.com",
            password="secret",
            folder="INBOX",
        )
        with patch("dropline.mail.imaplib.IMAP4_SSL", return_value=fake_client):
            with self.assertRaisesRegex(RuntimeError, "No bytes payload"):
                IMAPInboundClient(endpoint).fetch_since(0, 0, ("keqing@qq.com",))

    def test_imap_fetch_since_filters_allowlist_by_from_header_before_full_fetch(self) -> None:
        fake_client = _FakeIMAPClient()
        fake_client.search_results = {
            ("UID", "21:*"): b"21 22 23",
            ("UID", "21:*", "FROM", '"user@example.com"'): b"",
        }
        fake_client.header_payloads = {
            21: b"From: User <user@example.com>\r\n\r\n",
            22: b"From: Other <other@example.com>\r\n\r\n",
            23: b"From: User <user@example.com>\r\n\r\n",
        }
        fake_client.fetch_payloads = {21: b"message-21", 23: b"message-23"}
        endpoint = MailEndpointConfig(
            host="imap.qq.com",
            port=993,
            ssl=True,
            username="keqing@qq.com",
            password="secret",
            folder="INBOX",
        )
        with patch("dropline.mail.imaplib.IMAP4_SSL", return_value=fake_client):
            messages, highest_seen_uid = IMAPInboundClient(endpoint).fetch_since(20, 0, ("user@example.com",))

        self.assertEqual([item.imap_uid for item in messages], [21, 23])
        self.assertEqual(highest_seen_uid, 23)
        self.assertEqual(fake_client.header_fetch_uids, [21, 22, 23])
        self.assertEqual(fake_client.full_fetch_uids, [21, 23])

    def test_imap_fetch_since_checks_all_headers_when_allowlist_search_lags(self) -> None:
        fake_client = _FakeIMAPClient()
        all_uids = list(range(101, 201))
        fake_client.search_results = {
            ("UID", "101:*"): " ".join(str(uid) for uid in all_uids).encode("ascii"),
            ("UID", "101:*", "FROM", '"user@example.com"'): b"",
        }
        fake_client.header_payloads = {
            uid: (
                b"From: User <user@example.com>\r\n\r\n"
                if uid == 120
                else b"From: Other <other@example.com>\r\n\r\n"
            )
            for uid in all_uids
        }
        fake_client.fetch_payloads = {120: b"message-120"}
        endpoint = MailEndpointConfig(
            host="imap.qq.com",
            port=993,
            ssl=True,
            username="keqing@qq.com",
            password="secret",
            folder="INBOX",
        )
        with patch("dropline.mail.imaplib.IMAP4_SSL", return_value=fake_client):
            messages, highest_seen_uid = IMAPInboundClient(endpoint).fetch_since(100, 0, ("user@example.com",))

        self.assertEqual([item.imap_uid for item in messages], [120])
        self.assertEqual(highest_seen_uid, 200)
        self.assertEqual(fake_client.header_fetch_uids, all_uids)
        self.assertEqual(fake_client.full_fetch_uids, [120])

    def test_imap_fetch_since_supports_server_side_since_date(self) -> None:
        fake_client = _FakeIMAPClient()
        fake_client.search_results = {
            ("SINCE", "02-Apr-2026"): b"50 51 52",
        }
        fake_client.header_payloads = {
            50: b"From: Other <other@example.com>\r\n\r\n",
            51: b"From: User <user@example.com>\r\n\r\n",
            52: b"From: User <user@example.com>\r\n\r\n",
        }
        fake_client.fetch_payloads = {51: b"message-51", 52: b"message-52"}
        endpoint = MailEndpointConfig(
            host="imap.qq.com",
            port=993,
            ssl=True,
            username="keqing@qq.com",
            password="secret",
            folder="INBOX",
        )
        with patch("dropline.mail.imaplib.IMAP4_SSL", return_value=fake_client):
            messages, highest_seen_uid = IMAPInboundClient(endpoint).fetch_since(
                0,
                0,
                ("user@example.com",),
                date(2026, 4, 2),
            )

        self.assertEqual([item.imap_uid for item in messages], [51, 52])
        self.assertEqual(highest_seen_uid, 52)

    def test_imap_fetch_since_retries_transient_connect_failure(self) -> None:
        fake_client = _FakeIMAPClient()
        fake_client.search_results = {
            ("ALL",): b"1",
        }
        fake_client.header_payloads = {1: b"From: User <user@example.com>\r\n\r\n"}
        fake_client.fetch_payloads = {1: b"message-1"}
        endpoint = MailEndpointConfig(
            host="imap.qq.com",
            port=993,
            ssl=True,
            username="keqing@qq.com",
            password="secret",
            folder="INBOX",
        )
        with patch(
            "dropline.mail.imaplib.IMAP4_SSL",
            side_effect=[OSError("nodename nor servname provided, or not known"), fake_client],
        ), patch("dropline.utils.time.sleep", return_value=None):
            messages, highest_seen_uid = IMAPInboundClient(endpoint).fetch_since(0, 0, ("user@example.com",))

        self.assertEqual([item.imap_uid for item in messages], [1])
        self.assertEqual(highest_seen_uid, 1)

    def test_smtp_send_retries_transient_disconnect(self) -> None:
        endpoint = _smtp_endpoint(
            host="smtp.qq.com",
            port=465,
            security="ssl",
            username="keqing@qq.com",
            password="secret",
        )
        mailbox = CharacterMailboxConfig(
            address="keqing@qq.com",
            imap=MailEndpointConfig(
                host="imap.qq.com",
                port=993,
                ssl=True,
                username="keqing@qq.com",
                password="secret",
                folder="INBOX",
            ),
            smtp=endpoint,
        )
        message = EmailMessage()
        message["From"] = "keqing@qq.com"
        message["To"] = "user@example.com"
        message["Subject"] = "test"
        message["Message-ID"] = "<fixed@example.com>"
        message.set_content("hello")
        fake_client = _FakeSMTPClient()

        with patch(
            "dropline.mail.smtplib.SMTP_SSL",
            side_effect=[smtplib.SMTPServerDisconnected("server disconnected"), fake_client],
        ), patch("dropline.utils.time.sleep", return_value=None):
            message_id = SMTPOutboundClient(mailbox).send(message)

        self.assertEqual(message_id, "<fixed@example.com>")
        self.assertEqual(fake_client.sent_message_ids, ["<fixed@example.com>"])

    def test_smtp_security_ssl_uses_smtp_ssl_without_starttls(self) -> None:
        fake_client = _FakeSMTPClient()
        mailbox = _mailbox_with_smtp_security("ssl")
        message = _outbound_message()

        with patch("dropline.mail.smtplib.SMTP_SSL", return_value=fake_client) as smtp_ssl, patch(
            "dropline.mail.smtplib.SMTP",
        ) as smtp:
            SMTPOutboundClient(mailbox).send(message)

        smtp_ssl.assert_called_once_with("smtp.example.com", 465)
        smtp.assert_not_called()
        self.assertEqual(fake_client.starttls_calls, 0)
        self.assertEqual(fake_client.sent_message_ids, ["<fixed@example.com>"])

    def test_smtp_security_starttls_uses_plain_smtp_then_starttls(self) -> None:
        fake_client = _FakeSMTPClient()
        mailbox = _mailbox_with_smtp_security("starttls", port=587)
        message = _outbound_message()

        with patch("dropline.mail.smtplib.SMTP_SSL") as smtp_ssl, patch(
            "dropline.mail.smtplib.SMTP",
            return_value=fake_client,
        ) as smtp:
            SMTPOutboundClient(mailbox).send(message)

        smtp.assert_called_once_with("smtp.example.com", 587)
        smtp_ssl.assert_not_called()
        self.assertEqual(fake_client.starttls_calls, 1)
        self.assertEqual(fake_client.sent_message_ids, ["<fixed@example.com>"])

    def test_smtp_security_none_uses_plain_smtp_without_starttls(self) -> None:
        fake_client = _FakeSMTPClient()
        mailbox = _mailbox_with_smtp_security("none", port=25)
        message = _outbound_message()

        with patch("dropline.mail.smtplib.SMTP_SSL") as smtp_ssl, patch(
            "dropline.mail.smtplib.SMTP",
            return_value=fake_client,
        ) as smtp:
            SMTPOutboundClient(mailbox).send(message)

        smtp.assert_called_once_with("smtp.example.com", 25)
        smtp_ssl.assert_not_called()
        self.assertEqual(fake_client.starttls_calls, 0)
        self.assertEqual(fake_client.sent_message_ids, ["<fixed@example.com>"])


class _FakeIMAPClient:
    def __init__(self) -> None:
        self.search_results: dict[tuple[str, ...], bytes] = {}
        self.fetch_payloads: dict[int, bytes] = {}
        self.raw_fetch_data_by_uid: dict[int, list[object]] = {}
        self.header_payloads: dict[int, bytes] = {}
        self.full_fetch_uids: list[int] = []
        self.full_fetch_sections: list[str] = []
        self.header_fetch_uids: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        return None

    def login(self, username: str, password: str) -> None:
        return None

    def select(self, folder: str) -> None:
        return None

    def uid(self, command: str, *args: str):
        if command == "search":
            criteria = tuple(str(item) for item in args[1:])
            return "OK", [self.search_results.get(criteria, b"")]
        if command == "fetch":
            uid = int(args[0])
            section = args[1]
            if section in {"(RFC822)", "BODY.PEEK[]"}:
                self.full_fetch_uids.append(uid)
                self.full_fetch_sections.append(section)
                if uid in self.raw_fetch_data_by_uid:
                    return "OK", self.raw_fetch_data_by_uid[uid]
                return "OK", [(b"RFC822", self.fetch_payloads[uid])]
            if section == "(BODY.PEEK[HEADER.FIELDS (FROM)])":
                self.header_fetch_uids.append(uid)
                return "OK", [(b"BODY[HEADER.FIELDS (FROM)]", self.header_payloads.get(uid, b""))]
        raise AssertionError(f"Unexpected IMAP command: {command}")


class _FakeSMTPClient:
    def __init__(self) -> None:
        self.sent_message_ids: list[str] = []
        self.starttls_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        return None

    def login(self, username: str, password: str) -> None:
        return None

    def starttls(self) -> None:
        self.starttls_calls += 1

    def send_message(self, message: EmailMessage) -> None:
        self.sent_message_ids.append(message["Message-ID"])


def _smtp_endpoint(*, host: str, port: int, security: str, username: str, password: str):
    return config_module.SMTPEndpointConfig(
        host=host,
        port=port,
        security=security,
        username=username,
        password=password,
    )


def _mailbox_with_smtp_security(security: str, *, port: int = 465) -> CharacterMailboxConfig:
    return CharacterMailboxConfig(
        address="character@example.com",
        imap=MailEndpointConfig(
            host="imap.example.com",
            port=993,
            ssl=True,
            username="character@example.com",
            password="secret",
            folder="INBOX",
        ),
        smtp=_smtp_endpoint(
            host="smtp.example.com",
            port=port,
            security=security,
            username="character@example.com",
            password="secret",
        ),
    )


def _outbound_message() -> EmailMessage:
    message = EmailMessage()
    message["From"] = "character@example.com"
    message["To"] = "user@example.com"
    message["Subject"] = "test"
    message["Message-ID"] = "<fixed@example.com>"
    message.set_content("hello")
    return message


if __name__ == "__main__":
    unittest.main()
