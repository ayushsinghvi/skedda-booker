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
import requests
from dotenv import dotenv_values

CFG = dotenv_values(".env")
VENUE_SUB = CFG.get("SKEDDA_VENUE", "ksmbooking")
APP = "https://app.skedda.com"
VENUE = f"https://{VENUE_SUB}.skedda.com"
BOOKING_PAGE = f"{VENUE}/booking"
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


def cmd_book(args):
    s, ctx, saved_at = load_session()
    print(f"# using session from {saved_at}", file=sys.stderr)
    r = book(s, ctx, args.space, args.start, args.end, args.title)
    print(f"HTTP {r.status_code}")
    if r.status_code in (200, 201):
        print("booking confirmed")
        return 0
    if r.status_code in (401, 403):
        print("session expired or invalid — run: python booker.py login", file=sys.stderr)
    print(_extract_error(r.text))
    return 1


def main():
    ap = argparse.ArgumentParser(description="Skedda booker POC")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="authenticate and persist session to disk")
    sub.add_parser("list-spaces", help="list spaces from the saved session")
    b = sub.add_parser("book", help="book a slot using the saved session")
    b.add_argument("--space", required=True)
    b.add_argument("--start", required=True, help="local ISO e.g. 2026-08-15T09:00:00")
    b.add_argument("--end", required=True, help="local ISO e.g. 2026-08-15T10:00:00")
    b.add_argument("--title", default=None)
    args = ap.parse_args()

    if args.cmd == "login":
        cmd_login()
    elif args.cmd == "list-spaces":
        cmd_list_spaces()
    elif args.cmd == "book":
        sys.exit(cmd_book(args))


if __name__ == "__main__":
    main()
