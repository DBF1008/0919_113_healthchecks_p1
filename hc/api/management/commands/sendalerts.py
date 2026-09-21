from __future__ import annotations

import logging
import signal
import time
from argparse import ArgumentParser
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta as td
from threading import BoundedSemaphore
from types import FrameType
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections, connection, transaction
from django.utils.timezone import now

from hc.api.models import Check, Flip
from hc.lib.statsd import statsd

logger = logging.getLogger("hc")


def notify(flip: Flip) -> str | None:
    # This is run via ThreadPoolExecutor. The thread may already have an open
    # db connection. If notify has not run recently then the db connection may have
    # timed out. We call close_old_connections() to make sure we have a working db
    # connection. The if condition makes sure this does not run during tests.
    if not connection.in_atomic_block:
        close_old_connections()

    # Set or clear dates for followup nags
    check = flip.owner
    check.project.update_next_nag_dates()
    channels = flip.select_channels()
    if not channels:
        return None

    send_start = now()
    logs = [f"{check.code} goes {flip.new_status}"]
    for ch in channels:
        notify_start = time.time()
        error = ch.notify(flip)
        secs = time.time() - notify_start
        code8 = str(ch.code)[:8]
        if error:
            logs.append(f"  {code8} ({ch.kind}) Error in {secs:.1f}s: {error}")
        else:
            logs.append(f"  {code8} ({ch.kind}) OK in {secs:.1f}s")

    statsd.timing("hc.sendalerts.dwellTime", send_start - flip.created)
    statsd.timing("hc.sendalerts.sendTime", now() - send_start)
    return "\n".join(logs)


class Command(BaseCommand):
    help = "Sends UP/DOWN email alerts"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.executor = ThreadPoolExecutor(max_workers=10)
        self.seats = BoundedSemaphore(10)
        self.shutdown = False

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--num-workers",
            type=int,
            default=1,
            help="The number of concurrent worker processes to use",
        )

        parser.add_argument(
            "--pool",
            action="store_true",
            help="Use DB connection pool (PostgreSQL-only)",
        )

    def on_notify_done(self, future: Future[str | None]) -> None:
        self.seats.release()

        try:
            if logs := future.result():
                self.stdout.write(logs)
        except Exception as exc:
            logger.error("Exception in notify", exc_info=exc)
            raise exc

    def claim_next_flip(self) -> Flip | None:
        """Atomically claim the oldest unprocessed flip.

        Concurrent sendalerts processes must never claim the same flip.
        On backends that support row-level locks we atomically pick and lock
        an unprocessed flip with ``SELECT ... FOR UPDATE SKIP LOCKED`` inside a
        short transaction; every other process simply skips the locked row.
        On backends without row-level locks (SQLite) we fall back to a
        conditional UPDATE, which is itself atomic.
        """

        qs = Flip.objects.filter(processed=None).order_by("id")
        if connection.features.has_select_for_update:
            with transaction.atomic():
                qs = qs.select_for_update(
                    skip_locked=connection.features.supports_select_for_update_skip_locked
                )
                flip = qs.first()
                if flip is None:
                    return None

                Flip.objects.filter(id=flip.id, processed=None).update(processed=now())
            flip.refresh_from_db()
            return flip

        # No FOR UPDATE support: claim with an atomic conditional UPDATE.
        flip = qs.first()
        if flip is None:
            return None

        num_updated = Flip.objects.filter(id=flip.id, processed=None).update(
            processed=now()
        )
        if num_updated != 1:
            # Another sendalerts process claimed it first.
            return None

        flip.refresh_from_db()
        return flip

    def process_one_flip(self) -> bool:
        """Find unprocessed flip, send notifications.

        Return True if the main loop should continue right away.

        Return False if the main loop should  wait a bit before continuing.
        (because either all workers are currently busy or there are currently no
        unprocessed flips in the database).

        """

        if not self.seats.acquire(timeout=1):
            return False  # Workers busy, main thread should wait a bit

        try:
            flip = self.claim_next_flip()
        except Exception:
            self.seats.release()
            raise

        if flip is None:
            self.seats.release()
            # No work found, or another process claimed it first.
            # Distinguish the two via a cheap existence check so the main loop
            # knows whether to keep polling or back off.
            if Flip.objects.filter(processed=None).exists():
                return True
            return False

        statsd.incr("hc.sendalerts.processFlip")
        f = self.executor.submit(notify, flip)
        f.add_done_callback(self.on_notify_done)
        return True

    def handle_going_down(self) -> bool:
        """Process a single check going down.

        1. Find a check with alert_after in the past, and status other than "down".
        2. Calculate its current status.
        3. If calculation throws an exception, push alert_after forward and re-raise.
        4. If the current status is not "down", update alert_after and return.
        5. Update the check's status in the database to "down".
        6. If exactly 1 row gets updated, create a Flip object.

        """

        q = Check.objects.filter(alert_after__lt=now()).exclude(status="down")
        # Sort by alert_after, to avoid unnecessary sorting by id:
        check = q.order_by("alert_after").first()
        if check is None:
            return False

        old_status = check.status
        q = Check.objects.filter(id=check.id, status=old_status)

        try:
            status = check.get_status()
        except Exception as e:
            # Make sure we don't trip on this check again for an hour:
            # Otherwise sendalerts may end up in a crash loop.
            q.update(alert_after=now() + td(hours=1))
            # Then re-raise the exception:
            raise e

        if status != "down":
            # It is not down yet. Update alert_after
            q.update(alert_after=check.going_down_after())
            return True

        flip_time = check.going_down_after()
        # In theory, going_down_after() can return None, but:
        # get_status() just reported status "down", so "going_down_after()"
        # must be able to calculate precisely when the check's state flipped.
        assert flip_time

        # Atomically flip the check and create its Flip in one transaction.
        # If another worker got there first, the conditional UPDATE matches 0
        # rows, we create no Flip, and the check is left exactly as that worker
        # left it. Doing the UPDATE and INSERT in the same transaction also
        # guarantees other sessions can never observe status="down" without the
        # corresponding Flip (under READ COMMITTED), so sendalerts cannot miss
        # the alert.
        with transaction.atomic():
            # The conditional UPDATE takes a row lock for its duration, so two
            # sendalerts processes serialise here: exactly one UPDATE matches
            # the row. Because the Flip INSERT is in the same transaction, other
            # sessions cannot observe status="down" without the Flip.
            num_updated = q.update(alert_after=None, status="down")
            if num_updated != 1:
                # Nothing got updated: another worker process got there first.
                return True

            flip = Flip(owner=check)
            flip.created = flip_time
            flip.old_status = old_status
            flip.new_status = "down"
            flip.reason = "timeout"
            flip.save()

        return True

    def on_signal(self, signum: int, frame: FrameType | None) -> None:
        desc = signal.strsignal(signum)
        self.stdout.write(f"{desc}, finishing...\n")
        self.shutdown = True

    def handle(self, num_workers: int, pool: bool, **options: Any) -> str:
        db = settings.DATABASES["default"]
        if "OPTIONS" in db and "application_name" in db["OPTIONS"]:
            db["OPTIONS"]["application_name"] = "sendalerts"

        if pool:
            self.stdout.write(
                "WARNING: The --pool argument is not supported any more and will be ignored.\n"
            )

        self.seats = BoundedSemaphore(num_workers)
        self.executor = ThreadPoolExecutor(max_workers=num_workers)

        signal.signal(signal.SIGTERM, self.on_signal)
        signal.signal(signal.SIGINT, self.on_signal)

        self.stdout.write("sendalerts is now running\n")
        while not self.shutdown:
            # Create flips for any checks going down
            while self.handle_going_down() and not self.shutdown:
                pass

            # Submit unprocessed flips to the self.executor
            while self.process_one_flip() and not self.shutdown:
                pass

            # Either all workers are busy or there are no unprocessed flips.
            # Wait a bit:
            if not self.shutdown:
                time.sleep(2)

        self.executor.shutdown(wait=True)
        return "Done."
