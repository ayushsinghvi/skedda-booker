"""Throwaway probe to discover Skedda's login request shape."""
import re
import sys
import requests
from dotenv import dotenv_values

cfg = dotenv_values(".env")
EMAIL = cfg["SKEDDA_EMAIL"]
PASSWORD = cfg["SKEDDA_PASSWORD"]

s = requests.Session()
s.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
})

# 1. GET login page -> token + antiforgery cookie
r = s.get("https://app.skedda.com/account/login")
print("GET login:", r.status_code)
m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r.text)
token = m.group(1) if m else None
print("token found:", bool(token))
print("cookies:", list(s.cookies.keys()))

# 2. Try JSON POST to /account/login
payload = {"userName": EMAIL, "password": PASSWORD, "rememberMe": True}
headers = {
    "Content-Type": "application/json",
    "X-Skedda-RequestVerificationToken": token or "",
    "RequestVerificationToken": token or "",
    "Accept": "application/json, text/plain, */*",
}
r2 = s.post("https://app.skedda.com/account/login", json=payload, headers=headers,
            allow_redirects=False)
print("\nPOST /account/login (JSON):", r2.status_code)
print("resp headers location:", r2.headers.get("location"))
print("set-cookie present:", "set-cookie" in {k.lower() for k in r2.headers})
print("session cookies now:", list(s.cookies.keys()))
print("body (first 600):", r2.text[:600])
