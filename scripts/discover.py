"""Discovery harness: log in with .env creds and capture all Skedda network traffic.

Read-only: captures GET/POST endpoints the app uses. Does NOT create a booking.
Writes captured requests to discover_capture.json and a screenshot to discover_after_login.png.
"""
import json
import sys
from dotenv import dotenv_values
from playwright.sync_api import sync_playwright

cfg = dotenv_values(".env")
EMAIL = cfg["SKEDDA_EMAIL"]
PASSWORD = cfg["SKEDDA_PASSWORD"]
BOOKING_URL = "https://ksmbooking.skedda.com/booking?viewdate=2026-07-26&viewtype=0"

captured = []


def on_request(req):
    if "skedda.com" not in req.url:
        return
    # Only record API-ish calls (skip static assets / analytics)
    if any(x in req.url for x in ["cdn.skedda.com", "/assets/", ".js", ".css",
                                  ".png", ".woff", "amplitude", "sentry"]):
        return
    post = None
    try:
        post = req.post_data
    except Exception:
        pass
    captured.append({
        "method": req.method,
        "url": req.url,
        "post_data": post,
        "resource_type": req.resource_type,
    })


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context()
    page = ctx.new_page()
    page.on("request", on_request)

    print("navigating to booking url (will redirect to login)...")
    page.goto(BOOKING_URL, wait_until="networkidle")

    print("filling login form...")
    page.fill('input[type="email"]', EMAIL)
    page.fill('input[type="password"]', PASSWORD)
    page.click('button:has-text("Log in")')

    print("waiting for post-login navigation...")
    try:
        page.wait_for_url("**/booking**", timeout=30000)
    except Exception as e:
        print("wait_for_url note:", e)
    page.wait_for_load_state("networkidle")

    page.screenshot(path="discover_after_login.png", full_page=False)
    print("final url:", page.url)

    with open("discover_capture.json", "w") as f:
        json.dump(captured, f, indent=2)
    print(f"captured {len(captured)} api requests -> discover_capture.json")

    browser.close()
