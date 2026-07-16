from __future__ import annotations

import smtplib
import unittest

from dropline.utils import is_transient_network_error


class IsTransientNetworkErrorTests(unittest.TestCase):
    def test_smtp_authentication_error_is_not_transient(self) -> None:
        exc = smtplib.SMTPAuthenticationError(535, b"Authentication failed")
        self.assertFalse(is_transient_network_error(exc))

    def test_smtp_recipients_refused_is_not_transient(self) -> None:
        exc = smtplib.SMTPRecipientsRefused({"user@example.com": (550, b"Mailbox unavailable")})
        self.assertFalse(is_transient_network_error(exc))

    def test_smtp_server_disconnected_is_transient(self) -> None:
        exc = smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
        self.assertTrue(is_transient_network_error(exc))

    def test_smtp_connect_error_is_transient(self) -> None:
        exc = smtplib.SMTPConnectError(421, b"Service not available")
        self.assertTrue(is_transient_network_error(exc))

    def test_connection_error_is_transient(self) -> None:
        exc = ConnectionError("connection reset by peer")
        self.assertTrue(is_transient_network_error(exc))

    def test_plain_os_error_is_transient(self) -> None:
        exc = OSError("some low level failure")
        self.assertTrue(is_transient_network_error(exc))


if __name__ == "__main__":
    unittest.main()
