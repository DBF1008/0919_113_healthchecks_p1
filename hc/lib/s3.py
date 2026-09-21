from __future__ import annotations

import logging
import time
from io import BytesIO
from threading import Thread
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
            break
        except S3Error as e:
            if e.code == "InternalError" and retries > 0:
                retries -= 1
                print(f"InternalError, retrying ({retries=})...")
                time.sleep(0.5)
                continue

            raise e
        except (InvalidResponseError, HTTPError):
            # Transient transport error (connection reset, read timeout, ...).
            # The Ping row already exists with object_size set; retry the upload
            # so we do not end up with a Ping pointing at a missing body.
            if retries <= 0:
                raise
            retries -= 1
            logger.exception("Transient error in put_object, retrying")
            time.sleep(0.5)


REMOVE_MAX_TRIES = 3


def _remove_objects_once(code: UUID, upto_n: int) -> bool:
    """Delete keys with n <= upto_n. Return True if fully successful."""

    prefix = "%s/" % code
    start_after = prefix + enc(upto_n + 1)
    q = client().list_objects(settings.S3_BUCKET, prefix, start_after=start_after)
    delete_objs = [DeleteObject(obj.object_name) for obj in q]
    if not delete_objs:
        return True

    num_objs = len(delete_objs)
    try:
        with statsd.timer("hc.lib.s3.removeObjectsTime"):
            errors = list(client().remove_objects(settings.S3_BUCKET, delete_objs))
    except ReadTimeoutError:
        logger.exception(
            f"ReadTimeoutError while removing {num_objs} objects for {code}"
        )
        statsd.incr("hc.lib.s3.removeObjectsErrors")
        return False
    except (S3Error, InvalidResponseError, HTTPError):
        # Transient transport/server errors: safe to retry.
        logger.exception(f"Error while removing {num_objs} objects for {code}")
        statsd.incr("hc.lib.s3.removeObjectsErrors")
        return False

    if errors:
        for e in errors:
            statsd.incr("hc.lib.s3.removeObjectsErrors")
            logger.error(
                "remove_objects error for %s: [%s] %s",
                start_after,
                e.code,
                e.message,
            )
        return False

    return True


def _remove_objects(code: UUID, upto_n: int) -> None:
    assert settings.S3_BUCKET
    if upto_n <= 0:
        return

    for attempt in range(REMOVE_MAX_TRIES):
        if _remove_objects_once(code, upto_n):
            return

        if attempt + 1 < REMOVE_MAX_TRIES:
            # Linear backoff before retrying. Listing and deletion are
            # idempotent, so a retry simply cleans up whatever remains.
            time.sleep(attempt + 1)

    # Give up for now. The caller prunes on every 100th ping, so the orphaned
    # objects are picked up and compensated on a subsequent prune run.
    logger.error(
        "Giving up removing objects for %s up to n=%d after %d tries",
        code,
        upto_n,
        REMOVE_MAX_TRIES,
    )


def remove_objects(check_code: str, upto_n: int, wait: bool = False) -> None:
    """Remove keys with n values below or equal to `upto_n`.

    The S3 API calls can take seconds to complete, therefore the removal runs
    on a thread. The function is synchronous in the sense that all retry and
    compensation logic lives in `_remove_objects`; `wait` only controls whether
    the caller joins the worker thread.
    """

    t = Thread(target=_remove_objects, args=(check_code, upto_n), daemon=True)
    t.start()
    if wait:
        t.join()
