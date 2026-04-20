"""Optional Playwright-based session-cookie harvester for TrueNorth.

Stopgap until TN ships an official API key: if TN_SESSION_COOKIES is set
(JSON array in the "Cookie-Editor" / Chrome DevTools export format), launch
headless Chromium, restore the cookies, open app.true-north.xyz, confirm the
session is authenticated, and yank the current Privy access/refresh tokens
out of localStorage. The caller plugs those into token_store.

Design constraints:
  - Playwright is an optional dependency. Importing this module MUST NOT fail
    the bot process if playwright isn't installed. All entry points degrade
    to no-ops with a clear log message.
  - Never store passwords or drive Google OAuth here. Cookie-based restoration
    only.
  - Every external interaction runs with short timeouts so a broken harvest
    never blocks the main bot loop.
"""
from __future__ import annotations

import json
import os
from typing import Any, Awaitable, Callable

TN_URL = "https://app.true-north.xyz"

try:
    from playwright.async_api import async_playwright  # type: ignore

    HAS_PLAYWRIGHT = True
except Exception:
    async_playwright = None  # type: ignore
    HAS_PLAYWRIGHT = False


def _parse_cookies(raw: str) -> list[dict] | None:
    """Parse the TN_SESSION_COOKIES env var. Returns normalised cookies or None on parse error."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception as e:
        print(f"[Harvester] TN_SESSION_COOKIES JSON parse failed: {type(e).__name__}: {e}")
        return None
    if not isinstance(data, list):
        print("[Harvester] TN_SESSION_COOKIES must be a JSON list of cookie objects")
        return None
    normalised: list[dict] = []
    for raw_cookie in data:
        if not isinstance(raw_cookie, dict):
            continue
        name = raw_cookie.get("name")
        value = raw_cookie.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        cookie: dict[str, Any] = {"name": name, "value": value}
        # Playwright expects either url OR domain+path. Prefer explicit domain.
        domain = raw_cookie.get("domain") or ".true-north.xyz"
        cookie["domain"] = domain
        cookie["path"] = raw_cookie.get("path") or "/"
        if "expirationDate" in raw_cookie:
            try:
                cookie["expires"] = int(raw_cookie["expirationDate"])
            except Exception:
                pass
        if raw_cookie.get("httpOnly"):
            cookie["httpOnly"] = True
        if raw_cookie.get("secure"):
            cookie["secure"] = True
        # sameSite values in CookieEditor are "no_restriction"/"lax"/"strict" — map.
        same_site = raw_cookie.get("sameSite")
        if isinstance(same_site, str):
            mapped = {"no_restriction": "None", "lax": "Lax", "strict": "Strict"}.get(same_site.lower())
            if mapped:
                cookie["sameSite"] = mapped
        normalised.append(cookie)
    return normalised or None


async def harvest_session(raw_cookies: str, apply_creds: Callable[[dict], Awaitable[None]]) -> dict:
    """Run one harvest attempt.

    Returns a status dict {ok, reason, applied}. Applies credentials only when
    the session is confirmed authenticated and the tokens were successfully
    extracted from localStorage.
    """
    status: dict[str, Any] = {"ok": False, "reason": "", "applied": False}
    if not HAS_PLAYWRIGHT:
        status["reason"] = "playwright not installed (pip install playwright && python -m playwright install chromium)"
        print(f"[Harvester] skip: {status['reason']}")
        return status
    cookies = _parse_cookies(raw_cookies)
    if not cookies:
        status["reason"] = "TN_SESSION_COOKIES missing or unparseable"
        return status

    pw = None
    browser = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.add_cookies(cookies)
        page = await context.new_page()
        resp = await page.goto(TN_URL, timeout=20_000, wait_until="domcontentloaded")
        final_url = page.url
        if "login" in final_url or (resp is not None and resp.status >= 400):
            status["reason"] = f"session invalid: redirected to {final_url} (status={resp.status if resp else '?'})"
            return status
        # Give the app a moment to populate localStorage.
        await page.wait_for_timeout(3_000)
        access = await page.evaluate("() => window.localStorage.getItem('privy:token')")
        refresh = await page.evaluate("() => window.localStorage.getItem('privy:refresh_token')")
        if access:
            access = access.strip('"')
        if refresh:
            refresh = refresh.strip('"')
        if not access or not refresh:
            status["reason"] = "cookies loaded but privy:token / privy:refresh_token not found in localStorage"
            return status
        # Don't log the tokens themselves — just their lengths for sanity.
        print(f"[Harvester] extracted tokens (access_len={len(access)}, refresh_len={len(refresh)})")
        # Attempt to read current thread_id from any captured SSE request payload.
        # This is best-effort — if the user doesn't trigger a chat in <20s we
        # fall back to the cached / env thread_id.
        thread_id = os.environ.get("TN_THREAD_ID", "") or None
        update: dict[str, str] = {"access_token": access, "refresh_token": refresh}
        if thread_id:
            update["thread_id"] = thread_id
        await apply_creds(update)
        status["applied"] = True
        status["ok"] = True
        return status
    except Exception as e:
        status["reason"] = f"{type(e).__name__}: {e}"
        print(f"[Harvester] error: {status['reason']}")
        return status
    finally:
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass
        try:
            if pw is not None:
                await pw.stop()
        except Exception:
            pass
