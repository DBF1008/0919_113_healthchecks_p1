from __future__ import annotations

from datetime import timedelta as td
from unittest.mock import Mock, patch

from django.utils.timezone import now

from hc.api.management.commands.sendalerts import Command, notify
from hc.api.models import Channel, Check, Flip
from hc.test import BaseTestCase


class SendAlertsTestCase(BaseTestCase):
    def test_it_handles_grace_period(self) -> None:
        check = Check(project=self.project, status="up")
        # 1 day 30 minutes after ping the check is in grace period:
        check.last_ping = now() - td(days=1, minutes=30)
        check.alert_after = check.last_ping + td(days=1, hours=1)
        check.save()

        Command().handle_going_down()

        check.refresh_from_db()
        self.assertEqual(check.status, "up")
        self.assertEqual(Flip.objects.count(), 0)

    def test_it_creates_a_flip_when_check_goes_down(self) -> None:
        check = Check(project=self.project, status="up")
        check.last_ping = now() - td(days=2)
        check.alert_after = check.last_ping + td(days=1, hours=1)
        check.save()

        result = Command().handle_going_down()

        # If it finds work, it should return True
        self.assertTrue(result)

        # It should create a flip object
        flip = Flip.objects.get()
        self.assertEqual(flip.owner_id, check.id)
        self.assertEqual(flip.created, check.alert_after)
        self.assertEqual(flip.new_status, "down")
        self.assertEqual(flip.reason, "timeout")

        # It should change stored status to "down", and clear out alert_after
        check.refresh_from_db()
        self.assertEqual(check.status, "down")
        self.assertEqual(check.alert_after, None)

    @patch("hc.api.management.commands.sendalerts.statsd")
    @patch("hc.api.management.commands.sendalerts.notify")
    def test_it_processes_flip(self, mock_notify: Mock, statsd: Mock) -> None:
        check = Check(project=self.project, status="up")
        check.last_ping = now()
        check.alert_after = check.last_ping + td(days=1, hours=1)
        check.save()

        flip = Flip(owner=check, created=check.last_ping)
        flip.old_status = "down"
        flip.new_status = "up"
        flip.save()

        mock_notify.return_value = "all is well"
        result = Command(stdout=Mock()).process_one_flip()

        # If it finds work, it should return True
        self.assertTrue(result)

        # It should call `notify`
        mock_notify.assert_called_once()

        # It should set the processed date
        flip.refresh_from_db()
        self.assertTrue(flip.processed)

        # It should increase a statsd counter
        statsd.incr.assert_called_once()

    @patch("hc.api.management.commands.sendalerts.notify")
    def test_it_updates_alert_after(self, mock_notify: Mock) -> None:
        check = Check(project=self.project, status="up")
        check.last_ping = now() - td(hours=1)
        check.alert_after = check.last_ping
        check.save()

        result = Command().handle_going_down()

        # If it finds work, it should return True
        self.assertTrue(result)

        # alert_after should have been increased
        expected_aa = check.last_ping + td(days=1, hours=1)
        check.refresh_from_db()
        self.assertEqual(check.alert_after, expected_aa)

        # a flip should have not been created
        self.assertEqual(Flip.objects.count(), 0)

    def test_it_sets_next_nag_date(self) -> None:
        self.profile.nag_period = td(hours=1)
        self.profile.save()

        self.bobs_profile.nag_period = td(hours=1)
        self.bobs_profile.save()

        check = Check(project=self.project, status="down")
        check.last_ping = now() - td(days=2)
        check.save()

        flip = Flip(owner=check, created=check.last_ping)
        flip.old_status = "up"
        flip.new_status = "down"
        flip.save()

        notify(flip)

        # next_nag_gate should now be set for the project's owner
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.next_nag_date)

        # next_nag_gate should now be set for the project's members
        self.bobs_profile.refresh_from_db()
        self.assertIsNotNone(self.bobs_profile.next_nag_date)

    def test_it_clears_next_nag_date(self) -> None:
        self.profile.nag_period = td(hours=1)
        self.profile.next_nag_date = now() - td(minutes=30)
        self.profile.save()

        self.bobs_profile.nag_period = td(hours=1)
        self.bobs_profile.next_nag_date = now() - td(minutes=30)
        self.bobs_profile.save()

        check = Check(project=self.project, status="up")
        check.last_ping = now()
        check.save()

        flip = Flip(owner=check, created=check.last_ping)
        flip.old_status = "down"
        flip.new_status = "up"
        flip.save()

        notify(flip)

        # next_nag_gate should now be cleared out for the project's owner
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.next_nag_date)

        # next_nag_gate should now be cleared out for the project's members
        self.bobs_profile.refresh_from_db()
        self.assertIsNone(self.bobs_profile.next_nag_date)

    def test_it_does_not_touch_already_set_next_nag_dates(self) -> None:
        original_nag_date = now() - td(minutes=30)
        self.profile.nag_period = td(hours=1)
        self.profile.next_nag_date = original_nag_date
        self.profile.save()

        check = Check(project=self.project, status="down")
        check.last_ping = now() - td(days=2)
        check.save()

        flip = Flip(owner=check, created=check.last_ping)
        flip.old_status = "up"
        flip.new_status = "down"
        flip.save()

        notify(flip)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.next_nag_date, original_nag_date)

    def test_it_does_not_clobber_check_status(self) -> None:
        check = Check(project=self.project, status="down")
        check.last_ping = now() - td(days=2)
        check.save()

        flip = Flip(owner=check, created=check.last_ping)
        flip.old_status = "up"
        flip.new_status = "down"
        flip.save()

        channel = Channel.objects.create(project=self.project, kind="webhook")
        channel.checks.add(check)

        with patch("hc.api.models.Channel.transport") as Webhook:
            Webhook.is_noop.return_value = False
            notify(flip)

            args, kwargs = Webhook.notify.call_args
            # Before sending a notification, we used to set flip.owner.status value
            # to "IF_YOU_SEE_THIS_WE_HAVE_A_BUG". The idea was to use it as 0xDEADBEEF:
            # if it surfaces anywhere in notification contents we know we have a bug.
            # Problem is, webhooks have a $JSON placeholder, which calls
            # Check.get_status(), which reads Check.status. So we *must not*
            # clobber flip.owner.status.
            self.assertEqual(args[0].owner.status, "down")


class ClaimFlipTestCase(BaseTestCase):
    def _make_flip(self) -> Flip:
        check = Check.objects.create(project=self.project, status="up")
        check.last_ping = now()
        check.save()
        flip = Flip(owner=check, created=now())
        flip.old_status = "down"
        flip.new_status = "up"
        flip.save()
        return flip

    def test_claim_next_flip_returns_none_when_empty(self) -> None:
        self.assertIsNone(Command(stdout=Mock()).claim_next_flip())

    @patch("hc.api.management.commands.sendalerts.statsd")
    @patch("hc.api.management.commands.sendalerts.notify")
    def test_claim_next_flip_marks_processed(
        self, mock_notify: Mock, statsd: Mock
    ) -> None:
        flip = self._make_flip()

        claimed = Command(stdout=Mock()).claim_next_flip()

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, flip.id)
        claimed.refresh_from_db()
        self.assertIsNotNone(claimed.processed)

    @patch("hc.api.management.commands.sendalerts.statsd")
    @patch("hc.api.management.commands.sendalerts.notify")
    def test_claim_next_flip_is_exclusive(
        self, mock_notify: Mock, statsd: Mock
    ) -> None:
        """Two sequential claims must never return the same flip."""

        flip = self._make_flip()

        cmd = Command(stdout=Mock())
        first = cmd.claim_next_flip()
        second = cmd.claim_next_flip()

        self.assertEqual(first.id if first else None, flip.id)
        self.assertIsNone(second)
        # Exactly one flip, marked processed exactly once.
        self.assertEqual(Flip.objects.filter(processed__isnull=True).count(), 0)

    def test_claim_returns_none_when_racer_marked_it_first(self) -> None:
        """Simulate the losing side of the TOCTOU race.

        The caller picks an unprocessed flip, but a concurrent process marks it
        processed before the conditional UPDATE. The UPDATE matches 0 rows, so
        claim_next_flip() must return None instead of processing a flip already
        owned by someone else.
        """

        flip = self._make_flip()
        cmd = Command(stdout=Mock())

        # Reproduce exactly the fallback branch's two statements, with a
        # concurrent claim happening in between.
        chosen = Flip.objects.filter(processed=None).order_by("id").first()
        self.assertEqual(chosen.id, flip.id)

        # Racer wins:
        Flip.objects.filter(id=flip.id, processed=None).update(processed=now())

        # Our conditional UPDATE now changes nothing:
        num_updated = Flip.objects.filter(id=chosen.id, processed=None).update(
            processed=now()
        )
        self.assertEqual(num_updated, 0)
        self.assertIsNone(cmd.claim_next_flip())


class GoingDownAtomicityTestCase(BaseTestCase):
    def _make_due_check(self) -> Check:
        check = Check.objects.create(project=self.project, status="up")
        check.last_ping = now() - td(days=2)
        check.alert_after = check.last_ping + td(days=1, hours=1)
        check.save()
        return check

    def test_it_rolls_back_status_when_flip_save_fails(self) -> None:
        """status=down and the Flip must commit together.

        If Flip creation fails, the check's status/alert_after change must be
        rolled back as well, so no "down" check exists without its Flip.
        """

        check = self._make_due_check()

        with patch("hc.api.management.commands.sendalerts.Flip.save") as save:
            save.side_effect = RuntimeError("boom")
            with self.assertRaises(RuntimeError):
                Command(stdout=Mock()).handle_going_down()

        check.refresh_from_db()
        self.assertEqual(check.status, "up")
        self.assertIsNotNone(check.alert_after)
        self.assertEqual(Flip.objects.count(), 0)

    def test_loser_does_not_create_duplicate_flip(self) -> None:
        """If the conditional UPDATE matches 0 rows, no Flip is created."""

        check = self._make_due_check()

        # Simulate another worker transitioning the check first:
        Check.objects.filter(id=check.id).update(status="down", alert_after=None)

        # handle_going_down re-reads the due set; the row is now excluded
        # (status=down), so there is nothing to do and no duplicate Flip.
        result = Command(stdout=Mock()).handle_going_down()
        self.assertFalse(result)
        self.assertEqual(Flip.objects.count(), 0)

    def test_it_creates_status_and_flip_in_one_transition(self) -> None:
        check = self._make_due_check()

        Command(stdout=Mock()).handle_going_down()

        check.refresh_from_db()
        self.assertEqual(check.status, "down")
        self.assertIsNone(check.alert_after)
        flip = Flip.objects.get()
        self.assertEqual(flip.owner_id, check.id)
        self.assertEqual(flip.new_status, "down")
        self.assertEqual(flip.old_status, "up")
