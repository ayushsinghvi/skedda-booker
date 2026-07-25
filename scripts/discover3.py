"""Discovery step 3: open the new-booking dialog via the '+' FAB and capture the
create request payload by intercepting + aborting it (no real booking made)."""
import json
from dotenv import dotenv_values
from playwright.sync_api import sync_playwright

cfg = dotenv_values(".env")
EMAIL = cfg["SKEDDA_EMAIL"]
PASSWORD = cfg["SKEDDA_PASSWORD"]
BOOKING_URL = "https://ksmbooking.skedda.com/booking?viewdate=2026-07-26&viewtype=0"

create_attempts = []


def handle_route(route):
    req = route.request
    if req.method in ("POST", "PUT", "PATCH") and "skedda.com" in req.url \
            and "amplitude" not in req.url and "sentry" not in req.url \
            and "/logins" not in req.url and "applogin" not in req.url:
        create_attempts.append({
            "method": req.method, "url": req.url, "post_data": req.post_data,
            "headers": {k: v for k, v in req.headers.items()},
        })
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

    # Click the green '+' floating action button (bottom-right).
    page.mouse.click(1230, 670)
    page.wait_for_timeout(2000)
    page.screenshot(path="discover_new_dialog.png")

    # Dump interactive elements in the dialog so we know what to fill.
    els = page.eval_on_selector_all(
        "button, input, select, [role=button], [role=combobox]",
        """els => els.slice(0,80).map(e => ({
            tag: e.tagName, type: e.type||null, role: e.getAttribute('role'),
            text: (e.innerText||e.value||'').trim().slice(0,40),
            ph: e.getAttribute('placeholder'), name: e.getAttribute('name'),
            testid: e.getAttribute('data-test-id')||e.getAttribute('data-testid')
        }))"""
    )
    with open("new_dialog_elements.json", "w") as f:
        json.dump(els, f, indent=2)
    print("dialog elements ->", len(els))
    for e in els:
        if e["text"] or e["ph"] or e["testid"]:
            print(" ", {k: v for k, v in e.items() if v})

    browser.close()
