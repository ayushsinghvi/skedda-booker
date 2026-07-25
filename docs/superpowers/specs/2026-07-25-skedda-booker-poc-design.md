# Skedda Booker — POC Design

**Date:** 2026-07-25
**Status:** Approved (design), building

## Goal

A single command that logs into Skedda (Kerry Sports Manila, `ksmbooking.skedda.com`)
and submits **one** booking request, printing success or failure. This is a
proof-of-concept — scheduling and the "race to open slots" timing logic come later.

## Context / findings

- **Venue:** Kerry Sports Manila — a *private* venue. Bookings require a logged-in,
  approved account (`visitorBookingsEnabled: false`).
- **Auth:** ASP.NET-style anti-forgery flow. GET the login page → it sets an
  `X-Skedda-RequestVerificationCookie` cookie and embeds a hidden
  `__RequestVerificationToken`. POST both + email/password to `/account/login`
  to obtain the authenticated session cookies.
- **API:** Ember SPA over a JSON backend under `/webs/...` on the venue subdomain
  (`https://ksmbooking.skedda.com/webs/...`). Unauthenticated calls return 422.
  Bookings are created by POSTing to that API with the verification token as a header.

## Approach

**Direct HTTP API replay** in Python (`requests`), chosen over browser automation:

- Fast and precise (~100ms POST vs seconds for a browser) — matters for the later
  racing goal.
- Lightweight to schedule as a routine.
- Trade-off: more brittle to Skedda API changes; payload is reverse-engineered
  rather than clicked through the UI. Acceptable given the racing goal.

## Components (`booker.py`)

1. `login(session)` — GET login page, scrape `__RequestVerificationToken`,
   POST credentials, return an authenticated `requests.Session`.
2. `list_spaces(session)` — query `/webs` to discover space IDs and the exact
   booking payload shape.
3. `book(session, space_id, start, end)` — POST the booking with the anti-forgery
   header; return the parsed result.
4. CLI:
   - `python booker.py list-spaces`
   - `python booker.py book --space <id> --start <iso> --end <iso>`

Credentials are read from `.env` (git-ignored): `SKEDDA_EMAIL`, `SKEDDA_PASSWORD`,
`SKEDDA_VENUE`.

## Build & verification order

1. Build `login` + `list-spaces` first — both read-only. Confirm auth works and
   real space IDs are returned.
2. Only then wire up `book`. Confirm the exact target slot with the user before
   firing a real booking (it is a real side effect on the account).

## Out of scope (POC)

- Scheduling / cron / Claude Code routine.
- Racing / booking-window timing logic.
- Retries, notifications, error-recovery beyond basic reporting.

## Security

- Credentials live only in `.env`, which is git-ignored. Never committed or logged.
