from __future__ import annotations

from types import SimpleNamespace
from unittest import skipIf
from unittest.mock import Mock, call, patch

from django.test import TestCase
from django.test.utils import override_settings
from hc.lib.s3 import GetObjectError, get_object, remove_objects

try:
    from minio import InvalidResponseError, S3Error
    from urllib3.exceptions import ReadTimeoutError
    from urllib3.exceptions import InvalidHeader, ProtocolError

    have_minio = True
except ImportError:
    have_minio = False


@skipIf(not have_minio, "minio not installed")
@override_settings(S3_BUCKET="dummy-bucket")
class S3TestCase(TestCase):
    @patch("hc.lib.s3.statsd")
    @patch("hc.lib.s3._client")
    def test_get_object_handles_nosuchkey(self, client: Mock, statsd: Mock) -> None:
        e = S3Error(
            code="NoSuchKey",
            message="test-message",
            resource="test-resource",
            request_id="test-request-id",
            host_id="test-host-id",
            response=Mock(),
        )
        client.get_object.return_value.read = Mock(side_effect=e)
        self.assertIsNone(get_object("dummy-code", 1))
        # Should not increase the error counter for NoSuchKey responses
        self.assertEqual(statsd.incr.mock_calls, [call("hc.lib.s3.getObject")])

    @patch("hc.lib.s3.statsd")
    @patch("hc.lib.s3._client")
    def test_get_object_handles_s3error(self, client: Mock, statsd: Mock) -> None:
        e = S3Error(
            code="DummyError",
            message="test-message",
            resource="test-resource",
            request_id="test-request-id",
            host_id="test-host-id",
            response=Mock(),
        )
        client.get_object.return_value.read = Mock(side_effect=e)
        with self.assertRaises(GetObjectError):
            get_object("dummy-code", 1)
        client.get_object.assert_called_once()
        statsd.incr.assert_called_once()

    @patch("hc.lib.s3._client")
    def test_get_object_handles_urllib_exceptions(self, client: Mock) -> None:
        for e in [ProtocolError, InvalidHeader]:
            client.get_object.reset_mock()
            client.get_object.return_value.read = Mock(side_effect=e)
            with self.assertRaises(GetObjectError):
                get_object("dummy-code", 1)
            client.get_object.assert_called_once()

    @patch("hc.lib.s3._client")
    def test_get_object_handles_invalidresponseerror(self, client: Mock) -> None:
        e = InvalidResponseError(123, "text/plain", None)
        client.get_object.return_value.read = Mock(side_effect=e)
        with self.assertRaises(GetObjectError):
            get_object("dummy-code", 1)
        client.get_object.assert_called_once()

    @override_settings(S3_BUCKET=None)
    @patch("hc.lib.s3._client")
    def test_get_object_handles_no_s3_configuration(self, client: Mock) -> None:
        self.assertIsNone(get_object("dummy-code", 1))
        client.get_object.assert_not_called()

    @patch("hc.lib.s3.time.sleep")
    @patch("hc.lib.s3.statsd")
    @patch("hc.lib.s3._client")
    def test_remove_objects_returns_failed_ns(
        self, client: Mock, statsd: Mock, sleep: Mock
    ) -> None:
        from hc.lib.s3 import enc

        listed = [
            SimpleNamespace(object_name=f"code/{enc(1)}"),
            SimpleNamespace(object_name=f"code/{enc(2)}"),
        ]
        client.list_objects.return_value = listed
        # Object n=1 fails, object n=2 succeeds. Every batch reports n=1 as
        # failed so retries never recover it:
        client.remove_objects.side_effect = lambda *a, **k: iter(
            [SimpleNamespace(name=f"code/{enc(1)}", code="X", message="y")]
        )

        failed = remove_objects("code", 10)

        self.assertEqual(failed, {1})

    @patch("hc.lib.s3.time.sleep")
    @patch("hc.lib.s3.statsd")
    @patch("hc.lib.s3._client")
    def test_remove_objects_retries_failed_ns_until_success(
        self, client: Mock, statsd: Mock, sleep: Mock
    ) -> None:
        from hc.lib.s3 import enc

        listed = [SimpleNamespace(object_name=f"code/{enc(5)}")]
        client.list_objects.return_value = listed
        # Fails on the first attempt, succeeds on the retry:
        client.remove_objects.side_effect = [
            iter([SimpleNamespace(name=f"code/{enc(5)}", code="X", message="y")]),
            iter([]),
        ]

        failed = remove_objects("code", 10)

        self.assertEqual(failed, set())
        self.assertEqual(client.remove_objects.call_count, 2)

    @patch("hc.lib.s3.time.sleep")
    @patch("hc.lib.s3.statsd")
    @patch("hc.lib.s3._client")
    def test_remove_objects_relists_after_timeout(
        self, client: Mock, statsd: Mock, sleep: Mock
    ) -> None:
        from hc.lib.s3 import enc

        # First list+delete times out; the re-list finds no objects left:
        client.list_objects.side_effect = [
            [SimpleNamespace(object_name=f"code/{enc(5)}")],
            [],
        ]
        client.remove_objects.side_effect = ReadTimeoutError(None, None, None)

        failed = remove_objects("code", 10)

        self.assertEqual(failed, set())
        self.assertEqual(client.list_objects.call_count, 2)

    def test_remove_objects_no_s3_configuration(self) -> None:
        with patch("hc.lib.s3.settings") as mock_settings:
            mock_settings.S3_BUCKET = None
            self.assertEqual(remove_objects("code", 10), set())
