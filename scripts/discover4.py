"""Discovery step 4: fill the new-booking dialog, click Confirm, and capture the
create request (intercept + abort, so nothing is persisted)."""
import json
from dotenv import dotenv_values
from playwright.sync_api import sync_playwright

cfg = dotenv_values(".env")
EMAIL, PASSWORD = cfg["SKEDDA_EMAIL"], cfg["SKEDDA_PASSWORD"]
BOOKING_URL = "https://ksmbooking.skedda.com/booking?viewdate=2026-07-26&viewtype=0"

create_attempts = []


def handle_route(route):
    req = route.request
    if req.method in ("POST", "PUT", "PATCH") and "skedda.com" in req.url \
            and not any(x in req.url for x in ("amplitude", "sentry", "/logins", "applogin")):
        create_attempts.append({"method": req.method, "url": req.url,
                                "post_data": req.post_data, "headers": dict(req.headers)})
        route.abort()
    else:
        route.continue_()


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_context().new_page()
    page.goto(BOOKING_URL, wait_until="networkidle")
    page.fill('input[type="email"]', EMAIL)
    page.fill('input[type="password"]', PASSWORD)
    page.click('button:has-text("Log in")')
    page.wait_for_url("**/booking**", timeout=30000)
    page.wait_for_load_state("networkidle")
    try:
        page.click('button:has-text("Decline")', timeout=3000)
    except Exception:
        pass

    page.mouse.click(1230, 670)          # green '+' FAB
    page.wait_for_timeout(1500)

    # Select a space: open the Spaces dropdown and pick Tennis Court 1.
    page.click("text=No spaces selected")
    page.wait_for_timeout(800)
    page.screenshot(path="discover_spaces_dropdown.png")
    try:
        page.locator("text=Tennis Court 1").last.click(timeout=3000)
    except Exception as e:
        print("space select note:", str(e)[:100])
    page.wait_for_timeout(500)
    # Close dropdown if it stays open
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    page.route("**/*", handle_route)     # arm interception AFTER setup GETs
    page.screenshot(path="discover_before_confirm.png")
    try:
        page.click("button:has-text('Confirm booking')", timeout=4000)
    except Exception as e:
        print("confirm note:", str(e)[:120])
    page.wait_for_timeout(2500)
    page.screenshot(path="discover_after_confirm2.png")

    with open("create_attempts.json", "w") as f:
        json.dump(create_attempts, f, indent=2)
    print(f"captured {len(create_attempts)} write attempts")
    for a in create_attempts:
        print(" ", a["method"], a["url"])
        print("   body:", (a["post_data"] or "")[:500])
    browser.close()
