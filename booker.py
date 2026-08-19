#!/usr/bin/env python3
"""Skedda booker — POC.

Logs into a private Skedda venue and submits bookings via the JSON API (no browser).
Login is decoupled from booking: `login` authenticates once and persists the session
(auth cookies + antiforgery token + venue context) to disk; `book` and `list-spaces`
load that saved session and skip the login round-trips entirely.

Discovered contract for Kerry Sports Manila (ksmbooking):
  login:  GET  app.skedda.com/account/login         -> antiforgery cookie + token
          POST app.skedda.com/logins         (JSON)  -> validate creds, set auth cookie
          POST app.skedda.com/account/applogin (form) -> finalize auth cookies (302)
  data:   GET  ksmbooking.skedda.com/webs            -> spaces, venue id, user id, token
  write:  POST ksmbooking.skedda.com/bookings (JSON, X-Skedda-RequestVerificationToken)

Usage:
  python booker.py login            # authenticate and persist session to .session.json
  python booker.py list-spaces      # uses the saved session
  python booker.py book --space 1399963 --start 2026-08-15T09:00:00 --end 2026-08-15T10:00:00
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from dotenv import dotenv_values

CFG = dotenv_values(".env")
VENUE_SUB = CFG.get("SKEDDA_VENUE", "ksmbooking")
APP = "https://app.skedda.com"
VENUE = f"https://{VENUE_SUB}.skedda.com"
BOOKING_PAGE = f"{VENUE}/booking"
MANILA = ZoneInfo("Asia/Manila")
COURT_1 = "1399963"   # Tennis Court 1
COURT_2 = "1399964"   # Tennis Court 2
COURTS = [COURT_1, COURT_2]   # court doesn't matter; try 1 then 2 at each slot
SESSION_FILE = os.environ.get("SKEDDA_SESSION_FILE", ".session.json")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

TOKEN_RE = re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"')
META_TOKEN_RE = re.compile(r'name="[^"]*[Vv]erification[^"]*"[^>]*content="([^"]+)"')


def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


def _scrape_token(html):
    m = TOKEN_RE.search(html) or META_TOKEN_RE.search(html)
    return m.group(1) if m else None


def login(s, email, password):
    """Authenticate the session. Returns the login-page antiforgery token."""
    r = s.get(f"{APP}/account/login",
              params={"returnUrl": f"{BOOKING_PAGE}?viewtype=0"})
    r.raise_for_status()
    token = _scrape_token(r.text)
    if not token:
        raise RuntimeError("could not find antiforgery token on login page")

    # Step 1: JSON credential validation.
    r1 = s.post(f"{APP}/logins",
                json={"login": {"username": email, "password": password,
                                "rememberMe": False, "redirectUrl": None,
                                "arbitraryerrors": None}},
                headers={"X-Skedda-RequestVerificationToken": token,
                         "Content-Type": "application/json; charset=utf-8",
                         "Accept": "application/json, text/plain, */*"})
    if r1.status_code not in (200, 201, 204):
        raise RuntimeError(f"/logins failed: {r1.status_code} {r1.text[:300]}")

    # Step 2: form login that sets the auth cookies (302 redirect).
    r2 = s.post(f"{APP}/account/applogin",
                data={"username": email, "password": password,
                      "ReturnUrl": f"{BOOKING_PAGE}?viewtype=0"},
                headers={"X-Skedda-RequestVerificationToken": token,
                         "Content-Type": "application/x-www-form-urlencoded"},
                allow_redirects=True)
    if r2.status_code >= 400:
        raise RuntimeError(f"/account/applogin failed: {r2.status_code}")
    return token


def get_context(s):
    """Fetch /webs -> dict with spaces, venue_id, venueuser_id, and write token.

    The API requires the antiforgery token (scraped from the authenticated booking
    page) on the X-Skedda-RequestVerificationToken header for both reads and writes.
    """
    rp = s.get(f"{BOOKING_PAGE}?viewtype=0")
    token = _scrape_token(rp.text)
    if not token:
        raise RuntimeError("could not scrape antiforgery token from booking page "
                           "(login likely failed)")

    r = s.get(f"{VENUE}/webs", headers={
        "Accept": "application/json, text/plain, */*",
        "X-Skedda-RequestVerificationToken": token})
    if r.status_code in (401, 422):
        raise RuntimeError(f"/webs unauthorized ({r.status_code}) — login likely failed")
    r.raise_for_status()
    data = r.json()
    venue = data["venue"][0] if isinstance(data.get("venue"), list) else data.get("venue", {})
    vus = data.get("venueusers", [])
    spaces = [{"id": a["id"], "name": a["name"]} for a in data.get("assets", [])]
    return {
        "venue_id": venue.get("id"),
        "timezone": venue.get("timeZoneId") or venue.get("timezone"),
        "venueuser_id": vus[0]["id"] if vus else None,
        "spaces": spaces,
        "write_token": token,
    }


def save_session(s, ctx, path=SESSION_FILE):
    """Persist auth cookies + antiforgery token + venue context to disk.

    The file contains live auth cookies, so it is written with 0600 permissions
    and must stay git-ignored.
    """
    data = {
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cookies": [{"name": c.name, "value": c.value,
                     "domain": c.domain, "path": c.path} for c in s.cookies],
        "write_token": ctx["write_token"],
        "venue_id": ctx["venue_id"],
        "venueuser_id": ctx["venueuser_id"],
        "timezone": ctx.get("timezone"),
        "spaces": ctx.get("spaces", []),
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)


def load_session(path=SESSION_FILE):
    """Rebuild a requests.Session + ctx from a saved session file.

    Exits with guidance if no session exists yet. Does NOT contact the network.
    """
    if not os.path.exists(path):
        raise SystemExit(f"No saved session at {path}. Run: python booker.py login")
    with open(path) as f:
        data = json.load(f)
    s = new_session()
    for c in data.get("cookies", []):
        s.cookies.set(c["name"], c["value"],
                      domain=c.get("domain"), path=c.get("path", "/"))
    ctx = {k: data.get(k) for k in
           ("write_token", "venue_id", "venueuser_id", "timezone", "spaces")}
    return s, ctx, data.get("saved_at")


def following_week(run_dt=None):
    """Return the 7 dates (Mon..Sun) of the week following the run date, Manila time.

    Run on a Thursday, this yields next Monday (+4d) through next Sunday (+10d),
    which is exactly the window that opens at Thu 9AM. Computed in Asia/Manila so
    it stays correct even when the process runs in UTC (the cloud).
    """
    now = run_dt or datetime.now(MANILA)
    today = now.date()
    days_to_mon = (7 - today.weekday()) % 7 or 7   # always the *next* Monday
    monday = today + timedelta(days=days_to_mon)
    return [monday + timedelta(days=i) for i in range(7)]


def wait_until(hms, tz=MANILA):
    """Block until today's HH:MM:SS in the given tz. Returns immediately if past."""
    h, m, sec = (int(x) for x in hms.split(":"))
    now = datetime.now(tz)
    target = now.replace(hour=h, minute=m, second=sec, microsecond=0)
    delay = (target - now).total_seconds()
    if delay > 0:
        print(f"waiting {delay:.2f}s until {hms} {tz.key}...", file=sys.stderr)
        time.sleep(delay)


def weekday_target_slots(primary_day, spillover_days, times):
    """Ordered (weekday_index, hour) ladder for a weekday target.

    Primary day first, all times in preference order (we'd rather keep the day and
    shift the hour); then spillover time-major across the allowed days (the preferred
    hour on every spillover day before degrading to the next hour). The reserved
    other-primary day is simply absent from `spillover_days`, so it never appears.
    """
    slots = [(primary_day, t) for t in times]
    for t in times:
        for d in spillover_days:
            slots.append((d, t))
    return slots


def weekend_target_slots(days, times):
    """Ordered (weekday_index, hour) ladder for the weekend target, time-major:
    the preferred hour on each day before degrading to the next hour."""
    return [(d, t) for t in times for d in days]


def build_attempts(slots, week_monday):
    """Expand (weekday_index, hour) slots into concrete (date, hour, space_id) attempts.

    Each slot yields one attempt per court (Court 1 then Court 2), so the interleaved
    loop advances court-by-court then candidate-by-candidate through this flat list.
    """
    attempts = []
    for wd, hour in slots:
        d = week_monday + timedelta(days=wd)
        for court in COURTS:
            attempts.append((d, hour, court))
    return attempts


def classify(status, text):
    """Map a booking response to an outcome the interleaved loop acts on.

    booked    -> 2xx, slot secured.
    quota     -> weekly 3-booking limit hit; stop this target (retrying won't help).
    collision -> slot already taken; advance to the next candidate/court.
    auth      -> session expired/invalid; abort the run and re-login.
    retry     -> anything else, INCLUDING the (unknown-in-advance) "release not open
                 yet" error. The readiness gate relies on this residual bucket.

    Error signatures captured verbatim from live probes; see the booking-window notes.
    """
    if status in (200, 201):
        return "booked"
    if status in (401, 403):
        return "auth"
    low = (text or "").lower()
    if "quota is exceeded" in low or "individual maximum" in low:
        return "quota"
    if ("conflicts with one already scheduled" in low
            or "conflicting bookings are not allowed" in low):
        return "collision"
    return "retry"


# Preferred hours, in the user's stated preference order (24h): 10AM leads,
# then the prior ladder.
PREFERRED_TIMES = [10, 17, 18, 16, 11, 12, 13]  # 10AM, 5PM, 6PM, 4PM, 11AM, 12PM, 1PM
PREFERRED_DAYS = [0, 2, 4]                       # Mon, Wed, Fri — one target per day


def build_week_targets(monday):
    """Assemble the three weekly targets (name, attempts) for the target week.

    One target per preferred day (Mon, Wed, Fri). Each stays pinned to its own
    day — no cross-day spillover — so the three bookings land one-per-day, and
    within the day degrade down the PREFERRED_TIMES ladder (10AM first).
    """
    names = ["A", "B", "C"]
    return [(name, build_attempts(weekday_target_slots(day, [], PREFERRED_TIMES), monday))
            for name, day in zip(names, PREFERRED_DAYS)]


def run_targets(targets, attempt_fn, is_expired, sleep_fn=time.sleep, tick_seconds=2):
    """Interleaved booking loop — concurrent in effect, sequential in execution.

    `targets` is a list of (name, attempts), each attempts being an ordered list of
    (date, hour, space_id) from build_attempts(). Each tick fires ONE booking per
    unresolved target (in list order) at its current-best candidate, then paces
    `tick_seconds` before the next tick. Single-threaded, so the never-two-bookings-
    on-the-same-day rule needs no locking: a booked day is recorded immediately and
    every other target skips it.

    Outcomes (see classify): booked -> done + claim the day; collision -> advance to
    the next candidate/court; quota -> stop this target; auth -> abort; retry (incl.
    the not-yet-released case) -> stay on the same candidate and try again next tick.
    Runs until every target is resolved or is_expired() (the 09:05 deadline) fires.

    Returns a list of {name, booked, reason} — reason in
    {booked, collision-exhausted, quota, expired}.
    """
    n = len(targets)
    idx = [0] * n
    done = [False] * n
    booked = [None] * n
    reason = [None] * n
    claimed_days = set()

    def skip_claimed(i):
        attempts = targets[i][1]
        while idx[i] < len(attempts) and attempts[idx[i]][0] in claimed_days:
            idx[i] += 1

    while not all(done):
        if is_expired():
            for i in range(n):
                if not done[i]:
                    done[i], reason[i] = True, "expired"
            break
        for i, (name, attempts) in enumerate(targets):
            if done[i]:
                continue
            skip_claimed(i)
            if idx[i] >= len(attempts):
                done[i], reason[i] = True, "exhausted"
                continue
            d, hour, space = attempts[idx[i]]
            outcome = classify(*attempt_fn(d, hour, space))
            if outcome == "booked":
                booked[i], done[i], reason[i] = attempts[idx[i]], True, "booked"
                claimed_days.add(d)
            elif outcome == "collision":
                idx[i] += 1
            elif outcome == "quota":
                done[i], reason[i] = True, "quota"
            elif outcome == "auth":
                raise RuntimeError("session expired mid-run — re-login and retry")
            # else: retry -> leave idx[i] in place, try again next tick
        if not all(done):
            sleep_fn(tick_seconds)

    return [{"name": targets[i][0], "booked": booked[i], "reason": reason[i]}
            for i in range(n)]


def book(s, ctx, space_id, start, end, title=None):
    """POST a single booking. start/end are local ISO strings 'YYYY-MM-DDTHH:MM:SS'."""
    payload = {"booking": {
        "endOfLastOccurrence": None, "title": title, "price": 0,
        "claimedAllowanceUsage": None, "chargeTransactionId": None, "invoiceId": None,
        "hasMultiplePaymentIntents": False, "hasAnyRefunds": False, "lockInMargin": 12,
        "stripPrivateEventDetails": False, "unrecognizedOrganizer": False,
        "hasActiveAccessCode": False, "type": 1, "paymentStatus": 0,
        "recurrenceRule": None, "decoupleDate": None, "createdDate": None,
        "customFields": [], "piId": None, "checkInAudits": None,
        "allowInviteOthers": False, "addConference": False, "hideAttendees": True,
        "availabilityStatus": 1, "syncType": None, "conferenceLinkType": 0,
        "conferenceJoinUrl": None, "attendees": [], "appliedDiscountCode": None,
        "approvalNote": None, "start": start, "end": end, "arbitraryerrors": None,
        "spaces": [str(space_id)], "addOns": [],
        "venueuser": str(ctx["venueuser_id"]), "venue": str(ctx["venue_id"]),
        "decoupleBooking": None,
    }}
    r = s.post(f"{VENUE}/bookings", json=payload, headers={
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json, text/plain, */*",
        "X-Skedda-RequestVerificationToken": ctx["write_token"] or "",
    })
    return r


def _extract_error(text):
    """Best-effort clean error message from a Skedda JSON error response."""
    try:
        errs = json.loads(text).get("errors", [])
        msgs = [re.sub(r"<[^>]+>", "", e.get("detail", "")).strip() for e in errs]
        return "\n".join(m for m in msgs if m)
    except Exception:
        return text[:800]


def cmd_login():
    email, password = CFG["SKEDDA_EMAIL"], CFG["SKEDDA_PASSWORD"]
    s = new_session()
    print("logging in...", file=sys.stderr)
    login(s, email, password)
    ctx = get_context(s)
    if not ctx["write_token"]:
        raise SystemExit("login succeeded but no write token was obtained")
    save_session(s, ctx)
    print(f"session saved to {SESSION_FILE} "
          f"(venue={ctx['venue_id']} user={ctx['venueuser_id']} tz={ctx['timezone']})")


def cmd_list_spaces():
    _, ctx, saved_at = load_session()
    print(f"# session from {saved_at}", file=sys.stderr)
    for sp in ctx["spaces"]:
        print(f"{sp['id']}\t{sp['name']}")


def cmd_week():
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    print(f"# target week (Manila now: {datetime.now(MANILA):%Y-%m-%d %H:%M:%S %Z})",
          file=sys.stderr)
    for name, d in zip(days, following_week()):
        print(f"{name}\t{d.isoformat()}")


def cmd_book(args):
    s, ctx, saved_at = load_session()
    print(f"# using session from {saved_at}", file=sys.stderr)
    if args.at:
        wait_until(args.at)
    r = book(s, ctx, args.space, args.start, args.end, args.title)
    print(f"HTTP {r.status_code}")
    if r.status_code in (200, 201):
        print("booking confirmed")
        return 0
    if r.status_code in (401, 403):
        print("session expired or invalid — run: python booker.py login", file=sys.stderr)
    print(_extract_error(r.text))
    return 1


DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
COURT_NAMES = {COURT_1: "Court 1", COURT_2: "Court 2"}


def _fmt_slot(attempt):
    d, hour, space = attempt
    return (f"{DAY_NAMES[d.weekday()]} {d.isoformat()} "
            f"{hour:02d}:00 {COURT_NAMES.get(space, space)}")


def cmd_run(args):
    """Race the weekly release: wait until the start time, then run the interleaved
    loop over the three preferred targets until all resolve or the deadline passes."""
    s, ctx, saved_at = load_session()
    print(f"# using session from {saved_at}", file=sys.stderr)
    monday = following_week()[0]
    sunday = monday + timedelta(days=6)
    targets = build_week_targets(monday)
    print(f"# target week {monday.isoformat()} (Mon) .. {sunday.isoformat()} (Sun)",
          file=sys.stderr)

    if args.dry_run:
        for name, attempts in targets:
            print(f"Target {name}: {len(attempts)} attempts; first 6:")
            for a in attempts[:6]:
                print(f"  {_fmt_slot(a)}")
        return 0

    def attempt_fn(d, hour, space):
        start = f"{d.isoformat()}T{hour:02d}:00:00"
        end = f"{d.isoformat()}T{hour + 1:02d}:00:00"
        r = book(s, ctx, space, start, end)
        return r.status_code, r.text

    h, m, sec = (int(x) for x in args.until.split(":"))
    deadline = datetime.now(MANILA).replace(hour=h, minute=m, second=sec, microsecond=0)

    wait_until(args.start)   # block until exactly the start time (Manila), e.g. 09:00:00
    print(f"# firing at {datetime.now(MANILA):%H:%M:%S.%f} Manila; deadline {args.until}",
          file=sys.stderr)
    results = run_targets(targets, attempt_fn,
                          is_expired=lambda: datetime.now(MANILA) >= deadline,
                          tick_seconds=args.tick)

    booked = 0
    for r in results:
        if r["booked"]:
            booked += 1
            print(f"[{r['name']}] BOOKED  {_fmt_slot(r['booked'])}")
        else:
            print(f"[{r['name']}] none    ({r['reason']})")
    print(f"# {booked}/{len(results)} booked")
    return 0 if booked else 1


def main():
    ap = argparse.ArgumentParser(description="Skedda booker POC")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="authenticate and persist session to disk")
    sub.add_parser("list-spaces", help="list spaces from the saved session")
    sub.add_parser("week", help="print the target Mon-Sun dates (Manila) for the coming week")
    b = sub.add_parser("book", help="book a slot using the saved session")
    b.add_argument("--space", required=True)
    b.add_argument("--start", required=True, help="local ISO e.g. 2026-08-15T09:00:00")
    b.add_argument("--end", required=True, help="local ISO e.g. 2026-08-15T10:00:00")
    b.add_argument("--title", default=None)
    b.add_argument("--at", default=None,
                   help="wait until this Manila time (HH:MM:SS) before firing, e.g. 09:00:00")
    rn = sub.add_parser("run", help="race the weekly release and book the 3 preferred slots")
    rn.add_argument("--start", default="09:00:00",
                    help="Manila time to begin attempts (default 09:00:00)")
    rn.add_argument("--until", default="09:05:00",
                    help="Manila deadline to stop trying (default 09:05:00)")
    rn.add_argument("--tick", type=float, default=2.0,
                    help="seconds to pace between ticks (default 2)")
    rn.add_argument("--dry-run", action="store_true",
                    help="print the plan and exit without booking")
    args = ap.parse_args()

    if args.cmd == "login":
        cmd_login()
    elif args.cmd == "list-spaces":
        cmd_list_spaces()
    elif args.cmd == "week":
        cmd_week()
    elif args.cmd == "book":
        sys.exit(cmd_book(args))
    elif args.cmd == "run":
        sys.exit(cmd_run(args))


if __name__ == "__main__":
    main()
