from __future__ import annotations

from datetime import date
import email
from email import policy
from email.message import EmailMessage
from pathlib import Path
import smtplib
import ssl
import tempfile
import unittest
from unittest.mock import patch

import dropline.mail as mail_module
import dropline.config as config_module
from dropline.config import CharacterMailboxConfig, MailEndpointConfig
from dropline.mail import IMAPInboundClient, SMTPOutboundClient, normalize_inbound_message
from dropline.models import AttachmentMeta, RawInboundMessage, Stage2Result
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


RAW_EMAIL_UNKNOWN_CHARSET = """From: User <user@example.com>
To: keqing@example.com
Subject: weird charset
Message-ID: <weird-charset@example.com>
Content-Type: multipart/alternative; boundary="BOUNDARY"

--BOUNDARY
Content-Type: text/plain; charset="x-user-defined"

hello world
--BOUNDARY--
""".encode("utf-8")


QQ_MAIL_REPLY = """From: =?utf-8?b?55So5oi3?= <user@example.com>
To: keqing@qq.com
Subject: =?utf-8?b?5Zue5aSN77ya5LuK5aSc5YWI57yT5LiA57yT?=
Date: Wed, 01 Apr 2026 09:15:00 +0800
Message-ID: <qq-reply@example.com>
In-Reply-To: <out-1@example.com>
Content-Type: multipart/alternative; boundary="QQBOUNDARY"

--QQBOUNDARY
Content-Type: text/plain; charset="utf-8"
Content-Transfer-Encoding: 8bit

谢谢你昨晚的信，我今天好多了。
下周要考试，这几天在复习。




------------------ 原始邮件 ------------------
发件人: "刻晴" <keqing@qq.com>;
发送时间: 2026年3月31日(星期二) 晚上9:42
收件人: "用户"<user@example.com>;
主题: 今夜先缓一缓

见字如晤。今晚的风并不急，我把你写来的那几句话在灯下又看了一遍。

--QQBOUNDARY
Content-Type: text/html; charset="utf-8"
Content-Transfer-Encoding: 8bit

<div>谢谢你昨晚的信，我今天好多了。</div><div>下周要考试，这几天在复习。</div><div><br></div><div style="font-size: 12px;font-family: Arial Narrow;padding:2px 0 2px 0;">------------------&nbsp;原始邮件&nbsp;------------------</div><div style="font-size: 12px;background:#efefef;padding:8px;"><div><b>发件人:</b>&nbsp;"刻晴"&lt;keqing@qq.com&gt;;</div><div><b>发送时间:</b>&nbsp;2026年3月31日(星期二) 晚上9:42</div><div><b>收件人:</b>&nbsp;"用户"&lt;user@example.com&gt;;</div><div><b>主题:</b>&nbsp;今夜先缓一缓</div></div><div>见字如晤。今晚的风并不急，我把你写来的那几句话在灯下又看了一遍。</div>
--QQBOUNDARY--
""".encode()


NETEASE_163_REPLY = """From: =?utf-8?b?55So5oi3?= <user@163.com>
To: keqing@163.com
Subject: =?utf-8?b?5Zue5aSN77ya5LuK5aSc5YWI57yT5LiA57yT?=
Date: Wed, 01 Apr 2026 09:20:00 +0800
Message-ID: <163-reply@example.com>
Content-Type: multipart/alternative; boundary="NETEASE"

--NETEASE
Content-Type: text/plain; charset="utf-8"
Content-Transfer-Encoding: 8bit

收到了。昨天确实太累，今天请了半天假。



在 2026-03-31 21:42:10，"刻晴" <keqing@163.com> 写道：
> 见字如晤。今晚的风并不急，我把你写来的那几句话在灯下又看了一遍。
> 你提到的疲惫并不是一句轻巧的安慰就能化开的东西。
--NETEASE
Content-Type: text/html; charset="utf-8"
Content-Transfer-Encoding: 8bit

<div style="line-height:1.7;color:#000000;font-size:14px;font-family:Arial"><div>收到了。昨天确实太累，今天请了半天假。</div><br><div style="zoom:1"></div><div id="divNeteaseMailCard"></div><br><pre><br>在 2026-03-31 21:42:10，"刻晴" &lt;keqing@163.com&gt; 写道：<br></pre><blockquote id="isReplyContent" style="PADDING-LEFT: 1ex; MARGIN: 0px 0px 0px 0.8ex; BORDER-LEFT: #ccc 1px solid"><div>见字如晤。今晚的风并不急，我把你写来的那几句话在灯下又看了一遍。</div><div>你提到的疲惫并不是一句轻巧的安慰就能化开的东西。</div></blockquote></div>
--NETEASE--
""".encode()


GMAIL_ZH_REPLY = """From: =?utf-8?b?55So5oi3?= <user@gmail.com>
To: keqing@gmail.com
Subject: Re: =?utf-8?b?5LuK5aSc5YWI57yT5LiA57yT?=
Date: Wed, 01 Apr 2026 09:25:00 +0800
Message-ID: <gmail-reply@example.com>
Content-Type: multipart/alternative; boundary="GMAILBOUNDARY"

--GMAILBOUNDARY
Content-Type: text/plain; charset="UTF-8"
Content-Transfer-Encoding: 8bit

好，我记下了。这周末去看看那家店。

刻晴 <keqing@gmail.com> 于2026年3月31日周二 21:42写道：

> 见字如晤。今晚的风并不急，我把你写来的那几句话在灯下又看了一遍。
>
> 你提到的疲惫并不是一句轻巧的安慰就能化开的东西。
>

--GMAILBOUNDARY
Content-Type: text/html; charset="UTF-8"
Content-Transfer-Encoding: 8bit

<div dir="ltr">好，我记下了。这周末去看看那家店。</div><br><div class="gmail_quote gmail_quote_container"><div dir="ltr" class="gmail_attr">刻晴 &lt;<a href="mailto:keqing@gmail.com">keqing@gmail.com</a>&gt; 于2026年3月31日周二 21:42写道：<br></div><blockquote class="gmail_quote" style="margin:0px 0px 0px 0.8ex;border-left:1px solid rgb(204,204,204);padding-left:1ex"><div dir="ltr">见字如晤。今晚的风并不急，我把你写来的那几句话在灯下又看了一遍。</div><div dir="ltr">你提到的疲惫并不是一句轻巧的安慰就能化开的东西。</div></blockquote></div>
--GMAILBOUNDARY--
""".encode()


OUTLOOK_ZH_REPLY = """From: =?utf-8?b?55So5oi3?= <user@outlook.com>
To: keqing@example.com
Subject: =?utf-8?b?5Zue5aSN77ya5LuK5aSc5YWI57yT5LiA57yT?=
Date: Wed, 01 Apr 2026 09:30:00 +0800
Message-ID: <outlook-reply@example.com>
Content-Type: multipart/alternative; boundary="OUTLOOKBOUNDARY"

--OUTLOOKBOUNDARY
Content-Type: text/plain; charset="utf-8"
Content-Transfer-Encoding: 8bit

知道了，谢谢。明天再详细写给你。

________________________________
发件人: 刻晴 <keqing@example.com>
发送时间: 2026年3月31日 21:42
收件人: user@outlook.com
主题: 今夜先缓一缓

见字如晤。今晚的风并不急，我把你写来的那几句话在灯下又看了一遍。
--OUTLOOKBOUNDARY
Content-Type: text/html; charset="utf-8"
Content-Transfer-Encoding: 8bit

<html><body dir="ltr"><div dir="ltr">知道了，谢谢。明天再详细写给你。</div><div id="appendonsend"></div><hr style="display:inline-block;width:98%" tabindex="-1"><div id="divRplyFwdMsg" dir="ltr"><font face="Calibri, sans-serif" style="font-size:11pt" color="#000000"><b>发件人:</b> 刻晴 &lt;keqing@example.com&gt;<br><b>发送时间:</b> 2026年3月31日 21:42<br><b>收件人:</b> user@outlook.com<br><b>主题:</b> 今夜先缓一缓</font><div>&nbsp;</div></div><div dir="ltr">见字如晤。今晚的风并不急，我把你写来的那几句话在灯下又看了一遍。</div></body></html>
--OUTLOOKBOUNDARY--
""".encode()


OUTLOOK_DESKTOP_ZH_REPLY = """From: User <user@example.com>
To: keqing@example.com
Subject: =?utf-8?b?562U5aSN77ya5LuK5aSc5YWI57yT5LiA57yT?=
Date: Wed, 01 Apr 2026 09:35:00 +0800
Message-ID: <outlook-desktop-reply@example.com>
Content-Type: text/plain; charset="utf-8"
Content-Transfer-Encoding: 8bit

先回你一句：我没事，只是最近睡得少。

-----原始邮件-----
发件人: 刻晴 <keqing@example.com>
发送时间: 2026年3月31日 21:42
收件人: user@example.com
主题: 今夜先缓一缓

见字如晤。今晚的风并不急。
""".encode()


APPLE_MAIL_ZH_REPLY = """From: User <user@icloud.com>
To: keqing@example.com
Subject: Re: =?utf-8?b?5LuK5aSc5YWI57yT5LiA57yT?=
Date: Wed, 01 Apr 2026 09:40:00 +0800
Message-ID: <apple-reply@example.com>
Content-Type: text/plain; charset="utf-8"
Content-Transfer-Encoding: 8bit

嗯，在看了。这几天先把事情一件件做完。

> 在 2026年3月31日，21:42，刻晴 <keqing@example.com> 写道：
>
> 见字如晤。今晚的风并不急。
""".encode()


def _html_only_variant(raw_email: bytes) -> bytes:
    """Rebuild a multipart/alternative sample as an HTML-only message to exercise the HTML path."""
    parsed = email.message_from_bytes(raw_email, policy=policy.default)
    html_part = next(part for part in parsed.walk() if part.get_content_type() == "text/html")
    message = EmailMessage()
    for name in ("From", "To", "Subject", "Date", "Message-ID"):
        if parsed[name] is not None:
            message[name] = parsed[name]
    message.set_content(html_part.get_content(), subtype="html")
    return message.as_bytes()


def _message_with_inline_and_regular_attachments() -> bytes:
    message = EmailMessage()
    message["From"] = "User <user@example.com>"
    message["To"] = "keqing@example.com"
    message["Subject"] = "带图的信"
    message["Message-ID"] = "<with-attachments@example.com>"
    message.set_content("这是正文，图在下面。")
    message.add_alternative('<p>这是正文，图在下面。</p><img src="cid:photo1"><img src="cid:photo2">', subtype="html")
    html_part = message.get_payload()[1]
    html_part.add_related(b"\x89PNG-inline-bytes", maintype="image", subtype="png", cid="<photo1>", filename="photo.png", disposition="inline")
    html_part.add_related(b"\xff\xd8-jpeg-cid-only", maintype="image", subtype="jpeg", cid="<photo2>")
    message.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="notes.pdf")
    message.add_attachment(b"GIF89a", maintype="image", subtype="gif", filename="sticker.gif")
    return message.as_bytes()


class MailQuoteStrippingTests(unittest.TestCase):
    def test_qq_mail_reply_drops_original_message_block_in_plain_and_html_paths(self) -> None:
        normalized = normalize_inbound_message(RawInboundMessage(imap_uid=11, raw_bytes=QQ_MAIL_REPLY))

        self.assertEqual(normalized.subject, "回复：今夜先缓一缓")
        self.assertEqual(normalized.body_text, "谢谢你昨晚的信，我今天好多了。\n下周要考试，这几天在复习。")
        self.assertEqual(normalized.thread_message_id, "<out-1@example.com>")

        html_only = normalize_inbound_message(RawInboundMessage(imap_uid=12, raw_bytes=_html_only_variant(QQ_MAIL_REPLY)))

        self.assertTrue(html_only.body_html_present)
        self.assertTrue(html_only.body_text.startswith("谢谢你昨晚的信，我今天好多了。"))
        self.assertIn("下周要考试，这几天在复习。", html_only.body_text)
        self.assertNotIn("原始邮件", html_only.body_text)
        self.assertNotIn("发件人", html_only.body_text)
        self.assertNotIn("见字如晤", html_only.body_text)

    def test_163_mail_reply_drops_zai_xiedao_header_and_blockquote(self) -> None:
        normalized = normalize_inbound_message(RawInboundMessage(imap_uid=13, raw_bytes=NETEASE_163_REPLY))

        self.assertEqual(normalized.body_text, "收到了。昨天确实太累，今天请了半天假。")

        html_only = normalize_inbound_message(RawInboundMessage(imap_uid=14, raw_bytes=_html_only_variant(NETEASE_163_REPLY)))

        self.assertEqual(html_only.body_text, "收到了。昨天确实太累，今天请了半天假。")

    def test_chinese_gmail_reply_drops_yu_xiedao_header_and_gmail_quote(self) -> None:
        normalized = normalize_inbound_message(RawInboundMessage(imap_uid=15, raw_bytes=GMAIL_ZH_REPLY))

        self.assertEqual(normalized.body_text, "好，我记下了。这周末去看看那家店。")

        html_only = normalize_inbound_message(RawInboundMessage(imap_uid=16, raw_bytes=_html_only_variant(GMAIL_ZH_REPLY)))

        self.assertEqual(html_only.body_text, "好，我记下了。这周末去看看那家店。")

    def test_chinese_outlook_reply_drops_separator_and_fajianren_header(self) -> None:
        normalized = normalize_inbound_message(RawInboundMessage(imap_uid=17, raw_bytes=OUTLOOK_ZH_REPLY))

        self.assertEqual(normalized.body_text, "知道了，谢谢。明天再详细写给你。")

        html_only = normalize_inbound_message(RawInboundMessage(imap_uid=18, raw_bytes=_html_only_variant(OUTLOOK_ZH_REPLY)))

        self.assertEqual(html_only.body_text, "知道了，谢谢。明天再详细写给你。")

        desktop = normalize_inbound_message(RawInboundMessage(imap_uid=19, raw_bytes=OUTLOOK_DESKTOP_ZH_REPLY))

        self.assertEqual(desktop.body_text, "先回你一句：我没事，只是最近睡得少。")

    def test_apple_mail_chinese_reply_is_stripped(self) -> None:
        normalized = normalize_inbound_message(RawInboundMessage(imap_uid=20, raw_bytes=APPLE_MAIL_ZH_REPLY))

        self.assertEqual(normalized.body_text, "嗯，在看了。这几天先把事情一件件做完。")

    def test_user_prose_mentioning_xiedao_is_not_treated_as_quote_header(self) -> None:
        raw_email = """From: User <user@example.com>
To: keqing@example.com
Subject: book
Message-ID: <prose@example.com>
Content-Type: text/plain; charset="utf-8"
Content-Transfer-Encoding: 8bit

在那本书的最后一页，作者写道：
人要先学会休息。
我觉得这句话说得很对。
""".encode()

        normalized = normalize_inbound_message(RawInboundMessage(imap_uid=21, raw_bytes=raw_email))

        self.assertEqual(normalized.body_text, "在那本书的最后一页，作者写道：\n人要先学会休息。\n我觉得这句话说得很对。")

    def test_html_quote_containers_are_removed_as_whole_elements(self) -> None:
        html_body = (
            '<div class="content">正文第一段。</div>'
            '<div data-id="quote-looking">正文第二段。</div>'
            '<blockquote type="cite"><p>外层引用</p><blockquote><p>内层引用</p></blockquote><p>外层引用尾部</p></blockquote>'
            '<div id="x-quote-box"><p>另一个客户端的引用</p></div>'
            "<p>结尾一句。</p>"
        )

        text = mail_module._html_to_text(html_body)

        self.assertIn("正文第一段。", text)
        self.assertIn("正文第二段。", text)
        self.assertIn("结尾一句。", text)
        self.assertNotIn("外层引用", text)
        self.assertNotIn("内层引用", text)
        self.assertNotIn("另一个客户端的引用", text)

    def test_unterminated_html_quote_container_swallows_the_rest_of_the_document(self) -> None:
        text = mail_module._html_to_text('<p>我的话。</p><div class="gmail_quote"><p>引用开始')

        self.assertEqual(text.strip(), "我的话。")


class MailAttachmentMetadataTests(unittest.TestCase):
    def test_inline_images_and_regular_attachments_are_recorded_as_metadata_only(self) -> None:
        normalized = normalize_inbound_message(
            RawInboundMessage(imap_uid=30, raw_bytes=_message_with_inline_and_regular_attachments())
        )

        self.assertEqual(normalized.body_text, "这是正文，图在下面。")
        self.assertTrue(normalized.body_html_present)
        self.assertEqual(
            normalized.attachments,
            [
                AttachmentMeta(filename="photo.png", content_type="image/png", size=len(b"\x89PNG-inline-bytes"), inline=True),
                AttachmentMeta(filename="", content_type="image/jpeg", size=len(b"\xff\xd8-jpeg-cid-only"), inline=True),
                AttachmentMeta(filename="notes.pdf", content_type="application/pdf", size=len(b"%PDF-1.4"), inline=False),
                AttachmentMeta(filename="sticker.gif", content_type="image/gif", size=len(b"GIF89a"), inline=False),
            ],
        )
        payload = normalized.to_dict()
        self.assertEqual(payload["attachments"][0]["inline"], True)
        self.assertNotIn("raw_bytes", payload)

    def test_inline_text_parts_are_not_counted_as_attachments(self) -> None:
        raw_email = """From: User <user@example.com>
To: keqing@example.com
Subject: inline text
Message-ID: <inline-text@example.com>
Content-Type: multipart/mixed; boundary="MIXED"

--MIXED
Content-Type: text/plain; charset="utf-8"
Content-Disposition: inline
Content-Transfer-Encoding: 8bit

正文在这里。
--MIXED
Content-Type: text/calendar; charset="utf-8"
Content-Disposition: inline; filename="invite.ics"

BEGIN:VCALENDAR
END:VCALENDAR
--MIXED
Content-Type: application/pdf
Content-Disposition: inline; filename="report.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjQ=
--MIXED--
""".encode()

        normalized = normalize_inbound_message(RawInboundMessage(imap_uid=31, raw_bytes=raw_email))

        self.assertEqual(normalized.body_text, "正文在这里。")
        self.assertEqual(
            normalized.attachments,
            [AttachmentMeta(filename="report.pdf", content_type="application/pdf", size=len(b"%PDF-1.4"), inline=True)],
        )

    def test_attachment_meta_from_dict_tolerates_legacy_payloads(self) -> None:
        self.assertEqual(
            AttachmentMeta.from_dict({"filename": "a.txt", "content_type": "text/plain", "size": 3}),
            AttachmentMeta(filename="a.txt", content_type="text/plain", size=3, inline=False),
        )
        self.assertEqual(
            AttachmentMeta.from_dict({"size": "oops"}),
            AttachmentMeta(filename="", content_type="", size=0, inline=False),
        )


class KnownParagraphStrippingTests(unittest.TestCase):
    CHARACTER_LETTER = (
        "见字如晤。今晚的风并不急，我把你写来的那几句话在灯下又看了一遍。\n\n"
        "若今日的事情已经压得你心里发闷，就先拆成最小的一步去做。\n\n"
        "晚安。"
    )

    def test_paragraph_keys_ignore_whitespace_and_short_lines(self) -> None:
        keys = mail_module.paragraph_keys(self.CHARACTER_LETTER)

        self.assertEqual(
            keys,
            {
                "见字如晤。今晚的风并不急，我把你写来的那几句话在灯下又看了一遍。",
                "若今日的事情已经压得你心里发闷，就先拆成最小的一步去做。",
            },
        )
        self.assertEqual(mail_module.paragraph_keys(""), set())
        self.assertEqual(mail_module.paragraph_keys("晚安。\n\n好梦。"), set())

    def test_strip_known_paragraphs_removes_only_verbatim_character_paragraphs(self) -> None:
        keys = mail_module.paragraph_keys(self.CHARACTER_LETTER)
        inbound_body = (
            "我看完了，谢谢你。\n\n"
            "见字如晤。今晚的风并不急，\n我把你写来的那几句话在灯下又看了一遍。\n\n"
            "晚安。\n\n"
            "若今日的事情已经压得你心里发闷，就先拆成最小的一步去做。\n\n"
            "下周再写。"
        )

        stripped = mail_module.strip_known_paragraphs(inbound_body, keys)

        self.assertEqual(stripped, "我看完了，谢谢你。\n\n晚安。\n\n下周再写。")

    def test_strip_known_paragraphs_returns_body_unchanged_when_nothing_matches(self) -> None:
        keys = mail_module.paragraph_keys(self.CHARACTER_LETTER)
        inbound_body = "第一段  保留原样。\n\n\n第二段也保留。\n"

        self.assertIs(mail_module.strip_known_paragraphs(inbound_body, keys), inbound_body)
        self.assertIs(mail_module.strip_known_paragraphs(inbound_body, set()), inbound_body)
        self.assertEqual(mail_module.strip_known_paragraphs("", keys), "")

    def test_extract_plain_body_reads_archived_outbound_eml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            create_test_repo(root)
            settings = load_test_settings(root)
            message = mail_module.compose_email(
                settings,
                subject="今夜先缓一缓",
                body_text=self.CHARACTER_LETTER,
                image_path=None,
                reply_to_message_id=None,
                mode="initiative",
            )

        self.assertEqual(mail_module.extract_plain_body(message.as_bytes()), self.CHARACTER_LETTER)


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

    def test_normalize_inbound_message_handles_unrecognized_charset_without_raising(self) -> None:
        normalized = normalize_inbound_message(
            RawInboundMessage(imap_uid=3, raw_bytes=RAW_EMAIL_UNKNOWN_CHARSET)
        )

        self.assertEqual(normalized.message_id, "<weird-charset@example.com>")
        self.assertIn("hello world", normalized.body_text)

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
            messages, highest_seen_uid, uidvalidity = IMAPInboundClient(endpoint).fetch_since(
                0,
                2,
                ("keqing@qq.com",),
            )

        self.assertEqual([item.imap_uid for item in messages], [1, 2])
        self.assertEqual(highest_seen_uid, 2)
        self.assertEqual(uidvalidity, 1)
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
            messages, highest_seen_uid, _ = IMAPInboundClient(endpoint).fetch_since(0, 0, ("keqing@qq.com",))

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
            messages, highest_seen_uid, _ = IMAPInboundClient(endpoint).fetch_since(20, 0, ("user@example.com",))

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
            messages, highest_seen_uid, _ = IMAPInboundClient(endpoint).fetch_since(100, 0, ("user@example.com",))

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
            messages, highest_seen_uid, _ = IMAPInboundClient(endpoint).fetch_since(
                0,
                0,
                ("user@example.com",),
                date(2026, 4, 2),
            )

        self.assertEqual([item.imap_uid for item in messages], [51, 52])
        self.assertEqual(highest_seen_uid, 52)

    def test_imap_fetch_since_reports_uidvalidity_from_select_response(self) -> None:
        fake_client = _FakeIMAPClient(uidvalidity=4711)
        fake_client.search_results = {("ALL",): b"1"}
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
        with patch("dropline.mail.imaplib.IMAP4_SSL", return_value=fake_client):
            messages, highest_seen_uid, uidvalidity = IMAPInboundClient(endpoint).fetch_since(0, 0, ("user@example.com",))

        self.assertEqual([item.imap_uid for item in messages], [1])
        self.assertEqual(highest_seen_uid, 1)
        self.assertEqual(uidvalidity, 4711)
        self.assertEqual(fake_client.selected_folders, ["INBOX"])
        self.assertEqual(fake_client.response_codes, ["UIDVALIDITY"])

    def test_imap_fetch_since_reports_zero_uidvalidity_when_server_omits_it(self) -> None:
        fake_client = _FakeIMAPClient(uidvalidity=None)
        fake_client.search_results = {("ALL",): b""}
        endpoint = MailEndpointConfig(
            host="imap.qq.com",
            port=993,
            ssl=True,
            username="keqing@qq.com",
            password="secret",
            folder="INBOX",
        )
        with patch("dropline.mail.imaplib.IMAP4_SSL", return_value=fake_client), self.assertLogs(
            "dropline.mail", level="WARNING"
        ) as logs:
            messages, highest_seen_uid, uidvalidity = IMAPInboundClient(endpoint).fetch_since(0, 0, ())

        self.assertEqual(messages, [])
        self.assertEqual(highest_seen_uid, 0)
        self.assertEqual(uidvalidity, 0)
        self.assertTrue(any("did not report UIDVALIDITY" in line for line in logs.output))

    def test_imap_fetch_since_does_not_refetch_highest_uid_already_seen(self) -> None:
        # RFC 3501: "UID 21:*" still matches the highest UID (20) when nothing newer exists.
        fake_client = _FakeIMAPClient()
        fake_client.search_results = {("UID", "21:*"): b"20"}
        fake_client.header_payloads = {20: b"From: User <user@example.com>\r\n\r\n"}
        fake_client.fetch_payloads = {20: b"message-20"}
        endpoint = MailEndpointConfig(
            host="imap.qq.com",
            port=993,
            ssl=True,
            username="keqing@qq.com",
            password="secret",
            folder="INBOX",
        )
        with patch("dropline.mail.imaplib.IMAP4_SSL", return_value=fake_client):
            messages, highest_seen_uid, _ = IMAPInboundClient(endpoint).fetch_since(20, 0, ("user@example.com",))

        self.assertEqual(messages, [])
        self.assertEqual(highest_seen_uid, 0)
        self.assertEqual(fake_client.header_fetch_uids, [])
        self.assertEqual(fake_client.full_fetch_uids, [])

    def test_imap_fetch_since_keeps_only_uids_above_cursor(self) -> None:
        fake_client = _FakeIMAPClient()
        fake_client.search_results = {("UID", "21:*"): b"20 21 22"}
        fake_client.header_payloads = {
            20: b"From: User <user@example.com>\r\n\r\n",
            21: b"From: User <user@example.com>\r\n\r\n",
            22: b"From: User <user@example.com>\r\n\r\n",
        }
        fake_client.fetch_payloads = {21: b"message-21", 22: b"message-22"}
        endpoint = MailEndpointConfig(
            host="imap.qq.com",
            port=993,
            ssl=True,
            username="keqing@qq.com",
            password="secret",
            folder="INBOX",
        )
        with patch("dropline.mail.imaplib.IMAP4_SSL", return_value=fake_client):
            messages, highest_seen_uid, _ = IMAPInboundClient(endpoint).fetch_since(20, 0, ("user@example.com",))

        self.assertEqual([item.imap_uid for item in messages], [21, 22])
        self.assertEqual(highest_seen_uid, 22)
        self.assertEqual(fake_client.header_fetch_uids, [21, 22])
        self.assertEqual(fake_client.full_fetch_uids, [21, 22])

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
            messages, highest_seen_uid, _ = IMAPInboundClient(endpoint).fetch_since(0, 0, ("user@example.com",))

        self.assertEqual([item.imap_uid for item in messages], [1])
        self.assertEqual(highest_seen_uid, 1)

    def test_imap_ssl_connection_verifies_certificates_and_sets_timeout(self) -> None:
        fake_client = _FakeIMAPClient()
        fake_client.search_results = {("ALL",): b""}
        endpoint = MailEndpointConfig(
            host="imap.qq.com",
            port=993,
            ssl=True,
            username="keqing@qq.com",
            password="secret",
            folder="INBOX",
        )
        with patch("dropline.mail.imaplib.IMAP4_SSL", autospec=True, return_value=fake_client) as imap_ssl, patch(
            "dropline.mail.imaplib.IMAP4",
            autospec=True,
        ) as imap_plain, self.assertNoLogs("dropline.mail", level="WARNING"):
            messages, highest_seen_uid, uidvalidity = IMAPInboundClient(endpoint).fetch_since(0, 0, ())

        self.assertEqual(messages, [])
        self.assertEqual(highest_seen_uid, 0)
        self.assertEqual(uidvalidity, 1)
        imap_ssl.assert_called_once()
        self.assertEqual(imap_ssl.call_args.args, ("imap.qq.com", 993))
        self.assertEqual(imap_ssl.call_args.kwargs["timeout"], mail_module.MAIL_SOCKET_TIMEOUT_SECONDS)
        _assert_verified_tls_context(self, imap_ssl.call_args.kwargs["ssl_context"])
        imap_plain.assert_not_called()

    def test_imap_plain_connection_sets_timeout(self) -> None:
        fake_client = _FakeIMAPClient()
        fake_client.search_results = {("ALL",): b""}
        endpoint = MailEndpointConfig(
            host="imap.example.com",
            port=143,
            ssl=False,
            username="keqing@example.com",
            password="secret",
            folder="INBOX",
        )
        with patch("dropline.mail.imaplib.IMAP4", autospec=True, return_value=fake_client) as imap_plain, patch(
            "dropline.mail.imaplib.IMAP4_SSL",
            autospec=True,
        ) as imap_ssl, self.assertLogs("dropline.mail", level="WARNING") as logs:
            IMAPInboundClient(endpoint).fetch_since(0, 0, ())

        imap_plain.assert_called_once_with("imap.example.com", 143, timeout=mail_module.MAIL_SOCKET_TIMEOUT_SECONDS)
        imap_ssl.assert_not_called()
        self.assertTrue(any("unencrypted" in line for line in logs.output))

    def test_imap_certificate_failure_is_not_retried_and_logs_hint(self) -> None:
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
            autospec=True,
            side_effect=ssl.SSLCertVerificationError("certificate verify failed: self-signed certificate"),
        ) as imap_ssl, patch("dropline.utils.time.sleep") as sleep, self.assertLogs(
            "dropline.mail", level="ERROR"
        ) as logs, self.assertRaises(ssl.SSLCertVerificationError):
            IMAPInboundClient(endpoint).fetch_since(0, 0, ())

        self.assertEqual(imap_ssl.call_count, 1)
        sleep.assert_not_called()
        self.assertTrue(any("certificate verification failed" in line for line in logs.output))

    def test_build_tls_context_falls_back_to_certifi_without_system_cas(self) -> None:
        empty_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.assertEqual(empty_context.cert_store_stats()["x509_ca"], 0)

        with patch("dropline.mail.ssl.create_default_context", return_value=empty_context):
            context = mail_module.build_tls_context()

        self.assertIs(context, empty_context)
        _assert_verified_tls_context(self, context)

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

    def test_smtp_send_succeeds_when_quit_fails_after_message_already_sent(self) -> None:
        fake_client = _FakeSMTPClient(quit_exception=smtplib.SMTPResponseException(421, b"maintenance"))
        mailbox = _mailbox_with_smtp_security("ssl")
        message = _outbound_message()

        with patch("dropline.mail.smtplib.SMTP_SSL", return_value=fake_client), patch(
            "dropline.mail.smtplib.SMTP",
        ):
            message_id = SMTPOutboundClient(mailbox).send(message)

        self.assertEqual(message_id, "<fixed@example.com>")
        self.assertEqual(fake_client.sent_message_ids, ["<fixed@example.com>"])
        self.assertEqual(fake_client.quit_calls, 1)

    def test_smtp_security_ssl_uses_smtp_ssl_without_starttls(self) -> None:
        fake_client = _FakeSMTPClient()
        mailbox = _mailbox_with_smtp_security("ssl")
        message = _outbound_message()

        with patch("dropline.mail.smtplib.SMTP_SSL", return_value=fake_client) as smtp_ssl, patch(
            "dropline.mail.smtplib.SMTP",
        ) as smtp:
            SMTPOutboundClient(mailbox).send(message)

        smtp_ssl.assert_called_once()
        self.assertEqual(smtp_ssl.call_args.args, ("smtp.example.com", 465))
        self.assertEqual(smtp_ssl.call_args.kwargs["timeout"], mail_module.MAIL_SOCKET_TIMEOUT_SECONDS)
        _assert_verified_tls_context(self, smtp_ssl.call_args.kwargs["context"])
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

        smtp.assert_called_once_with("smtp.example.com", 587, timeout=mail_module.MAIL_SOCKET_TIMEOUT_SECONDS)
        smtp_ssl.assert_not_called()
        self.assertEqual(fake_client.starttls_calls, 1)
        _assert_verified_tls_context(self, fake_client.starttls_contexts[0])
        self.assertEqual(fake_client.sent_message_ids, ["<fixed@example.com>"])

    def test_smtp_security_none_uses_plain_smtp_without_starttls(self) -> None:
        fake_client = _FakeSMTPClient()
        mailbox = _mailbox_with_smtp_security("none", port=25)
        message = _outbound_message()

        with patch("dropline.mail.smtplib.SMTP_SSL") as smtp_ssl, patch(
            "dropline.mail.smtplib.SMTP",
            return_value=fake_client,
        ) as smtp, self.assertLogs("dropline.mail", level="WARNING") as logs:
            SMTPOutboundClient(mailbox).send(message)

        self.assertTrue(any("unencrypted" in line for line in logs.output))
        smtp.assert_called_once_with("smtp.example.com", 25, timeout=mail_module.MAIL_SOCKET_TIMEOUT_SECONDS)
        smtp_ssl.assert_not_called()
        self.assertEqual(fake_client.starttls_calls, 0)
        self.assertEqual(fake_client.sent_message_ids, ["<fixed@example.com>"])


class _FakeIMAPClient:
    def __init__(self, *, uidvalidity: int | None = 1) -> None:
        self.search_results: dict[tuple[str, ...], bytes] = {}
        self.fetch_payloads: dict[int, bytes] = {}
        self.raw_fetch_data_by_uid: dict[int, list[object]] = {}
        self.header_payloads: dict[int, bytes] = {}
        self.full_fetch_uids: list[int] = []
        self.full_fetch_sections: list[str] = []
        self.header_fetch_uids: list[int] = []
        self.uidvalidity = uidvalidity
        self.selected_folders: list[str] = []
        self.response_codes: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        return None

    def login(self, username: str, password: str) -> None:
        return None

    def select(self, folder: str) -> None:
        self.selected_folders.append(folder)

    def response(self, code: str):
        # imaplib returns (code, [None]) when the server never sent that response code.
        self.response_codes.append(code)
        if code == "UIDVALIDITY" and self.uidvalidity is not None:
            return code, [str(self.uidvalidity).encode("ascii")]
        return code, [None]

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
    def __init__(self, *, quit_exception: Exception | None = None) -> None:
        self.sent_message_ids: list[str] = []
        self.starttls_calls = 0
        self.starttls_contexts: list[object] = []
        self.quit_calls = 0
        self.close_calls = 0
        self._quit_exception = quit_exception

    def login(self, username: str, password: str) -> None:
        return None

    def starttls(self, *, context: object = None) -> None:
        self.starttls_calls += 1
        self.starttls_contexts.append(context)

    def send_message(self, message: EmailMessage) -> None:
        self.sent_message_ids.append(message["Message-ID"])

    def quit(self) -> None:
        self.quit_calls += 1
        if self._quit_exception is not None:
            raise self._quit_exception

    def close(self) -> None:
        self.close_calls += 1


def _assert_verified_tls_context(test_case: unittest.TestCase, context: object) -> None:
    test_case.assertIsInstance(context, ssl.SSLContext)
    test_case.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
    test_case.assertTrue(context.check_hostname)
    test_case.assertGreater(context.cert_store_stats()["x509_ca"], 0)


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
