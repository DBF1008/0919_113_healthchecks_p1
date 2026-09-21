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

    def process_one_flip(self) -> bool:
        """Find unprocessed flip, send notifications.

        Return True if the main loop should continue right away.

        Return False if the main loop should  wait a bit before continuing.
        (because either all workers are currently busy or there are currently no
        unprocessed flips in the database).

        """

        if not self.seats.acquire(timeout=1):
            return False  # Workers busy, main thread should wait a bit

        flip = None
        try:
            # Atomically pick and claim an unprocessed flip.
            #
            # SELECT ... FOR UPDATE SKIP LOCKED locks the row until the
            # transaction commits, so other sendalerts processes/threads skip
            # over this flip instead of racing to claim it. This replaces the
            # old read-then-update sequence which was a TOCTOU race: two
            # processes could read the same flip before either marked it.
            with transaction.atomic():
                qs = Flip.objects.select_for_update(skip_locked=True)
                flip = qs.filter(processed=None).order_by("id").first()
                if flip is None:
                    return False  # No work found, main thread should wait a bit

                processed_time = now()
                num_updated = Flip.objects.filter(
                    id=flip.id, processed=None
                ).update(processed=processed_time)
                if num_updated != 1:
                    # Should be impossible while we hold the row lock, but keep
                    # the defensive check in place.
                    logger.warning(
                        "Failed to claim flip %s (%s row(s) updated)",
                        flip.id,
                        num_updated,
                    )
                    return True

                # Remember the claimed timestamp so we can restore it if the
                # worker pool refuses the job.
                flip.processed = processed_time
        finally:
            # The seat is released either in on_notify_done (if the job was
            # submitted) or here (if there was no work / claim failed).
            if flip is None or flip.processed is None:
                self.seats.release()

        statsd.incr("hc.sendalerts.processFlip")
        try:
            f = self.executor.submit(notify, flip)
        except RuntimeError:
            # executor.shutdown() was called (shutdown in progress). Un-claim
            # the flip so a future run picks it up.
            Flip.objects.filter(id=flip.id).update(processed=None)
            self.seats.release()
            return False

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

        # Everything from candidate selection through flip creation happens in
        # one transaction while holding a row lock:
        # - FOR UPDATE SKIP LOCKED makes concurrent sendalerts processes skip
        #   checks already being handled by another process (TOCTOU fix).
        # - The check row lock is the same lock Check.ping() takes via
        #   select_for_update(), so a flip created by ping() is always visible
        #   to us and vice versa (no "check is down but no flip" window).
        # - The status update and the Flip insert commit atomically, so a crash
        #   can never leave a down check without its flip.
        with transaction.atomic():
            q = (
                Check.objects.select_for_update(skip_locked=True)
                .filter(alert_after__lt=now())
                .exclude(status="down")
            )
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
            # must be able to calculate precisely when the check's state
            # flipped.
            assert flip_time

            # Atomically update status
            num_updated = q.update(alert_after=None, status="down")
            if num_updated != 1:
                # Nothing got updated: another worker process got there first.
                # (Unreachable with SKIP LOCKED, kept as a defensive guard.)
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
