from __future__ import annotations

from smtplib import SMTPDataError, SMTPServerDisconnected
from unittest import TestCase
from unittest.mock import Mock, patch

from django.test.utils import override_settings

from hc.lib.emails import EmailThread, send


@patch("hc.lib.emails.time.sleep")
class EmailsTestCase(TestCase):
    def test_it_retries(self, mock_time: Mock) -> None:
        mock_msg = Mock()
        mock_msg.send = Mock(side_effect=[SMTPServerDisconnected, None])

        t = EmailThread(mock_msg)
        t.run()

        self.assertEqual(mock_msg.send.call_count, 2)

    def test_it_limits_retries(self, mock_time: Mock) -> None:
        mock_msg = Mock()
        mock_msg.send = Mock(side_effect=SMTPServerDisconnected)

        with self.assertRaises(SMTPServerDisconnected):
            t = EmailThread(mock_msg)
            t.run()

        self.assertEqual(mock_msg.send.call_count, 3)

    def test_it_retries_smtp_data_error(self, mock_time: Mock) -> None:
        mock_msg = Mock()
        mock_msg.send = Mock(side_effect=[SMTPDataError(454, "hello"), None])

        t = EmailThread(mock_msg)
        t.run()

        self.assertEqual(mock_msg.send.call_count, 2)

    @override_settings(EMAIL_HOST="")
    def test_it_requires_smtp_configuration(self, mock_time: Mock) -> None:
        with self.assertRaises(AssertionError):
            send(Mock())


class EmailConnectionLifecycleTestCase(TestCase):
    def test_it_closes_connection_after_success(self) -> None:
        mock_conn = Mock()
        mock_msg = Mock()
        with patch("hc.lib.emails.get_connection", return_value=mock_conn):
            EmailThread(mock_msg).run()

        mock_msg.send.assert_called_once()
        mock_conn.close.assert_called_once()
        # The message must not retain a reference to the closed connection.
        self.assertIsNone(mock_msg.connection)

    def test_it_closes_connection_between_retries(self) -> None:
        mock_conn = Mock()
        mock_msg = Mock()
        mock_msg.send = Mock(side_effect=[SMTPServerDisconnected, None])

        with (
            patch("hc.lib.emails.time.sleep"),
            patch("hc.lib.emails.get_connection", return_value=mock_conn),
        ):
            EmailThread(mock_msg).run()

        self.assertEqual(mock_msg.send.call_count, 2)
        # One close for each attempt (failed + successful).
        self.assertEqual(mock_conn.close.call_count, 2)
        self.assertIsNone(mock_msg.connection)

    def test_it_uses_fresh_connection_per_attempt(self) -> None:
        connections = [Mock(name=f"conn{i}") for i in range(3)]
        mock_msg = Mock()
        mock_msg.send = Mock(side_effect=SMTPServerDisconnected)

        with (
            patch("hc.lib.emails.time.sleep"),
            patch("hc.lib.emails.get_connection", side_effect=connections),
        ):
            with self.assertRaises(SMTPServerDisconnected):
                EmailThread(mock_msg).run()

        # A brand new connection per attempt, and all are closed.
        self.assertEqual(mock_msg.send.call_count, 3)
        for conn in connections:
            conn.close.assert_called_once()

    def test_it_closes_connection_on_final_failure(self) -> None:
        mock_conn = Mock()
        mock_msg = Mock()
        mock_msg.send = Mock(side_effect=SMTPDataError(500, "nope"))

        with (
            patch("hc.lib.emails.time.sleep"),
            patch("hc.lib.emails.get_connection", return_value=mock_conn),
        ):
            with self.assertRaises(SMTPDataError):
                EmailThread(mock_msg).run()

        mock_conn.close.assert_called()
        self.assertIsNone(mock_msg.connection)
