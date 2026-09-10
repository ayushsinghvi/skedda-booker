"""Tests for the multi-account interleaved booking loop and its pure helpers."""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import booker

# Weekday indices per date.weekday(): Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
MON = date(2026, 8, 3)  # a Monday; the target-week anchor used across tests


class LoadAccountsTest(unittest.TestCase):
    def test_reads_numbered_email_password_pairs_in_order(self):
        cfg = {
            "SKEDDA_EMAIL_1": "a@x.com", "SKEDDA_PASSWORD_1": "p1",
            "SKEDDA_EMAIL_2": "b@x.com", "SKEDDA_PASSWORD_2": "p2",
            "SKEDDA_EMAIL_3": "c@x.com", "SKEDDA_PASSWORD_3": "p3",
        }
        accounts = booker.load_accounts(cfg)
        self.assertEqual([a["account"] for a in accounts], [1, 2, 3])
        self.assertEqual(accounts[0]["email"], "a@x.com")
        self.assertEqual(accounts[0]["password"], "p1")
        self.assertEqual(accounts[2]["email"], "c@x.com")

    def test_stops_at_first_missing_index(self):
        cfg = {"SKEDDA_EMAIL_1": "a@x.com", "SKEDDA_PASSWORD_1": "p1",
               "SKEDDA_EMAIL_3": "c@x.com", "SKEDDA_PASSWORD_3": "p3"}
        accounts = booker.load_accounts(cfg)
        self.assertEqual([a["account"] for a in accounts], [1])


class BuildAttemptsTest(unittest.TestCase):
    def test_maps_weekday_index_to_date_and_expands_both_courts(self):
        attempts = booker.build_attempts([(1, 17)], MON)
        tue = date(2026, 8, 4)
        self.assertEqual(attempts, [(tue, 17, booker.COURT_2),
                                    (tue, 17, booker.COURT_1)])


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


MON_D, TUE, WED, FRI = (date(2026, 8, 3), date(2026, 8, 4),
                        date(2026, 8, 5), date(2026, 8, 7))
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
            [("Tue 17:00", 1, [(TUE, 17, booker.COURT_1)])],
            attempt_fn=lambda acct, d, h, s: OK,
            is_expired=NeverExpires(), sleep_fn=self.sleep)
        self.assertEqual(results[0]["booked"], (TUE, 17, booker.COURT_1))
        self.assertEqual(results[0]["reason"], "booked")
        self.assertEqual(results[0]["account"], 1)
        self.assertEqual(self.slept, [])  # all done on first pass -> never paced

    def test_dispatches_each_target_to_its_own_account(self):
        seen = []

        def attempt(acct, d, h, s):
            seen.append((acct, d, h))
            return OK

        booker.run_targets(
            [("Tue 10:00", 1, [(TUE, 10, booker.COURT_1)]),
             ("Mon 09:00", 2, [(MON_D, 9, booker.COURT_1)])],
            attempt_fn=attempt, is_expired=NeverExpires(), sleep_fn=self.sleep)
        self.assertIn((1, TUE, 10), seen)
        self.assertIn((2, MON_D, 9), seen)

    def test_allows_two_bookings_on_the_same_day(self):
        # Account 1 books Tue 10:00 and Tue 17:00 — both must succeed (no
        # one-per-day exclusion any more).
        targets = [
            ("Tue 10:00", 1, [(TUE, 10, booker.COURT_1)]),
            ("Tue 17:00", 1, [(TUE, 17, booker.COURT_1)]),
        ]
        results = booker.run_targets(
            targets, attempt_fn=lambda acct, d, h, s: OK,
            is_expired=NeverExpires(), sleep_fn=self.sleep)
        self.assertEqual(results[0]["booked"], (TUE, 10, booker.COURT_1))
        self.assertEqual(results[1]["booked"], (TUE, 17, booker.COURT_1))

    def test_collision_advances_to_the_other_court(self):
        calls = []

        def attempt(acct, d, h, s):
            calls.append(s)
            return COLLISION if s == booker.COURT_1 else OK

        results = booker.run_targets(
            [("Tue 17:00", 1, [(TUE, 17, booker.COURT_1), (TUE, 17, booker.COURT_2)])],
            attempt_fn=attempt, is_expired=NeverExpires(), sleep_fn=self.sleep)
        self.assertEqual(results[0]["booked"], (TUE, 17, booker.COURT_2))
        self.assertEqual(calls, [booker.COURT_1, booker.COURT_2])
        self.assertEqual(self.slept, [2])  # one pace between the two ticks

    def test_retries_while_release_not_open_then_books_when_it_opens(self):
        state = {"n": 0}

        def attempt(acct, d, h, s):
            state["n"] += 1
            return NOT_OPEN if state["n"] < 3 else OK

        results = booker.run_targets(
            [("Tue 17:00", 1, [(TUE, 17, booker.COURT_1)])],
            attempt_fn=attempt, is_expired=NeverExpires(),
            sleep_fn=self.sleep, tick_seconds=2)
        self.assertEqual(results[0]["reason"], "booked")
        self.assertEqual(state["n"], 3)          # stayed on the same slot, retried
        self.assertEqual(self.slept, [2, 2])     # paced 2s between each retry

    def test_quota_stops_target_without_booking(self):
        results = booker.run_targets(
            [("Tue 17:00", 1, [(TUE, 17, booker.COURT_1)])],
            attempt_fn=lambda acct, d, h, s: QUOTA,
            is_expired=NeverExpires(), sleep_fn=self.sleep)
        self.assertIsNone(results[0]["booked"])
        self.assertEqual(results[0]["reason"], "quota")

    def test_gives_up_at_deadline_with_expired_reason(self):
        results = booker.run_targets(
            [("Tue 17:00", 1, [(TUE, 17, booker.COURT_1)])],
            attempt_fn=lambda acct, d, h, s: NOT_OPEN,
            is_expired=ExpiresAfter(3), sleep_fn=self.sleep)
        self.assertIsNone(results[0]["booked"])
        self.assertEqual(results[0]["reason"], "expired")


class BuildWeekTargetsTest(unittest.TestCase):
    def setUp(self):
        self.targets = booker.build_week_targets(MON)  # MON = 2026-08-03

    def test_one_target_per_plan_entry(self):
        self.assertEqual(len(self.targets), len(booker.WEEKLY_PLAN))

    def test_each_target_is_name_account_attempts(self):
        name, account, attempts = self.targets[0]
        self.assertIsInstance(name, str)
        self.assertIsInstance(account, int)
        self.assertIsInstance(attempts, list)

    def test_account_assignments_match_the_plan(self):
        # (weekday, hour) -> account, as specified by the user.
        by_slot = {(attempts[0][0].weekday(), attempts[0][1]): account
                   for _, account, attempts in self.targets}
        self.assertEqual(by_slot[(1, 16)], 1)   # Tue 16:00 -> A1
        self.assertEqual(by_slot[(1, 17)], 1)   # Tue 17:00 -> A1
        self.assertEqual(by_slot[(2, 9)], 1)    # Wed 09:00 -> A1
        self.assertEqual(by_slot[(3, 16)], 2)   # Thu 16:00 -> A2
        self.assertEqual(by_slot[(3, 17)], 2)   # Thu 17:00 -> A2
        self.assertEqual(by_slot[(5, 10)], 2)   # Sat 10:00 -> A2
        self.assertEqual(by_slot[(5, 11)], 3)   # Sat 11:00 -> A3
        self.assertEqual(by_slot[(5, 17)], 3)   # Sat 17:00 -> A3

    def test_each_target_tries_court2_then_court1(self):
        for _, _, attempts in self.targets:
            self.assertEqual([a[2] for a in attempts],
                             [booker.COURT_2, booker.COURT_1])

    def test_no_account_exceeds_the_weekly_quota_of_three(self):
        counts = {}
        for _, account, _ in self.targets:
            counts[account] = counts.get(account, 0) + 1
        self.assertTrue(all(c <= 3 for c in counts.values()), counts)


if __name__ == "__main__":
    unittest.main()
