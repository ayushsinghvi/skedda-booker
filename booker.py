#!/usr/bin/env python3
"""Skedda booker — POC.

Logs into a private Skedda venue and submits a single booking via the JSON API
(no browser). Discovered contract for Kerry Sports Manila (ksmbooking):

  login:  GET  app.skedda.com/account/login        -> antiforgery cookie + token
          POST app.skedda.com/logins        (JSON)  -> validate credentials
          POST app.skedda.com/account/applogin (form) -> sets auth cookies (302)
  data:   GET  ksmbooking.skedda.com/webs           -> spaces, venue id, user id
  write:  POST ksmbooking.skedda.com/bookings (JSON, X-Skedda-RequestVerificationToken)

Usage:
  python booker.py list-spaces
  python booker.py book --space 1399963 --start 2026-08-15T09:00:00 --end 2026-08-15T10:00:00
"""
import argparse
import re
import sys
import requests
from dotenv import dotenv_values

CFG = dotenv_values(".env")
VENUE_SUB = CFG.get("SKEDDA_VENUE", "ksmbooking")
APP = "https://app.skedda.com"
VENUE = f"https://{VENUE_SUB}.skedda.com"
BOOKING_PAGE = f"{VENUE}/booking"
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


def main():
    ap = argparse.ArgumentParser(description="Skedda booker POC")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-spaces")
    b = sub.add_parser("book")
    b.add_argument("--space", required=True)
    b.add_argument("--start", required=True, help="local ISO e.g. 2026-08-15T09:00:00")
    b.add_argument("--end", required=True, help="local ISO e.g. 2026-08-15T10:00:00")
    b.add_argument("--title", default=None)
    args = ap.parse_args()

    email, password = CFG["SKEDDA_EMAIL"], CFG["SKEDDA_PASSWORD"]
    s = new_session()
    print("logging in...", file=sys.stderr)
    login(s, email, password)
    ctx = get_context(s)
    print(f"authed. venue={ctx['venue_id']} user={ctx['venueuser_id']} "
          f"tz={ctx['timezone']} token={'yes' if ctx['write_token'] else 'NO'}",
          file=sys.stderr)

    if args.cmd == "list-spaces":
        for sp in ctx["spaces"]:
            print(f"{sp['id']}\t{sp['name']}")
    elif args.cmd == "book":
        r = book(s, ctx, args.space, args.start, args.end, args.title)
        print(f"HTTP {r.status_code}")
        print(r.text[:800])
        sys.exit(0 if r.status_code in (200, 201) else 1)


if __name__ == "__main__":
    main()
