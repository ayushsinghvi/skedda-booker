"""Discovery step 2: capture /webs config (space IDs) and the booking-create payload.

Logs in, saves the /webs response, then opens a booking dialog and INTERCEPTS +
ABORTS the create request so we learn its URL/payload WITHOUT creating a real booking.
"""
import json
from dotenv import dotenv_values
from playwright.sync_api import sync_playwright

cfg = dotenv_values(".env")
EMAIL = cfg["SKEDDA_EMAIL"]
PASSWORD = cfg["SKEDDA_PASSWORD"]
BOOKING_URL = "https://ksmbooking.skedda.com/booking?viewdate=2026-07-26&viewtype=0"

webs_body = {}
create_attempts = []


def on_response(resp):
    if resp.url.rstrip("/").endswith("ksmbooking.skedda.com/webs"):
        try:
            webs_body["data"] = resp.json()
        except Exception as e:
            webs_body["error"] = str(e)


def handle_route(route):
    req = route.request
    # Abort any booking-create write so nothing is persisted, but record it.
    if req.method in ("POST", "PUT", "PATCH") and (
        "/bookings" in req.url or "/webs" in req.url and req.method != "GET"
    ):
        create_attempts.append({
            "method": req.method, "url": req.url,
            "post_data": req.post_data,
            "headers": {k: v for k, v in req.headers.items()
                        if k.lower() in ("content-type", "x-skedda-requestverificationtoken",
                                         "requestverificationtoken", "x-requested-with")},
        })
        route.abort()
    else:
        route.continue_()


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context()
    page = ctx.new_page()
    page.on("response", on_response)

    page.goto(BOOKING_URL, wait_until="networkidle")
    page.fill('input[type="email"]', EMAIL)
    page.fill('input[type="password"]', PASSWORD)
    page.click('button:has-text("Log in")')
    page.wait_for_url("**/booking**", timeout=30000)
    page.wait_for_load_state("networkidle")

    with open("webs_config.json", "w") as f:
        json.dump(webs_body.get("data", webs_body), f, indent=2)
    print("saved webs_config.json (keys:",
          list(webs_body.get("data", {}).keys()) if "data" in webs_body else webs_body, ")")

    # Now arm interception and try to create a booking via the UI.
    page.route("**/*", handle_route)

    # Dismiss the "Help make Skedda better" popup if present (decline data collection).
    try:
        page.click('button:has-text("Decline")', timeout=3000)
    except Exception:
        pass

    # Click an empty slot on Tennis Court 1 (e.g. 2:00 PM row) to open the booking form.
    # Coordinates approximate the grid cell for Court 1 @ ~2 PM.
    clicked = False
    for label in ["2:00 PM–3:00 PM", "3:00 PM–4:00 PM", "4:00 PM–5:00 PM"]:
        try:
            page.locator(f"text={label}").first.click(timeout=3000)
            clicked = True
            print("opened slot:", label)
            break
        except Exception as e:
            print("slot click failed:", label, str(e)[:80])
    page.wait_for_timeout(1500)
    page.screenshot(path="discover_booking_form.png")

    # Try to find and click a confirm/save button in the booking dialog.
    for btn in ["Confirm", "Save", "Book", "Create booking", "Make booking"]:
        try:
            page.locator(f"button:has-text('{btn}')").first.click(timeout=2500)
            print("clicked confirm button:", btn)
            break
        except Exception:
            pass
    page.wait_for_timeout(2000)
    page.screenshot(path="discover_booking_after_confirm.png")

    with open("create_attempts.json", "w") as f:
        json.dump(create_attempts, f, indent=2)
    print(f"captured {len(create_attempts)} write attempts -> create_attempts.json")

    browser.close()
