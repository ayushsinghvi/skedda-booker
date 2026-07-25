# scripts/

One-off harnesses used to **reverse-engineer** Skedda's login and booking API
while building `booker.py`. They are not part of the tool and are kept only for
reference (e.g. if Skedda changes its API and the flow needs re-discovery).

They were run from the repo root (they read `.env` and, in some cases,
`import booker`), and use Playwright to capture the network traffic.

| Script | What it discovered |
|--------|--------------------|
| `probe_login.py` | Login endpoint probing (`/logins`, `/account/applogin`) |
| `discover.py`  | Full login network capture |
| `discover2.py` | `/webs` config (space IDs, venue/user) |
| `discover3.py` | New-booking dialog structure |
| `discover4.py` | Booking-create payload (`POST /bookings`) via intercept+abort |

Requires the dev extras: `pip install playwright && playwright install chromium`.
