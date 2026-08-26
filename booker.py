#!/usr/bin/env python3
"""Skedda booker.

Logs into a private Skedda venue and submits bookings via the JSON API (no browser).
Login is decoupled from booking: `login` authenticates every configured account
(SKEDDA_EMAIL_1/SKEDDA_PASSWORD_1, _2, _3, ... in .env, sharing one SKEDDA_VENUE) and
persists all their sessions (auth cookies + antiforgery token + venue context) to disk;
`book`, `list-spaces`, and `run` load those saved sessions and skip the login round-trips.

`run` fires WEEKLY_PLAN — an explicit sequence of (weekday, hour, account) slots — as an
interleaved race at the weekly release, dispatching each slot's booking to its assigned
account (each account capped at Skedda's 3-bookings-per-week quota).

Discovered contract for Kerry Sports Manila (ksmbooking):
  login:  GET  app.skedda.com/account/login         -> antiforgery cookie + token
          POST app.skedda.com/logins         (JSON)  -> validate creds, set auth cookie
          POST app.skedda.com/account/applogin (form) -> finalize auth cookies (302)
  data:   GET  ksmbooking.skedda.com/webs            -> spaces, venue id, user id, token
  write:  POST ksmbooking.skedda.com/bookings (JSON, X-Skedda-RequestVerificationToken)

Usage:
  python booker.py login                    # authenticate all accounts -> .session.json
  python booker.py list-spaces [--account N]  # uses a saved account session (default 1)
  python booker.py run --dry-run            # print the multi-account plan and exit
  python booker.py run                       # race the release and book the planned slots
  python booker.py book --account 1 --space 1399963 \
      --start 2026-08-15T09:00:00 --end 2026-08-15T10:00:00
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


def load_accounts(cfg=CFG):
    """Read numbered account credentials from config into an ordered list.

    Looks for SKEDDA_EMAIL_1/SKEDDA_PASSWORD_1, _2, _3, ... and stops at the first
    missing index. The venue (SKEDDA_VENUE) is shared across all accounts. Returns
    [{"account": 1, "email": ..., "password": ...}, ...].
    """
    accounts = []
    i = 1
    while True:
        email = cfg.get(f"SKEDDA_EMAIL_{i}")
        password = cfg.get(f"SKEDDA_PASSWORD_{i}")
        if not email or not password:
            break
        accounts.append({"account": i, "email": email, "password": password})
        i += 1
    return accounts


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


def save_sessions(entries, path=SESSION_FILE):
    """Persist one or more authenticated accounts to disk under a single file.

    `entries` is a list of (account, email, session, ctx). The file contains live
    auth cookies, so it is written with 0600 permissions and must stay git-ignored.
    """
    data = {
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "accounts": [{
            "account": account,
            "email": email,
            "cookies": [{"name": c.name, "value": c.value,
                         "domain": c.domain, "path": c.path} for c in s.cookies],
            "write_token": ctx["write_token"],
            "venue_id": ctx["venue_id"],
            "venueuser_id": ctx["venueuser_id"],
            "timezone": ctx.get("timezone"),
            "spaces": ctx.get("spaces", []),
        } for account, email, s, ctx in entries],
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)


def load_sessions(path=SESSION_FILE):
    """Rebuild per-account (session, ctx) from a saved session file.

    Returns (sessions, saved_at) where sessions maps account -> (email, session, ctx).
    Exits with guidance if no session exists yet. Does NOT contact the network.
    """
    if not os.path.exists(path):
        raise SystemExit(f"No saved session at {path}. Run: python booker.py login")
    with open(path) as f:
        data = json.load(f)
    sessions = {}
    for a in data.get("accounts", []):
        s = new_session()
        for c in a.get("cookies", []):
            s.cookies.set(c["name"], c["value"],
                          domain=c.get("domain"), path=c.get("path", "/"))
        ctx = {k: a.get(k) for k in
               ("write_token", "venue_id", "venueuser_id", "timezone", "spaces")}
        sessions[a["account"]] = (a.get("email"), s, ctx)
    return sessions, data.get("saved_at")


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


# The weekly booking plan: an explicit, ordered sequence of (weekday, hour, account)
# assignments. weekday uses date.weekday() (Mon=0 .. Sun=6); account is 1-based into
# the accounts list from load_accounts(). Each account may hold at most 3 bookings/week
# (Skedda quota), so no account appears more than three times below.
WEEKLY_PLAN = [
    (0, 17, 2),   # Mon 17:00 — Account 2
    (1, 10, 1),   # Tue 10:00 — Account 1
    (1, 17, 1),   # Tue 17:00 — Account 1
    (2, 10, 1),   # Wed 10:00 — Account 1
    (4, 16, 2),   # Fri 16:00 — Account 2
    (5, 10, 3),   # Sat 10:00 — Account 3
    (5, 17, 2),   # Sat 17:00 — Account 2
    (6, 10, 3),   # Sun 10:00 — Account 3
]


def build_week_targets(monday):
    """Expand WEEKLY_PLAN into (name, account, attempts) targets for the target week.

    Each plan entry becomes one target owning a single (day, hour) slot; attempts are
    the two courts tried in order (Court 1 then Court 2). The account travels with the
    target so the run loop dispatches its bookings to the right session. Distinct slots
    are guaranteed by the plan, so there is no cross-target day exclusion — one account
    can legitimately hold two slots on the same day.
    """
    return [(f"{DAY_NAMES[wd]} {hour:02d}:00", account,
             build_attempts([(wd, hour)], monday))
            for wd, hour, account in WEEKLY_PLAN]


def run_targets(targets, attempt_fn, is_expired, sleep_fn=time.sleep, tick_seconds=2):
    """Interleaved booking loop — concurrent in effect, sequential in execution.

    `targets` is a list of (name, account, attempts); attempts is an ordered list of
    (date, hour, space_id) from build_attempts(). Each tick fires ONE booking per
    unresolved target (in list order) at its current-best candidate via
    attempt_fn(account, date, hour, space_id), then paces `tick_seconds` before the
    next tick. Slots are pre-assigned distinct by the plan, so no cross-target day
    exclusion is needed — one account may hold two slots on the same day.

    Outcomes (see classify): booked -> done; collision -> advance to the next court;
    quota -> stop this target; auth -> abort; retry (incl. the not-yet-released case)
    -> stay on the same candidate and try again next tick. Runs until every target is
    resolved or is_expired() (the deadline) fires.

    Returns a list of {name, account, booked, reason} — reason in
    {booked, exhausted, quota, expired}.
    """
    n = len(targets)
    idx = [0] * n
    done = [False] * n
    booked = [None] * n
    reason = [None] * n

    while not all(done):
        if is_expired():
            for i in range(n):
                if not done[i]:
                    done[i], reason[i] = True, "expired"
            break
        for i, (name, account, attempts) in enumerate(targets):
            if done[i]:
                continue
            if idx[i] >= len(attempts):
                done[i], reason[i] = True, "exhausted"
                continue
            d, hour, space = attempts[idx[i]]
            outcome = classify(*attempt_fn(account, d, hour, space))
            if outcome == "booked":
                booked[i], done[i], reason[i] = attempts[idx[i]], True, "booked"
            elif outcome == "collision":
                idx[i] += 1
            elif outcome == "quota":
                done[i], reason[i] = True, "quota"
            elif outcome == "auth":
                raise RuntimeError("session expired mid-run — re-login and retry")
            # else: retry -> leave idx[i] in place, try again next tick
        if not all(done):
            sleep_fn(tick_seconds)

    return [{"name": targets[i][0], "account": targets[i][1],
             "booked": booked[i], "reason": reason[i]}
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
    accounts = load_accounts()
    if not accounts:
        raise SystemExit("no accounts configured — set SKEDDA_EMAIL_1/SKEDDA_PASSWORD_1 "
                         "(and _2, _3, ...) in .env")
    entries = []
    for a in accounts:
        print(f"logging in account {a['account']} ({a['email']})...", file=sys.stderr)
        s = new_session()
        login(s, a["email"], a["password"])
        ctx = get_context(s)
        if not ctx["write_token"]:
            raise SystemExit(f"account {a['account']} login succeeded but no write "
                             f"token was obtained")
        entries.append((a["account"], a["email"], s, ctx))
    save_sessions(entries)
    print(f"{len(entries)} session(s) saved to {SESSION_FILE}:")
    for account, email, _, ctx in entries:
        print(f"  account {account} ({email}): venue={ctx['venue_id']} "
              f"user={ctx['venueuser_id']} tz={ctx['timezone']}")


def cmd_list_spaces(args):
    sessions, saved_at = load_sessions()
    print(f"# session from {saved_at}", file=sys.stderr)
    if args.account not in sessions:
        raise SystemExit(f"account {args.account} not in saved session "
                         f"(have: {sorted(sessions)})")
    _, _, ctx = sessions[args.account]
    for sp in ctx["spaces"]:
        print(f"{sp['id']}\t{sp['name']}")


def cmd_week():
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    print(f"# target week (Manila now: {datetime.now(MANILA):%Y-%m-%d %H:%M:%S %Z})",
          file=sys.stderr)
    for name, d in zip(days, following_week()):
        print(f"{name}\t{d.isoformat()}")


def cmd_book(args):
    sessions, saved_at = load_sessions()
    print(f"# using session from {saved_at} (account {args.account})", file=sys.stderr)
    if args.account not in sessions:
        raise SystemExit(f"account {args.account} not in saved session "
                         f"(have: {sorted(sessions)})")
    _, s, ctx = sessions[args.account]
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
    loop over every planned target (across all accounts) until all resolve or the
    deadline passes. Each target's bookings go to its assigned account's session."""
    sessions, saved_at = load_sessions()
    print(f"# using session from {saved_at} ({len(sessions)} account(s))", file=sys.stderr)
    monday = following_week()[0]
    sunday = monday + timedelta(days=6)
    targets = build_week_targets(monday)
    print(f"# target week {monday.isoformat()} (Mon) .. {sunday.isoformat()} (Sun)",
          file=sys.stderr)

    if args.dry_run:
        for name, account, attempts in targets:
            print(f"[A{account}] {name}: "
                  f"{', '.join(COURT_NAMES.get(a[2], a[2]) for a in attempts)}")
        return 0

    needed = {account for _, account, _ in targets}
    missing = needed - set(sessions)
    if missing:
        raise SystemExit(f"plan needs account(s) {sorted(missing)} but the saved "
                         f"session only has {sorted(sessions)}. Run: python booker.py login")

    def attempt_fn(account, d, hour, space):
        _, s, ctx = sessions[account]
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
            print(f"[A{r['account']}] BOOKED  {_fmt_slot(r['booked'])}")
        else:
            print(f"[A{r['account']}] none    {r['name']} ({r['reason']})")
    print(f"# {booked}/{len(results)} booked")
    return 0 if booked else 1


def main():
    ap = argparse.ArgumentParser(description="Skedda booker POC")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="authenticate all configured accounts and persist to disk")
    ls = sub.add_parser("list-spaces", help="list spaces from a saved account session")
    ls.add_argument("--account", type=int, default=1,
                    help="which saved account to read (default 1)")
    sub.add_parser("week", help="print the target Mon-Sun dates (Manila) for the coming week")
    b = sub.add_parser("book", help="book a slot using a saved account session")
    b.add_argument("--space", required=True)
    b.add_argument("--start", required=True, help="local ISO e.g. 2026-08-15T09:00:00")
    b.add_argument("--end", required=True, help="local ISO e.g. 2026-08-15T10:00:00")
    b.add_argument("--title", default=None)
    b.add_argument("--account", type=int, default=1,
                   help="which saved account to book with (default 1)")
    b.add_argument("--at", default=None,
                   help="wait until this Manila time (HH:MM:SS) before firing, e.g. 09:00:00")
    rn = sub.add_parser("run", help="race the weekly release and book the planned slots "
                                    "across all accounts")
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
        cmd_list_spaces(args)
    elif args.cmd == "week":
        cmd_week()
    elif args.cmd == "book":
        sys.exit(cmd_book(args))
    elif args.cmd == "run":
        sys.exit(cmd_run(args))


if __name__ == "__main__":
    main()
