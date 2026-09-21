from __future__ import annotations

import logging
import time
from io import BytesIO
from uuid import UUID

from django.conf import settings
from hc.lib.statsd import statsd

try:
    from minio import InvalidResponseError, Minio, S3Error
    from minio.deleteobjects import DeleteObject
    from urllib3 import PoolManager
    from urllib3.exceptions import HTTPError, ReadTimeoutError
    from urllib3.util import Retry
except ImportError:
    # Enforce
    settings.S3_BUCKET = None

_client = None
logger = logging.getLogger(__name__)


class GetObjectError(Exception):
    pass


def client() -> Minio:
    if not settings.S3_BUCKET:
        raise Exception("Object storage is not configured")

    global _client
    if _client is None:
        assert settings.S3_ENDPOINT
        _client = Minio(
            settings.S3_ENDPOINT,
            settings.S3_ACCESS_KEY,
            settings.S3_SECRET_KEY,
            region=settings.S3_REGION,
            secure=settings.S3_SECURE,
            http_client=PoolManager(
                timeout=settings.S3_TIMEOUT, retries=Retry(total=1)
            ),
        )

    return _client


ASCII_J = ord("j")
ASCII_Z = ord("z")


def enc(n: int) -> str:
    """Generate an object key in the "<sorting prefix>-<n>" form.

    >>> [enc(i) for i in range(0, 5)]
    ['zj-0', 'zi-1', 'zh-2', 'zg-3', 'zf-4']

    The purpose of the sorting prefix is to sort keys with smaller n values
    last:

    >>> sorted([enc(i) for i in range(0, 5)])
    ['zf-4', 'zg-3', 'zh-2', 'zi-1', 'zj-0']

    This allows efficient lookup of objects with n
    values below a specific threshold. For example, the following
    retrieves all keys at bucket's root directory with n < 123:

    >>> client.list_objects(bucket_name, start_after=enc(123))
    """
    s = str(n)
    len_inverted = chr(ASCII_Z - len(s) + 1)
    inverted = "".join(chr(ASCII_J - int(c)) for c in s)
    return len_inverted + inverted + "-" + s


def get_object(code: str, n: int) -> bytes | None:
    if not settings.S3_BUCKET:
        return None

    statsd.incr("hc.lib.s3.getObject")
    with statsd.timer("hc.lib.s3.getObjectTime"):
        key = f"{code}/{enc(n)}"
        response = None
        try:
            response = client().get_object(settings.S3_BUCKET, key)
            return response.read()
        except (S3Error, InvalidResponseError, HTTPError) as e:
            if isinstance(e, S3Error) and e.code == "NoSuchKey":
                # It's not an error condition if an object does not exist.
                # Return None, don't log, don't raise.
                return None

            logger.exception(f"{e.__class__.__name__} in hc.lib.s3.get_object")
            raise GetObjectError() from e
        finally:
            if response:
                response.close()
                response.release_conn()


def put_object(code: UUID, n: int, data: bytes) -> None:
    assert settings.S3_BUCKET
    key = "%s/%s" % (code, enc(n))
    retries = 10
    while True:
        try:
            client().put_object(settings.S3_BUCKET, key, BytesIO(data), len(data))
            return
        except (S3Error, InvalidResponseError, HTTPError) as e:
            if isinstance(e, S3Error) and e.code != "InternalError":
                raise e
            if retries == 0:
                raise e
            retries -= 1
            logger.warning(
                "%s while uploading %s, retrying (%d retries left)",
                e.__class__.__name__,
                key,
                retries,
            )
            time.sleep(1)


def _decode_key_n(object_name: str) -> int | None:
    """Extract the ping n value from an object key.

    Keys have the form "<check-code>/<encoded-prefix>-<n>". Returns None if
    the key does not end in an integer.
    """
    try:
        return int(object_name.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return None


def _remove_objects_once(
    code: UUID, upto_n: int, targets: list[str] | None = None
) -> tuple[set[int], bool]:
    """Attempt one batch deletion of objects with n <= upto_n.

    Returns a tuple (failed_ns, timed_out):
    - failed_ns: n values of objects the S3 API reported per-object errors for
    - timed_out: True if the whole batch hit a ReadTimeoutError (the caller
      may re-list and retry because the outcome is unknown)
    """
    assert settings.S3_BUCKET
    if upto_n <= 0:
        return set(), False

    prefix = "%s/" % code
    start_after = prefix + enc(upto_n + 1)
    if targets is None:
        q = client().list_objects(
            settings.S3_BUCKET, prefix, start_after=start_after
        )
        delete_objs = [DeleteObject(obj.object_name) for obj in q]
    else:
        delete_objs = [DeleteObject(name) for name in targets]

    if not delete_objs:
        return set(), False

    num_objs = len(delete_objs)
    try:
        with statsd.timer("hc.lib.s3.removeObjectsTime"):
            errors = client().remove_objects(settings.S3_BUCKET, delete_objs)
            failed: set[int] = set()
            for e in errors:
                statsd.incr("hc.lib.s3.removeObjectsErrors")
                logger.error(
                    "remove_objects error for %s: [%s] %s",
                    start_after,
                    e.code,
                    e.message,
                )
                n_value = _decode_key_n(e.name)
                if n_value is not None:
                    failed.add(n_value)
            return failed, False
    except ReadTimeoutError:
        logger.exception(
            f"ReadTimeoutError while removing {num_objs} objects for {code}"
        )
        statsd.incr("hc.lib.s3.removeObjectsErrors")
        return set(), True


def _remove_objects(code: UUID, upto_n: int) -> set[int]:
    """Remove keys with n values below or equal to `upto_n`, with retries.

    Returns the set of n values which could not be deleted after all retries.
    The caller keeps the corresponding DB Ping rows so no ping body ends up
    referenced in the DB but missing from object storage.
    """
    assert settings.S3_BUCKET
    if upto_n <= 0:
        return set()

    failed_ns, timed_out = _remove_objects_once(code, upto_n)
    if not failed_ns and not timed_out:
        return set()

    prefix = "%s/" % code
    max_attempts = 3
    for attempt in range(2, max_attempts + 1):
        time.sleep(1)
        if timed_out:
            # Outcome of the previous attempt is unknown: re-list and delete
            # whatever is still present.
            retry_failed, retry_timed_out = _remove_objects_once(code, upto_n)
        elif failed_ns:
            targets = [f"{prefix}{enc(n)}" for n in sorted(failed_ns)]
            retry_failed, retry_timed_out = _remove_objects_once(
                code, upto_n, targets=targets
            )
        else:
            retry_failed, retry_timed_out = set(), False

        # Objects that failed before and are not reported by this attempt are
        # assumed gone; the new report is authoritative for what remains.
        failed_ns, timed_out = retry_failed, retry_timed_out

        if not failed_ns and not timed_out:
            return set()

    if timed_out:
        # The final attempt timed out; we cannot identify which objects
        # survived. Surviving objects are harmless orphans that a future
        # prune run cleans up. Do not block the corresponding DB rows.
        logger.error(
            "remove_objects for %s still timing out after %d attempts",
            code,
            max_attempts,
        )
        return set()

    logger.error(
        "remove_objects for %s gave up on %d object(s): %s",
        code,
        len(failed_ns),
        sorted(failed_ns),
    )
    return failed_ns


def remove_objects(check_code: str, upto_n: int, wait: bool = False) -> set[int]:
    """Remove keys with n values below or equal to `upto_n`.

    Runs synchronously so the caller can learn which objects failed deletion
    and keep their DB rows. The API calls have their own timeout/retry logic.

    The `wait` argument is accepted for backwards compatibility and has no
    effect (the call always waits for the result).

    Returns the set of n values which could not be deleted.
    """
    if not settings.S3_BUCKET:
        return set()

    return _remove_objects(check_code, upto_n)
