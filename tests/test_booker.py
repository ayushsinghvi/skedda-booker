"""Tests for the interleaved booking loop and its pure helpers."""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import booker

# Weekday indices per date.weekday(): Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
MON = date(2026, 8, 3)  # a Monday; the target-week anchor used across tests


class WeekdayTargetSlotsTest(unittest.TestCase):
    def test_primary_day_times_come_first_in_preference_order(self):
        slots = booker.weekday_target_slots(
            primary_day=1, spillover_days=[0, 2, 4], times=[17, 18, 16, 11, 12, 13])
        self.assertEqual(slots[:6],
                         [(1, 17), (1, 18), (1, 16), (1, 11), (1, 12), (1, 13)])

    def test_spillover_is_time_major_across_days(self):
        slots = booker.weekday_target_slots(
            primary_day=1, spillover_days=[0, 2, 4], times=[17, 18, 16, 11, 12, 13])
        # After the 6 primary-day slots: 5PM across Mon/Wed/Fri before any 6PM slot.
        self.assertEqual(slots[6:9], [(0, 17), (2, 17), (4, 17)])
        self.assertEqual(slots[9:12], [(0, 18), (2, 18), (4, 18)])

    def test_spillover_never_uses_the_reserved_other_primary_day(self):
        # Target A reserves Thu (3) for Target B: it must never appear.
        slots = booker.weekday_target_slots(
            primary_day=1, spillover_days=[0, 2, 4], times=[17, 18, 16, 11, 12, 13])
        self.assertFalse(any(day == 3 for day, _ in slots))


class WeekendTargetSlotsTest(unittest.TestCase):
    def test_time_major_across_saturday_then_sunday(self):
        slots = booker.weekend_target_slots(days=[5, 6], times=[10, 11, 12, 13])
        self.assertEqual(slots, [(5, 10), (6, 10), (5, 11), (6, 11),
                                 (5, 12), (6, 12), (5, 13), (6, 13)])


class BuildAttemptsTest(unittest.TestCase):
    def test_maps_weekday_index_to_date_and_expands_both_courts(self):
        attempts = booker.build_attempts([(1, 17)], MON)
        tue = date(2026, 8, 4)
        self.assertEqual(attempts, [(tue, 17, booker.COURT_1),
                                    (tue, 17, booker.COURT_2)])


QUOTA_MSG = ("This booking cannot be confirmed because it would mean that your quota "
             "is exceeded for the week 7/27/26 to 8/2/26. Specifically, you are allowed "
             "an individual maximum of 3 booking(s) across the space(s) Tennis Court 1, "
             "Tennis Court 2.")
COLLISION_MSG = ("We couldn't put in your booking because it conflicts with one already "
                 "scheduled on Tuesday, July 28, 2026, 4:00 PM (Tennis Court 1). "
                 "Conflicting bookings are not allowed, so resolve the conflict and give "
                 "it another go!")


class ClassifyTest(unittest.TestCase):
    def test_success_status_is_booked(self):
        self.assertEqual(booker.classify(200, ""), "booked")
        self.assertEqual(booker.classify(201, ""), "booked")

    def test_quota_message_is_quota(self):
        self.assertEqual(booker.classify(422, QUOTA_MSG), "quota")

    def test_collision_message_is_collision(self):
        self.assertEqual(booker.classify(422, COLLISION_MSG), "collision")

    def test_unknown_422_is_retry(self):
        # The "release not open yet" error is unknown in advance, so any other 422
        # must fall through to retry — the readiness gate depends on this.
        self.assertEqual(booker.classify(422, "Some message we've never seen"), "retry")

    def test_auth_failure_is_auth(self):
        self.assertEqual(booker.classify(401, ""), "auth")
        self.assertEqual(booker.classify(403, ""), "auth")


TUE, WED, THU, FRI = (date(2026, 8, 4), date(2026, 8, 5),
                      date(2026, 8, 6), date(2026, 8, 7))
OK = (200, "")
COLLISION = (422, COLLISION_MSG)
QUOTA = (422, QUOTA_MSG)
NOT_OPEN = (422, "release not open yet — some unknown message")


class NeverExpires:
    def __call__(self):
        return False


class ExpiresAfter:
    """is_expired() that returns True once it has been polled `n` times."""
    def __init__(self, n):
        self.n, self.calls = n, 0

    def __call__(self):
        self.calls += 1
        return self.calls > self.n


class RunTargetsTest(unittest.TestCase):
    def setUp(self):
        self.slept = []

    def sleep(self, secs):
        self.slept.append(secs)

    def test_books_first_attempt_and_stops_without_sleeping(self):
        results = booker.run_targets(
            [("A", [(TUE, 17, booker.COURT_1)])],
            attempt_fn=lambda d, h, s: OK,
            is_expired=NeverExpires(), sleep_fn=self.sleep)
        self.assertEqual(results[0]["booked"], (TUE, 17, booker.COURT_1))
        self.assertEqual(results[0]["reason"], "booked")
        self.assertEqual(self.slept, [])  # all done on first pass -> never paced

    def test_never_books_two_targets_on_the_same_day(self):
        # Both targets' best attempt is Wed; A grabs it, B must fall to Fri.
        targets = [
            ("A", [(WED, 17, booker.COURT_1)]),
            ("B", [(WED, 17, booker.COURT_1), (FRI, 17, booker.COURT_1)]),
        ]
        results = booker.run_targets(
            targets, attempt_fn=lambda d, h, s: OK,
            is_expired=NeverExpires(), sleep_fn=self.sleep)
        self.assertEqual(results[0]["booked"], (WED, 17, booker.COURT_1))
        self.assertEqual(results[1]["booked"], (FRI, 17, booker.COURT_1))

    def test_collision_advances_to_the_other_court(self):
        calls = []

        def attempt(d, h, s):
            calls.append(s)
            return COLLISION if s == booker.COURT_1 else OK

        results = booker.run_targets(
            [("A", [(TUE, 17, booker.COURT_1), (TUE, 17, booker.COURT_2)])],
            attempt_fn=attempt, is_expired=NeverExpires(), sleep_fn=self.sleep)
        self.assertEqual(results[0]["booked"], (TUE, 17, booker.COURT_2))
        self.assertEqual(calls, [booker.COURT_1, booker.COURT_2])
        self.assertEqual(self.slept, [2])  # one pace between the two ticks

    def test_retries_while_release_not_open_then_books_when_it_opens(self):
        state = {"n": 0}

        def attempt(d, h, s):
            state["n"] += 1
            return NOT_OPEN if state["n"] < 3 else OK

        results = booker.run_targets(
            [("A", [(TUE, 17, booker.COURT_1)])],
            attempt_fn=attempt, is_expired=NeverExpires(),
            sleep_fn=self.sleep, tick_seconds=2)
        self.assertEqual(results[0]["reason"], "booked")
        self.assertEqual(state["n"], 3)          # stayed on the same slot, retried
        self.assertEqual(self.slept, [2, 2])     # paced 2s between each retry

    def test_quota_stops_target_without_booking(self):
        results = booker.run_targets(
            [("A", [(TUE, 17, booker.COURT_1)])],
            attempt_fn=lambda d, h, s: QUOTA,
            is_expired=NeverExpires(), sleep_fn=self.sleep)
        self.assertIsNone(results[0]["booked"])
        self.assertEqual(results[0]["reason"], "quota")

    def test_gives_up_at_deadline_with_expired_reason(self):
        results = booker.run_targets(
            [("A", [(TUE, 17, booker.COURT_1)])],
            attempt_fn=lambda d, h, s: NOT_OPEN,
            is_expired=ExpiresAfter(3), sleep_fn=self.sleep)
        self.assertIsNone(results[0]["booked"])
        self.assertEqual(results[0]["reason"], "expired")


class BuildWeekTargetsTest(unittest.TestCase):
    def setUp(self):
        self.targets = booker.build_week_targets(MON)  # MON = 2026-08-03

    def test_three_targets_named_A_B_C(self):
        self.assertEqual([t[0] for t in self.targets], ["A", "B", "C"])

    def test_primary_first_attempts_are_mon_wed_fri_10am_on_court1(self):
        firsts = {name: attempts[0] for name, attempts in self.targets}
        self.assertEqual(firsts["A"], (date(2026, 8, 3), 10, booker.COURT_1))  # Mon 10AM
        self.assertEqual(firsts["B"], (date(2026, 8, 5), 10, booker.COURT_1))  # Wed 10AM
        self.assertEqual(firsts["C"], (date(2026, 8, 7), 10, booker.COURT_1))  # Fri 10AM

    def test_each_target_stays_pinned_to_its_own_day(self):
        days = {name: {d.weekday() for d, _, _ in attempts}
                for name, attempts in self.targets}
        self.assertEqual(days["A"], {0})  # Monday only
        self.assertEqual(days["B"], {2})  # Wednesday only
        self.assertEqual(days["C"], {4})  # Friday only


if __name__ == "__main__":
    unittest.main()
