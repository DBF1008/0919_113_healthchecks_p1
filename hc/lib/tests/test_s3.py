from __future__ import annotations

from unittest import skipIf
from unittest.mock import Mock, call, patch

from django.test import TestCase
from django.test.utils import override_settings
from hc.lib.s3 import GetObjectError, get_object

try:
    from minio import InvalidResponseError, S3Error
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


@skipIf(not have_minio, "minio not installed")
@override_settings(S3_BUCKET="dummy-bucket")
class RemoveObjectsRetryTestCase(TestCase):
    def _make_obj(self, client: Mock, name: str) -> Mock:
        obj = Mock()
        obj.object_name = name
        return obj

    @patch("hc.lib.s3.time.sleep")
    @patch("hc.lib.s3._client")
    def test_remove_retries_and_succeeds(self, client: Mock, mock_sleep: Mock) -> None:
        from hc.lib.s3 import _remove_objects
        from urllib3.exceptions import ReadTimeoutError

        client.list_objects.return_value = [self._make_obj(client, "code/zf-4")]
        client.remove_objects.side_effect = [
            ReadTimeoutError(None, None, None),
            [],
        ]

        _remove_objects("code", 4)

        self.assertEqual(client.remove_objects.call_count, 2)

    @patch("hc.lib.s3.time.sleep")
    @patch("hc.lib.s3._client")
    def test_remove_gives_up_after_max_tries(
        self, client: Mock, mock_sleep: Mock
    ) -> None:
        from hc.lib.s3 import REMOVE_MAX_TRIES, _remove_objects
        from urllib3.exceptions import ReadTimeoutError

        client.list_objects.return_value = [self._make_obj(client, "code/zf-4")]
        client.remove_objects.side_effect = ReadTimeoutError(None, None, None)

        _remove_objects("code", 4)

        self.assertEqual(client.remove_objects.call_count, REMOVE_MAX_TRIES)
        # Backoff happens between attempts, but not after the final one.
        self.assertEqual(mock_sleep.call_count, REMOVE_MAX_TRIES - 1)

    @patch("hc.lib.s3._client")
    def test_remove_retries_on_per_object_errors(self, client: Mock) -> None:
        from hc.lib.s3 import _remove_objects

        err = Mock()
        err.code = "InternalError"
        err.message = "fail"
        client.list_objects.return_value = [self._make_obj(client, "code/zf-4")]
        client.remove_objects.side_effect = [[err], []]

        with patch("hc.lib.s3.time.sleep"):
            _remove_objects("code", 4)

        self.assertEqual(client.remove_objects.call_count, 2)

    @patch("hc.lib.s3._client")
    def test_remove_noop_when_threshold_zero(self, client: Mock) -> None:
        from hc.lib.s3 import _remove_objects

        _remove_objects("code", 0)
        client.list_objects.assert_not_called()

    @patch("hc.lib.s3._client")
    def test_remove_success_lists_nothing(self, client: Mock) -> None:
        from hc.lib.s3 import _remove_objects

        client.list_objects.return_value = []
        _remove_objects("code", 5)
        client.remove_objects.assert_not_called()


@skipIf(not have_minio, "minio not installed")
@override_settings(S3_BUCKET="dummy-bucket")
class PutObjectRetryTestCase(TestCase):
    @patch("hc.lib.s3.time.sleep")
    @patch("hc.lib.s3._client")
    def test_put_retries_on_transport_error(
        self, client: Mock, mock_sleep: Mock
    ) -> None:
        from hc.lib.s3 import put_object

        client.put_object.side_effect = [
            InvalidResponseError(500, "text/plain", None),
            None,
        ]
        put_object("code", 1, b"x" * 200)
        self.assertEqual(client.put_object.call_count, 2)
        mock_sleep.assert_called()

    @patch("hc.lib.s3.time.sleep")
    @patch("hc.lib.s3._client")
    def test_put_retries_on_internal_error(
        self, client: Mock, mock_sleep: Mock
    ) -> None:
        from hc.lib.s3 import put_object

        e = S3Error(
            code="InternalError",
            message="test",
            resource="r",
            request_id="id",
            host_id="h",
            response=Mock(),
        )
        client.put_object.side_effect = [e, None]
        put_object("code", 1, b"x" * 200)
        self.assertEqual(client.put_object.call_count, 2)
