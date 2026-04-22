#!/usr/bin/env python3
"""Mac-local harvester for dct-truenorth-bot.

Reads fresh Privy credentials + the current TrueNorth thread_id out of an
already-logged-in Chrome profile via the Chrome DevTools Protocol (CDP),
then POSTs them to the bot's /credentials webhook.

Design:
  * No Playwright, no Chromium download on Railway — Chrome runs on your
    Mac where you're already authenticated in a normal browser profile.
  * Chrome must be launched with --remote-debugging-port=9222. A helper
    command is documented in harvester_local/README.md.
  * Credentials never touch the repo / disk beyond the bot's atomic cache.
    The Mac side only keeps the webhook URL + shared secret in
    ~/.dct-harvester-config.

Install: `pip install pychrome requests` (or use harvester_local/setup.sh).

Usage:
  python3 harvester_local/harvester.py            # one-shot push
  python3 harvester_local/harvester.py --once     # same (default)
  python3 harvester_local/harvester.py --verify   # dry-run, don't POST

Cron (pushes every 8h, plenty of headroom vs. Privy's ~24h token TTL):
  0 */8 * * * /usr/bin/python3 /abs/path/to/harvester_local/harvester.py >> ~/.dct-harvester.log 2>&1
"""
from __future__ import annotations

import argparse
import configparser
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TN_URL_FRAGMENT = "app.true-north.xyz"
STREAMS_URL_FRAGMENT = "/sse/v2/streams"
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

CONFIG_PATH = Path.home() / ".dct-harvester-config"
LOG_PATH = Path.home() / ".dct-harvester.log"
THREAD_CACHE_PATH = Path.home() / ".dct-harvester-thread"
CDP_ENDPOINT = "http://localhost:9222"
STREAMS_WAIT_SECONDS = 30


def _configure_logging() -> None:
    """Write timestamped entries to both stderr and ~/.dct-harvester.log."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler(LOG_PATH, encoding="utf-8"))
    except OSError as e:
        print(f"[harvester] could not open {LOG_PATH}: {e}", file=sys.stderr)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


def load_config() -> dict[str, str]:
    """Read ~/.dct-harvester-config (INI section [harvester])."""
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"missing config file: {CONFIG_PATH}\n"
            "Run harvester_local/setup.sh first."
        )
    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH)
    if "harvester" not in parser:
        raise SystemExit(f"{CONFIG_PATH} is missing [harvester] section")
    section = parser["harvester"]
    bot_url = section.get("bot_url", "").strip().rstrip("/")
    secret = section.get("harvester_secret", "").strip()
    if not bot_url or not secret:
        raise SystemExit(
            f"{CONFIG_PATH} must define both bot_url and harvester_secret"
        )
    return {"bot_url": bot_url, "harvester_secret": secret}


# --- CDP helpers ---------------------------------------------------------

def _http_get_json(url: str, timeout: float = 5.0) -> Any:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def list_cdp_tabs() -> list[dict]:
    """Hit the CDP `/json` endpoint to list open tabs."""
    return _http_get_json(f"{CDP_ENDPOINT}/json")


def find_tn_tab(tabs: list[dict]) -> dict | None:
    """Pick the tab whose URL contains app.true-north.xyz (ignore DevTools itself)."""
    for tab in tabs:
        if tab.get("type") != "page":
            continue
        url = tab.get("url", "")
        if TN_URL_FRAGMENT in url:
            return tab
    return None


def read_localstorage_via_pychrome(tab: dict) -> tuple[str, str]:
    """Return (access_token, refresh_token) from the tab's localStorage."""
    try:
        import pychrome  # type: ignore
    except ImportError as e:
        raise SystemExit(
            f"pychrome not installed: {e}. Run `pip install pychrome` or "
            "harvester_local/setup.sh."
        )
    browser = pychrome.Browser(url=CDP_ENDPOINT)
    target_tab = None
    for t in browser.list_tab():
        if t.id == tab["id"]:
            target_tab = t
            break
    if target_tab is None:
        raise SystemExit(f"pychrome could not attach to tab {tab['id']}")
    target_tab.start()
    try:
        target_tab.Runtime.enable()
        access = target_tab.Runtime.evaluate(
            expression="window.localStorage.getItem('privy:token') || ''"
        )["result"].get("value", "")
        refresh = target_tab.Runtime.evaluate(
            expression="window.localStorage.getItem('privy:refresh_token') || ''"
        )["result"].get("value", "")
    finally:
        target_tab.stop()
    # Privy stores JSON-stringified values; strip outer quotes.
    return strip_json_quotes(access), strip_json_quotes(refresh)


def strip_json_quotes(value: str) -> str:
    v = (value or "").strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1]
    return v


def extract_thread_id_from_url(url: str) -> str | None:
    """Pull a UUID out of the tab URL (e.g. .../chat/<uuid>). Returns None if absent."""
    if not url:
        return None
    m = UUID_RE.search(url)
    return m.group(0) if m else None


def _read_thread_id_from_cache() -> str | None:
    """Read a previously-saved thread_id from ~/.dct-harvester-thread, if valid."""
    if not THREAD_CACHE_PATH.exists():
        return None
    try:
        cached = THREAD_CACHE_PATH.read_text().strip()
    except OSError:
        return None
    return cached if UUID_RE.fullmatch(cached) else None


def capture_thread_id_via_cdp(tab: dict, timeout_s: int = STREAMS_WAIT_SECONDS) -> str | None:
    """Enable the Network CDP domain and intercept the next /sse/v2/streams POST.

    This used to be the primary path but is now the fallback — we hit it only
    when the tab URL doesn't contain a thread UUID. Pychrome's internal
    _recv_loop has been observed to crash with JSONDecodeError on certain
    empty WebSocket frames; we wrap every call defensively so a failure here
    can never bring down the whole harvest.
    """
    try:
        import pychrome  # type: ignore
    except ImportError:
        logging.warning("pychrome not installed — skipping CDP-based capture")
        return None

    target_tab = None
    try:
        browser = pychrome.Browser(url=CDP_ENDPOINT)
        for t in browser.list_tab():
            if t.id == tab["id"]:
                target_tab = t
                break
    except Exception as e:
        logging.warning("CDP attach failed (%s: %s)", type(e).__name__, e)
        return None
    if target_tab is None:
        logging.warning("CDP could not find tab id=%s", tab.get("id"))
        return None

    captured: dict[str, str] = {}

    def _on_request_will_be_sent(**kwargs):
        if captured.get("tid"):
            return
        try:
            request = kwargs.get("request") or {}
            url = request.get("url", "")
            method = request.get("method", "")
            if STREAMS_URL_FRAGMENT not in url or method.upper() != "POST":
                return
            body = request.get("postData")
            if not body:
                return
            try:
                obj = json.loads(body)
            except Exception:
                return
            tid = obj.get("thread_id") if isinstance(obj, dict) else None
            if isinstance(tid, str) and UUID_RE.fullmatch(tid.strip()):
                captured["tid"] = tid.strip()
        except Exception as listener_err:
            logging.warning("CDP request listener error: %s", listener_err)

    try:
        target_tab.start()
    except Exception as e:
        logging.warning("CDP target_tab.start() failed (%s: %s)", type(e).__name__, e)
        return None

    try:
        try:
            target_tab.Network.enable()
            target_tab.set_listener("Network.requestWillBeSent", _on_request_will_be_sent)
        except Exception as e:
            logging.warning("CDP Network.enable() / set_listener failed (%s: %s)",
                            type(e).__name__, e)
            return None
        deadline = time.time() + timeout_s
        while time.time() < deadline and not captured.get("tid"):
            try:
                target_tab.wait(1)
            except json.JSONDecodeError as e:
                logging.warning("pychrome _recv_loop JSON decode error (ignored): %s", e)
                # The recv-loop runs on a daemon thread; we can't restart it,
                # but we can stop polling and surrender gracefully.
                break
            except Exception as e:
                logging.warning("pychrome wait() raised %s: %s — aborting capture",
                                type(e).__name__, e)
                break
    finally:
        try:
            target_tab.stop()
        except Exception:
            pass

    return captured.get("tid")


def resolve_thread_id(tab: dict) -> tuple[str | None, str]:
    """Resolve thread_id, preferring the cheapest source.

    Order:
      1. UUID embedded in the tab URL (no CDP traffic at all)
      2. CDP /sse/v2/streams interception (up to STREAMS_WAIT_SECONDS)
      3. Last-known value in ~/.dct-harvester-thread

    Returns (thread_id_or_None, source_label). The source label is
    "url" / "network" / "cache" / "missing" so the caller can log which
    path won.
    """
    url_tid = extract_thread_id_from_url(tab.get("url", ""))
    if url_tid:
        _save_thread_cache(url_tid)
        return url_tid, "url"

    logging.info(
        "no UUID in tab URL — falling back to CDP /streams interception "
        "(up to %ss)",
        STREAMS_WAIT_SECONDS,
    )
    cdp_tid = capture_thread_id_via_cdp(tab)
    if cdp_tid:
        _save_thread_cache(cdp_tid)
        return cdp_tid, "network"

    cached = _read_thread_id_from_cache()
    if cached:
        return cached, "cache"

    return None, "missing"


def _save_thread_cache(tid: str) -> None:
    try:
        THREAD_CACHE_PATH.write_text(tid)
    except OSError as e:
        logging.warning("could not write %s: %s", THREAD_CACHE_PATH, e)


# --- Webhook client ------------------------------------------------------

def post_credentials(bot_url: str, secret: str, payload: dict) -> dict:
    """POST the creds to the bot. Returns the parsed JSON response on success."""
    url = f"{bot_url.rstrip('/')}/credentials"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Harvester-Secret": secret,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:400]
        raise SystemExit(f"webhook HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise SystemExit(f"webhook network error: {e}")


# --- Entry point --------------------------------------------------------

def run_once(dry_run: bool = False) -> int:
    config = load_config()
    try:
        tabs = list_cdp_tabs()
    except (urllib.error.URLError, OSError) as e:
        logging.error(
            "could not reach Chrome DevTools at %s — is Chrome running with "
            "--remote-debugging-port=9222? (%s)",
            CDP_ENDPOINT, e,
        )
        return 2
    tab = find_tn_tab(tabs)
    if tab is None:
        logging.error(
            "no tab containing %s. Open app.true-north.xyz in the Chrome "
            "instance that was launched with --remote-debugging-port=9222.",
            TN_URL_FRAGMENT,
        )
        return 3

    logging.info("found TrueNorth tab: %s", tab.get("url"))
    access, refresh = read_localstorage_via_pychrome(tab)
    if not access or not refresh:
        logging.error(
            "localStorage missing privy:token / privy:refresh_token "
            "(access_len=%d, refresh_len=%d) — cookies may have expired; "
            "log in again in the Chrome tab.",
            len(access), len(refresh),
        )
        return 4
    if len(access) < 500:
        logging.warning(
            "access_token is short (len=%d) — may not be a full JWT (real "
            "Privy access tokens are typically 1500+ chars). Proceeding anyway.",
            len(access),
        )
    logging.info("pulled tokens (access_len=%d, refresh_len=%d)",
                 len(access), len(refresh))

    tid, tid_source = resolve_thread_id(tab)
    if not tid:
        logging.error(
            "no thread_id available — tab URL has no UUID and CDP fallback "
            "did not capture one. Open a chat in the TrueNorth tab (the URL "
            "should become .../chat/<uuid>) and re-run, or write a UUID to %s.",
            THREAD_CACHE_PATH,
        )
        return 5
    logging.info("thread_id: %s (from %s)", tid, tid_source)

    payload = {
        "access_token": access,
        "refresh_token": refresh,
        "thread_id": tid,
    }
    if dry_run:
        logging.info(
            "--verify set; skipping POST. Payload would be: %s",
            {
                **payload,
                "access_token": f"<jwt len={len(access)}>",
                "refresh_token": f"<opaque len={len(refresh)}>",
            },
        )
        return 0

    resp = post_credentials(config["bot_url"], config["harvester_secret"], payload)
    if not resp.get("ok"):
        logging.error("bot rejected credentials: %s", resp)
        return 6
    logging.info(
        "bot accepted credentials: thread_id=%s, access_exp=%s",
        resp.get("thread_id"), resp.get("access_token_exp"),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mac-local TN credential harvester")
    parser.add_argument("--once", action="store_true",
                        help="Run a single harvest cycle and exit (default).")
    parser.add_argument("--verify", action="store_true",
                        help="Read tokens + thread_id but do NOT POST to the bot.")
    args = parser.parse_args(argv)
    _configure_logging()
    return run_once(dry_run=args.verify)


if __name__ == "__main__":
    sys.exit(main())
