"""Optional Playwright-based session-cookie harvester for TrueNorth.

Stopgap until TN ships an official API key: if TN_SESSION_COOKIES is set
(JSON array in the "Cookie-Editor" / Chrome DevTools export format), launch
headless Chromium, restore the cookies, open app.true-north.xyz, confirm the
session is authenticated, and yank the current Privy access/refresh tokens
out of localStorage. We also listen for any POST to /sse/v2/streams and
intercept its `thread_id` so the caller can refresh the shared thread too.

Design constraints:
  - Playwright is an optional dependency. Importing this module MUST NOT fail
    the bot process if playwright isn't installed. All entry points degrade
    to no-ops with a clear log message.
  - Never store passwords or drive Google OAuth here. Cookie-based restoration
    only.
  - Every external interaction runs with short timeouts so a broken harvest
    never blocks the main bot loop.
  - Chromium needs ~180-220 MB RAM at launch. Run on a Railway instance with
    at least 512 MB available.
"""
from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

TN_URL = "https://app.true-north.xyz"
STREAMS_URL_FRAGMENT = "/sse/v2/streams"
_UUID_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)

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


def _extract_thread_id_from_post_body(body_raw: Any) -> str | None:
    """Pull a UUID `thread_id` out of a POST body (str or dict). None if absent."""
    if body_raw is None:
        return None
    data: Any = body_raw
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return None
    if not isinstance(data, dict):
        return None
    tid = data.get("thread_id")
    if isinstance(tid, str) and _UUID_RE.match(tid.strip()):
        return tid.strip()
    return None


async def harvest_session(raw_cookies: str, apply_creds: Callable[[dict], Awaitable[None]]) -> dict:
    """Run one harvest attempt.

    Returns a status dict with keys:
      * ``ok`` (bool)         — True iff tokens were successfully applied
      * ``reason`` (str)      — failure reason, empty on success
      * ``applied`` (bool)    — True iff apply_creds was called
      * ``cookies_expired`` (bool) — True iff Chromium was redirected to
                                     login (session cookies are dead)
      * ``thread_id`` (str|None)   — the intercepted thread_id if captured
    """
    status: dict[str, Any] = {
        "ok": False, "reason": "", "applied": False,
        "cookies_expired": False, "thread_id": None,
    }
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
    captured_thread_id: list[str] = []  # mutable cell so the event handler can append

    def _on_request(request: Any) -> None:
        if captured_thread_id:
            return
        try:
            if STREAMS_URL_FRAGMENT not in request.url:
                return
            if request.method.upper() != "POST":
                return
            # Prefer post_data_json when Playwright exposes it.
            body = getattr(request, "post_data_json", None)
            if body is None:
                body = getattr(request, "post_data", None)
            tid = _extract_thread_id_from_post_body(body)
            if tid:
                captured_thread_id.append(tid)
                print(f"[Harvester] captured thread_id from {request.url}: {tid}")
        except Exception as e:  # pragma: no cover — defensive
            print(f"[Harvester] request handler error: {type(e).__name__}: {e}")

    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.add_cookies(cookies)
        page = await context.new_page()
        page.on("request", _on_request)
        resp = await page.goto(TN_URL, timeout=20_000, wait_until="domcontentloaded")
        final_url = page.url
        if "login" in final_url or (resp is not None and resp.status >= 400):
            status["cookies_expired"] = True
            status["reason"] = f"session invalid: redirected to {final_url} (status={resp.status if resp else '?'})"
            return status
        # Give the app a moment to populate localStorage and issue its
        # first /streams request (it usually fetches the user's current
        # thread on mount). We wait longer than the original 3 s so the
        # request interceptor has a real chance to fire.
        await page.wait_for_timeout(8_000)
        access = await page.evaluate("() => window.localStorage.getItem('privy:token')")
        refresh = await page.evaluate("() => window.localStorage.getItem('privy:refresh_token')")
        if access:
            access = access.strip('"')
        if refresh:
            refresh = refresh.strip('"')
        if not access or not refresh:
            status["cookies_expired"] = True
            status["reason"] = "cookies loaded but privy:token / privy:refresh_token not found in localStorage"
            return status
        # Don't log the tokens themselves — just their lengths for sanity.
        print(f"[Harvester] extracted tokens (access_len={len(access)}, refresh_len={len(refresh)})")
        update: dict[str, str] = {"access_token": access, "refresh_token": refresh}
        if captured_thread_id:
            update["thread_id"] = captured_thread_id[0]
            status["thread_id"] = captured_thread_id[0]
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
