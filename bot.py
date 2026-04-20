"""
DCT TrueNorth Bot

Discord bot integrating TrueNorth AI trading intelligence with session-based scheduling.

Channels:
  #claude-integration -- Universal chat (finance->TN, general->Claude)
  #asia-session       -- Asia session briefs only
  #london-session     -- London session briefs only
  #us-session         -- US session briefs only
  #regime-outlook     -- Daily 24h macro outlook
  #trades             -- All trade setups from every session brief
"""

from __future__ import annotations

import os
import re
import json
import uuid
import time
import base64
import asyncio
import traceback
from collections import defaultdict
from datetime import datetime

import discord
from discord.ext import commands
import httpx
import anthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from zoneinfo import ZoneInfo

# Optional harvester — imports playwright internally with graceful degradation.
try:
    import harvester as _harvester
except Exception as _harvester_import_err:  # pragma: no cover — defensive
    _harvester = None
    print(f"[BOOT] harvester module unavailable: {_harvester_import_err}")

# --- Config ---
DISCORD_TOKEN    = os.environ["DISCORD_BOT_TOKEN"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
TN_TOKEN         = os.environ.get("TN_TOKEN", "")
TN_REFRESH       = os.environ.get("TN_REFRESH_TOKEN", "")
PRIVY_APP_ID     = os.environ.get("PRIVY_APP_ID", "cm6afcumv0688a6x3r78jkx7v")
PRANAY_DISCORD_ID = os.environ.get("PRANAY_DISCORD_ID", "").strip()
TN_SESSION_COOKIES = os.environ.get("TN_SESSION_COOKIES", "").strip()

TN_ENDPOINT      = "https://api.adventai.io/api/discovery-agents/sse/v2/streams"
TN_THREAD_ENV    = os.environ.get("TN_THREAD_ID", "").strip()
THREAD_CACHE_PATH = os.environ.get("TN_THREAD_CACHE", "thread_cache.json")
TOKEN_CACHE_PATH  = os.environ.get("TN_TOKEN_CACHE", "token_cache.json")
PRIVY_SESSION_URL = "https://auth.privy.io/api/v1/sessions"

IST = ZoneInfo("Asia/Kolkata")

CH = {
    "claude":  int(os.environ["CH_CLAUDE_INTEGRATION"]),
    "asia":    int(os.environ["CH_ASIA_SESSION"]),
    "london":  int(os.environ["CH_LONDON_SESSION"]),
    "us":      int(os.environ["CH_US_SESSION"]),
    "regime":  int(os.environ["CH_REGIME_OUTLOOK"]),
    "trades":  int(os.environ["CH_TRADES"]),
}

# --- Sonnet middleman config ---
SONNET_MODEL = "claude-sonnet-4-6"
RATE_LIMIT_MAX = 3
RATE_LIMIT_WINDOW = 3600
KNOWN_COMMANDS = ("!brief", "!trades", "!regime", "!winrate", "!refreshtoken", "!health", "!setcreds")

SONNET_SYSTEM = """You are a research assistant for crypto traders in the Corgi Calls Discord. You have access to TrueNorth AI, a multi-agent trading intelligence platform covering crypto, equities, Polymarket, derivatives, on-chain, smart money flow, options, liquidation risk, and meme discovery.

Use the query_truenorth tool to pull real-time market intelligence. For non-trivial questions, call it multiple times in parallel with different angles (technicals, derivatives/funding, on-chain, sentiment, smart money) to build a full picture before answering.

Tone: direct, trader-native. Use perps, HTF, R:R, OI, CMP, funding, liquidations, aping. Specific numbers over hedged language. No marketing speak. If TrueNorth returns nothing useful, say so — never invent data. Keep answers under 1800 chars for Discord."""

SONNET_TOOLS = [
    {
        "name": "query_truenorth",
        "description": "Ask TrueNorth AI a trading or market question. TrueNorth runs 50+ internal tools in parallel (technical analysis, derivatives flow, on-chain data, Polymarket, Hyperliquid smart money, options, liquidation risk, meme discovery, token unlocks, performance scanning, events). Returns a detailed natural-language answer. Call this multiple times with different angles for complex questions — e.g. one query for TA, another for derivatives, another for sentiment — then synthesize.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The question in natural language. Be specific: name the ticker(s), timeframe, and what kind of analysis you want (technicals, funding, liquidations, smart money positioning, etc.)."
                }
            },
            "required": ["query"],
            "additionalProperties": False
        },
        "cache_control": {"type": "ephemeral"}
    }
]

tn_thread_map: dict[int, str] = {}
user_sonnet_calls: dict[int, list[float]] = defaultdict(list)

# --- Embed colors ---
COLOR_LONG   = 0x00FF88
COLOR_SHORT  = 0xFF4444
COLOR_RISK   = 0xFFAA00
COLOR_ASIA   = 0x00BFFF
COLOR_LONDON = 0xFF8C00
COLOR_US     = 0x7B68EE
COLOR_REGIME = 0x9B59B6
COLOR_INFO   = 0x2F3136

FOOTER = "DCT TrueNorth Bot · Powered by TrueNorth AI"

# --- Bot setup ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
scheduler = AsyncIOScheduler(timezone=IST)

# --- Token cache (survives in-container restarts; helps because Privy refresh
# tokens are single-use — each refresh rotates them, so env-only storage loses
# the rotated token if the process restarts).
def _load_token_cache() -> dict:
    try:
        with open(TOKEN_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


def _save_token_cache(access: str, refresh: str) -> None:
    try:
        with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "access": access,
                    "refresh": refresh,
                    "updated_at": datetime.now(IST).isoformat(),
                },
                f,
            )
    except OSError as e:
        print(f"[Token] cache write failed: {e}")


_cached_tokens = _load_token_cache()
# Mutable token holder: prefer cached values (they are newer than env after any prior refresh)
token_store = {
    "access": _cached_tokens.get("access") or TN_TOKEN,
    "refresh": _cached_tokens.get("refresh") or TN_REFRESH,
}
if _cached_tokens:
    print(f"[BOOT] Loaded tokens from {TOKEN_CACHE_PATH} (updated_at={_cached_tokens.get('updated_at')})")

# --- Observability state ---
# A single mutable dict avoids any ambiguity about `global` declarations in
# functions that mutate these values (and any reader gets the current value
# automatically via dict lookup).
BOT_STARTED_AT: datetime = datetime.now(IST)
tn_state: dict = {
    "last_success_at": None,        # datetime | None
    "last_error_at": None,          # datetime | None
    "last_error": None,             # str | None
    "last_alert_at": 0.0,           # time.time() float
    "thread_invalid": False,        # bool — set when ping/empty-200 confirms TN doesn't know thread
    "thread_invalid_reason": "",    # str — surfaced in failure embeds and !health
    "boot_warning_posted": False,   # bool — guard against double-posting the boot warning
}
ALERT_COOLDOWN_SEC = 3600

# SSE / HTTP error markers that indicate auth vs thread-access failures.
AUTH_ERROR_MARKERS = (
    "not authenticated",
    "unauthorized",
    "invalid token",
    "token expired",
    "expired token",
    "invalid auth",
)
THREAD_ERROR_MARKERS = (
    "do not have access to this thread",
    "thread not found",
    "no such thread",
)

# --- Thread id cache ---
def load_cached_thread() -> str | None:
    try:
        with open(THREAD_CACHE_PATH, "r", encoding="utf-8") as f:
            tid = json.load(f).get("thread_id")
            if tid and isinstance(tid, str):
                return tid
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None

def save_cached_thread(thread_id: str) -> None:
    try:
        with open(THREAD_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {"thread_id": thread_id, "updated_at": datetime.now(IST).isoformat()},
                f,
            )
    except OSError as e:
        print(f"[Thread] cache write failed: {e}")

_tn_thread_current: str | None = None


def ensure_thread_id() -> str | None:
    """Return the current shared TrueNorth thread id from env/cache. NEVER mints a UUID.

    Random UUIDs are not valid TN threads — TN responds with HTTP 200 + zero
    chunks for unknown ids, which previously triggered ~25 minutes of useless
    retries before surfacing as 'No response from TrueNorth'. Returning None
    instead lets callers fast-fail with an actionable error.
    """
    global _tn_thread_current
    if _tn_thread_current:
        return _tn_thread_current
    if TN_THREAD_ENV:
        _tn_thread_current = TN_THREAD_ENV
        print(f"[Thread] Using TN_THREAD_ID from env: {_tn_thread_current}")
        return _tn_thread_current
    cached = load_cached_thread()
    if cached:
        _tn_thread_current = cached
        print(f"[Thread] Using cached thread id: {cached}")
        return cached
    return None


# AdventAI does not publish a thread-management spec. We probe a small set of
# REST siblings of the existing /sse/v2/streams endpoint AND likely top-level
# /api/threads paths. The first one that returns 200/201 with a thread id is
# cached and reused. If none work we surface that to the user instead of
# silently generating a UUID (which TN treats as an invalid thread).
TN_THREAD_CREATE_CANDIDATES = (
    "https://api.adventai.io/api/discovery-agents/threads",
    "https://api.adventai.io/api/discovery-agents/v2/threads",
    "https://api.adventai.io/api/discovery-agents/sse/v2/threads",
    "https://api.adventai.io/api/threads",
    "https://api.adventai.io/api/v1/threads",
    "https://api.adventai.io/api/chat/threads",
    "https://api.adventai.io/api/discovery-agents/threads/create",
)
_thread_create_endpoint: str | None = None  # cached after first success


async def _try_api_create_thread() -> str | None:
    """Probe known thread-create endpoints. Return id on success, None otherwise."""
    global _thread_create_endpoint
    candidates = (
        [_thread_create_endpoint] if _thread_create_endpoint
        else list(TN_THREAD_CREATE_CANDIDATES)
    )
    headers = {
        "Authorization": f"Bearer {token_store['access']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for url in candidates:
                try:
                    resp = await client.post(url, headers=headers, json={})
                except Exception as e:
                    print(f"[Thread] candidate {url} network error: {type(e).__name__}: {e}")
                    continue
                body_preview = resp.text[:200] if hasattr(resp, "text") else ""
                print(f"[Thread] POST {url} -> {resp.status_code} body={body_preview}")
                if resp.status_code not in (200, 201):
                    continue
                try:
                    data = resp.json()
                except Exception as e:
                    print(f"[Thread] {url} 200 OK but JSON parse failed: {e}")
                    continue
                tid = None
                if isinstance(data, dict):
                    tid = (
                        data.get("thread_id")
                        or data.get("id")
                        or data.get("threadId")
                    )
                    if not tid and isinstance(data.get("thread"), dict):
                        tid = data["thread"].get("id") or data["thread"].get("thread_id")
                if tid and isinstance(tid, str):
                    _thread_create_endpoint = url
                    print(f"[Thread] API created thread {tid} via {url}")
                    return tid
                print(f"[Thread] {url} 200 OK but no thread id field: keys={list(data) if isinstance(data, dict) else type(data).__name__}")
    except Exception as e:
        print(f"[Thread] _try_api_create_thread fatal: {type(e).__name__}: {e}")
    return None


async def ensure_thread_id_async() -> str | None:
    """Boot-time thread acquisition: env → cache → API create. Returns None if all fail.

    Never falls back to a random UUID. Callers must check for None and fast-fail.
    """
    global _tn_thread_current
    if _tn_thread_current:
        return _tn_thread_current
    if TN_THREAD_ENV:
        _tn_thread_current = TN_THREAD_ENV
        print(f"[Thread] Using TN_THREAD_ID from env: {_tn_thread_current}")
        return _tn_thread_current
    cached = load_cached_thread()
    if cached:
        _tn_thread_current = cached
        print(f"[Thread] Using cached thread id: {cached}")
        return cached
    tid = await _try_api_create_thread()
    if tid:
        _tn_thread_current = tid
        save_cached_thread(tid)
        return tid
    print("[Thread] No thread id available — TN-dependent commands will fast-fail.")
    return None


async def validate_thread(tid: str, timeout_s: float = 30.0) -> bool:
    """Tiny ping to confirm the thread is real. True iff TN streams >0 chunks within timeout."""
    if not tid:
        return False
    try:
        text, status, _err, sse_err, _completed = await _tn_call_once("ping", tid, timeout_s)
    except Exception as e:
        print(f"[Thread] validate_thread({tid}) raised {type(e).__name__}: {e}")
        return False
    ok = status == 200 and bool(text) and not sse_err
    print(f"[Thread] validate_thread({tid}) status={status} text_len={len(text)} ok={ok}")
    return ok


def invalidate_thread(reason: str) -> None:
    """Mark the shared thread invalid; future calls fast-fail."""
    global _tn_thread_current
    print(f"[Thread] Marking thread invalid: {reason}")
    tn_state["thread_invalid"] = True
    tn_state["thread_invalid_reason"] = reason
    _tn_thread_current = None

# --- JWT helper ---
def decode_jwt_exp(token: str) -> datetime | None:
    """Decode the `exp` claim from a JWT. Returns tz-aware datetime or None."""
    if not token or token.count(".") < 2:
        return None
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()))
        exp = payload.get("exp")
        if isinstance(exp, (int, float)):
            return datetime.fromtimestamp(int(exp), tz=IST)
    except Exception:
        return None
    return None

# --- TrueNorth SSE query ---
TN_DEFAULT_READ_TIMEOUT = 240.0  # TN multi-tool calls can take 2-3 min
TN_RETRY_READ_TIMEOUT   = 360.0  # Bump on retry-after-incomplete
_SSE_ERROR_MARKERS = (
    "error", "denied", "expired", "unauthorized", "not authenticated",
    "forbidden", "invalid", "fail",
)


async def _tn_call_once(
    prompt: str,
    thread_id: str,
    timeout_read: float,
) -> tuple[str, int, str, bool, bool]:
    """One SSE call.

    Returns (text, status_code, error_snippet, sse_error_flag, completed_flag).
    Raises on network errors (timeouts/connection issues are signaled as exceptions).
    """
    headers = {
        "Authorization": f"Bearer {token_store['access']}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "X-Accel-Buffering": "no",
    }
    body = {"query": prompt, "thread_id": thread_id}
    chunks: list[str] = []
    err_snippet = ""
    sse_error = False
    completed = False
    current_event = ""
    timeout = httpx.Timeout(30.0, read=timeout_read)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", TN_ENDPOINT, headers=headers, json=body) as resp:
            status = resp.status_code
            print(f"[TN HTTP] status={status} (read_timeout={timeout_read}s)")
            if status != 200:
                body_text = ""
                async for chunk in resp.aiter_text():
                    body_text += chunk
                    if len(body_text) > 500:
                        break
                err_snippet = body_text[:500]
                print(f"[TN HTTP] error body: {err_snippet}")
                return "", status, err_snippet, False, False
            async for line in resp.aiter_lines():
                line = line.rstrip()
                if not line:
                    continue
                if line.startswith("event:"):
                    current_event = line[6:].strip().lower()
                    if current_event in ("done", "complete", "end"):
                        completed = True
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    completed = True
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError as e:
                    print(f"[TN SSE] JSON error: {e} | raw: {data[:100]}")
                    continue
                if not isinstance(obj, dict):
                    continue
                event_type = obj.get("event_type") or current_event
                if event_type == "llm_output":
                    inner = obj.get("data") or {}
                    if isinstance(inner, dict):
                        content = inner.get("content", "")
                        if content:
                            if TN_DEBUG:
                                print(f"[Stream] +chunk len={len(content)}: {content[:80]!r}")
                            chunks.append(content)
                elif event_type == "error":
                    payload = obj.get("data") or obj
                    err_snippet = json.dumps(payload)[:500]
                    sse_error = True
                    print(f"[TN SSE] explicit error event: {err_snippet[:300]}")
                elif event_type in ("done", "complete", "end"):
                    completed = True
                elif "content" in obj and isinstance(obj["content"], str):
                    chunks.append(obj["content"])
                elif "detail" in obj:
                    # `detail` is sometimes informational, sometimes an error.
                    # Only flag as error when the payload contains error markers.
                    detail_val = obj["detail"]
                    detail_str = detail_val if isinstance(detail_val, str) else json.dumps(detail_val)
                    detail_low = detail_str.lower()
                    if any(m in detail_low for m in _SSE_ERROR_MARKERS):
                        if not err_snippet:
                            err_snippet = detail_str[:500]
                        sse_error = True
                        print(f"[TN SSE] error-shaped detail: {detail_str[:300]}")
                    else:
                        # benign informational detail — ignore
                        pass
    text = stitch_sse_chunks(chunks).strip()
    print(
        f"[TN STREAM] chunks={len(chunks)} stitched_len={len(text)} "
        f"completed={completed} sse_error={sse_error}"
    )
    return text, status, err_snippet, sse_error, completed


def _classify_error(status: int, err_snippet: str) -> tuple[bool, bool]:
    """Return (is_auth_error, is_thread_error) from a status/error snippet."""
    low = (err_snippet or "").lower()
    auth = status in (401, 403) or any(m in low for m in AUTH_ERROR_MARKERS)
    thread = any(m in low for m in THREAD_ERROR_MARKERS) or (
        "access" in low and "thread" in low
    )
    return auth, thread


def _looks_like_preamble(text: str) -> bool:
    """Heuristic: TN occasionally closes the stream early with only a 'thinking' preamble.

    If the captured text is short and matches typical preamble phrases, treat it
    as an incomplete stream and retry with a longer timeout.
    """
    if not text:
        return False
    if len(text) > 600:
        return False
    head = text.strip().lower()[:200]
    return any(
        phrase in head
        for phrase in (
            "let me scan",
            "let me check",
            "let me look",
            "let me analyze",
            "let me pull",
            "i'll scan",
            "i'll check",
            "scanning ",
            "checking ",
        )
    )


# Bounded retry policy: 3 attempts total, sleep-then-retry on transient failures.
TN_MAX_ATTEMPTS = 3
TN_RETRY_BACKOFF_S = (10.0, 30.0)  # before retry 1, before retry 2

THREAD_INVALID_HINT = (
    "TN_THREAD_ID is invalid or unknown to TrueNorth. "
    "Get a fresh thread_id: open https://app.true-north.xyz, start any chat, "
    "open DevTools → Network, find the request to /api/discovery-agents/sse/v2/streams, "
    "copy `thread_id` from its JSON body, set TN_THREAD_ID in Railway, redeploy."
)


def _parse_sse_error_payload(err: str) -> dict:
    """Best-effort pull of error_type / error_code / error_message out of a JSON SSE error blob."""
    out = {"error_type": "", "error_code": "", "error_message": ""}
    if not err:
        return out
    try:
        obj = json.loads(err)
    except Exception:
        return out
    if not isinstance(obj, dict):
        return out
    # TN wraps the actual fields under "data" sometimes.
    candidate = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    for k in ("error_type", "error_code", "error_message"):
        v = candidate.get(k) or obj.get(k)
        if isinstance(v, str):
            out[k] = v
    return out


def _classify_failure(status: int, err: str, sse_error: bool, exc: Exception | None,
                      text: str, completed: bool) -> str:
    """Categorize a single _tn_call_once result. Returns a short tag used to pick error text."""
    if exc is not None:
        return "timeout" if isinstance(exc, (httpx.ReadTimeout, httpx.ConnectTimeout)) else "exception"
    if status in (401, 403):
        return "auth"
    if status >= 400:
        return "http_error"
    if sse_error:
        parsed = _parse_sse_error_payload(err)
        if parsed["error_type"] == "InvalidRequestError" or "InvalidRequestError" in (err or ""):
            return "invalid_request"
        return "sse_error"
    if status == 200 and not text:
        # 200 OK with zero content is the signature of an unknown thread on TN.
        return "empty_200"
    if status == 200 and not completed and _looks_like_preamble(text):
        return "incomplete_preamble"
    return "ok"


async def query_truenorth(
    prompt: str,
    thread_id: str | None = None,
    timeout_read: float = TN_DEFAULT_READ_TIMEOUT,
) -> str:
    """Send prompt to TrueNorth with bounded retries and honest fast-fail.

    Retry policy (max 3 attempts total, capped at ~40s of backoff):
      - auth failure → refresh token, retry once
      - empty 200 (unknown thread) → mark thread invalid, do NOT retry
      - timeout / 5xx → backoff 10s, then 30s
      - incomplete preamble → one retry with longer timeout
    """
    caller_pinned_thread = thread_id is not None
    tid = thread_id

    if tid is None:
        if tn_state.get("thread_invalid"):
            print("[TN] shared thread previously invalidated — fast-failing")
            tn_state["last_error"] = THREAD_INVALID_HINT
            tn_state["last_error_at"] = datetime.now(IST)
            await alert_thread_invalid()
            return ""
        tid = ensure_thread_id()
        if tid is None:
            invalidate_thread("no thread id available (env unset, cache empty, API probe found no endpoint)")
            await alert_thread_invalid()
            return ""

    last: dict = {"status": 0, "err": "", "sse_error": False, "exc": None, "text": "", "completed": False, "tag": "init"}
    for attempt in range(1, TN_MAX_ATTEMPTS + 1):
        if attempt > 1:
            backoff = TN_RETRY_BACKOFF_S[min(attempt - 2, len(TN_RETRY_BACKOFF_S) - 1)]
            print(f"[TN] backoff {backoff}s before attempt {attempt}/{TN_MAX_ATTEMPTS}")
            await asyncio.sleep(backoff)

        text, status, err, sse_error, completed, exc = "", 0, "", False, False, None
        try:
            text, status, err, sse_error, completed = await _tn_call_once(prompt, tid, timeout_read)
        except Exception as e:
            exc = e
            print(f"[TN ERROR attempt={attempt}] {type(e).__name__}: {e}")

        tag = _classify_failure(status, err, sse_error, exc, text, completed)
        last = {"status": status, "err": err, "sse_error": sse_error, "exc": exc,
                "text": text, "completed": completed, "tag": tag, "attempt": attempt, "tid": tid}
        print(f"[TN] attempt={attempt} tag={tag} status={status} text_len={len(text)} completed={completed}")

        if tag == "ok":
            tn_state["last_success_at"] = datetime.now(IST)
            cleaned = dedupe_tn_text(sanitize_tn_text(text))
            return cleaned

        # Empty 200: thread is unknown to TN. Stop immediately.
        if tag == "empty_200":
            invalidate_thread("TN returned HTTP 200 with zero stream chunks (unknown thread)")
            await alert_thread_invalid()
            tn_state["last_error"] = THREAD_INVALID_HINT
            tn_state["last_error_at"] = datetime.now(IST)
            return ""

        if tag == "auth":
            print("[TN] auth failure — attempting token refresh before retrying")
            await refresh_tn_token()
            timeout_read = TN_DEFAULT_READ_TIMEOUT
            continue

        if tag == "incomplete_preamble":
            timeout_read = TN_RETRY_READ_TIMEOUT
            continue

        # timeout / http_error / sse_error / exception → backoff + retry
        # Retry only if we have attempts left.
        if attempt >= TN_MAX_ATTEMPTS:
            break
        # On http_error 5xx we keep timeout the same; on timeout we bump it.
        if tag == "timeout":
            timeout_read = TN_RETRY_READ_TIMEOUT

    # All attempts exhausted.
    reason = _format_failure_reason(last)
    tn_state["last_error"] = reason
    tn_state["last_error_at"] = datetime.now(IST)
    await alert_tn_failure(reason, last)
    return ""


def _format_failure_reason(last: dict) -> str:
    tag = last.get("tag", "unknown")
    status = last.get("status", 0)
    err = last.get("err", "") or ""
    exc = last.get("exc")
    if tag == "auth":
        return f"Auth failed (HTTP {status}). Run !refreshtoken."
    if tag == "empty_200":
        return THREAD_INVALID_HINT
    if tag == "invalid_request":
        parsed = _parse_sse_error_payload(err)
        msg = parsed["error_message"] or err[:300] or "request rejected"
        return (
            f"Request rejected by TrueNorth: {msg}. "
            f"Thread may be stale — try `!setcreds` with a fresh thread_id."
        )
    if tag == "http_error":
        return f"TrueNorth API error: HTTP {status}. {err[:200]}"
    if tag == "sse_error":
        return f"TrueNorth SSE error: {err[:300]}"
    if tag == "timeout":
        return f"TrueNorth timed out after {TN_RETRY_READ_TIMEOUT}s. Try again."
    if exc is not None:
        return f"{type(exc).__name__}: {exc}"
    return err or f"unknown failure (status={status})"


def _token_exp_summary() -> str:
    exp = decode_jwt_exp(token_store.get("access", ""))
    if not exp:
        return "unknown (token missing or malformed)"
    now = datetime.now(IST)
    delta = int((exp - now).total_seconds())
    base = exp.strftime("%Y-%m-%d %H:%M %Z")
    if delta > 0:
        return f"{base} (in {delta // 3600}h {(delta % 3600) // 60}m)"
    return f"{base} (EXPIRED {(-delta) // 60}m ago)"


async def alert_tn_failure(reason: str, last: dict | None = None) -> None:
    """Rate-limited diagnostic embed for unrecovered TN failures.

    The embed reflects the ACTUAL failure mode (status, chunks, completed flag,
    SSE error, retry count) instead of the old generic 'token may need refresh'.
    """
    now = time.time()
    if now - tn_state["last_alert_at"] < ALERT_COOLDOWN_SEC:
        return
    tn_state["last_alert_at"] = now
    try:
        channel = bot.get_channel(CH["claude"])
        if not channel:
            return
        tag = (last or {}).get("tag", "unknown")
        title_by_tag = {
            "auth": "🚨 TrueNorth auth failure",
            "http_error": "🚨 TrueNorth API error",
            "sse_error": "🚨 TrueNorth SSE error",
            "invalid_request": "🚨 TrueNorth rejected request",
            "timeout": "🚨 TrueNorth timeout",
            "exception": "🚨 TrueNorth network error",
            "empty_200": "🚨 TrueNorth thread invalid",
        }
        title = title_by_tag.get(tag, "🚨 TrueNorth failure")
        e = discord.Embed(title=title, description=f"```\n{reason[:1500]}\n```", color=COLOR_RISK)
        e.add_field(name="Thread", value=f"`{_tn_thread_current or '—'}`", inline=True)
        if last:
            e.add_field(name="Status", value=str(last.get("status", "—")), inline=True)
            e.add_field(name="Attempt", value=f"{last.get('attempt', '?')}/{TN_MAX_ATTEMPTS}", inline=True)
            e.add_field(name="Chunks (text len)", value=str(len(last.get("text") or "")), inline=True)
            e.add_field(name="Stream completed", value=("yes" if last.get("completed") else "no"), inline=True)
            err_val = last.get("err") or ""
            parsed = _parse_sse_error_payload(err_val)
            if parsed["error_type"] or parsed["error_code"] or parsed["error_message"]:
                if parsed["error_type"]:
                    e.add_field(name="SSE error_type", value=f"`{parsed['error_type']}`", inline=True)
                if parsed["error_code"]:
                    e.add_field(name="SSE error_code", value=f"`{parsed['error_code']}`", inline=True)
                if parsed["error_message"]:
                    e.add_field(name="SSE error_message", value=f"```\n{parsed['error_message'][:400]}\n```", inline=False)
            elif err_val:
                e.add_field(name="Last SSE error", value=f"```\n{str(err_val)[:400]}\n```", inline=False)
        e.add_field(name="Token exp", value=_token_exp_summary(), inline=False)
        e.set_footer(text=f"{FOOTER} · alert rate-limited 1/hr")
        e.timestamp = datetime.now(IST)
        await channel.send(embed=e)
    except Exception as send_err:
        print(f"[Alert] send failed: {type(send_err).__name__}: {send_err}")


async def alert_thread_invalid() -> None:
    """One-time per-process warning when the shared thread is unusable."""
    if tn_state.get("boot_warning_posted"):
        return
    tn_state["boot_warning_posted"] = True
    try:
        channel = bot.get_channel(CH["claude"])
        if not channel:
            return
        reason = tn_state.get("thread_invalid_reason") or "TN_THREAD_ID rejected by TrueNorth"
        e = discord.Embed(
            title="⚠️ TN_THREAD_ID is invalid — TN-dependent commands are disabled",
            description=THREAD_INVALID_HINT,
            color=COLOR_RISK,
        )
        e.add_field(name="Detected because", value=reason[:1000], inline=False)
        e.add_field(name="Currently configured thread", value=f"`{TN_THREAD_ENV or load_cached_thread() or '—'}`", inline=False)
        e.add_field(name="What still works", value="`!health`, `!refreshtoken`", inline=False)
        e.add_field(
            name="What will fail",
            value="`!brief`, `!trades`, `!regime`, scheduled session briefs",
            inline=False,
        )
        e.add_field(name="Token exp", value=_token_exp_summary(), inline=False)
        e.set_footer(text=FOOTER)
        e.timestamp = datetime.now(IST)
        await channel.send(embed=e)
    except Exception as send_err:
        print(f"[Alert] thread-invalid send failed: {type(send_err).__name__}: {send_err}")

# --- Claude fallback ---
def ask_claude(prompt: str) -> str:
    """Direct Claude query for non-finance questions."""
    try:
        resp = claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text
    except Exception as e:
        print(f"[CLAUDE ERROR] {e}")
        return "Something went wrong with Claude. Try again."

# --- Token refresh ---
TN_APP_ORIGIN = os.environ.get("TN_APP_ORIGIN", "https://app.true-north.xyz")


def _privy_headers() -> dict[str, str]:
    """Headers for the Privy /sessions endpoint.

    Privy validates Origin against the app's allowed-origins list — without it,
    refresh returns 403 missing_origin. Referer is also commonly checked.
    """
    return {
        "privy-app-id": PRIVY_APP_ID,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "privy-client": "react-auth:2.0.0",
        "Origin": TN_APP_ORIGIN,
        "Referer": f"{TN_APP_ORIGIN.rstrip('/')}/",
    }


def _mask(tok: str) -> str:
    """Return a truncated token for log safety (first 8 + last 4 chars)."""
    if not tok:
        return "<empty>"
    if len(tok) < 16:
        return f"{tok[:4]}…"
    return f"{tok[:8]}…{tok[-4:]} (len={len(tok)})"


def _privy_refresh_variants(refresh_token: str, access_token: str) -> list[dict]:
    """Body+header variants we'll try in order until one returns 200.

    Privy's session endpoint isn't publicly documented for direct calls; SDK
    integrations differ in body shape. We probe the common shapes and cache
    the first one that works in `_privy_refresh_variant`.
    """
    base = _privy_headers()
    rt_bearer = {**base, "Authorization": f"Bearer {refresh_token}"}
    at_bearer = {**base, "Authorization": f"Bearer {access_token}"} if access_token else None
    variants = [
        # 1) Original spec shape (current default).
        {"name": "snake_body", "body": {"refresh_token": refresh_token}, "headers": base},
        # 2) Same body + bearer header carrying the refresh token.
        {"name": "snake_body+bearer_refresh", "body": {"refresh_token": refresh_token}, "headers": rt_bearer},
        # 3) camelCase body field.
        {"name": "camel_body", "body": {"refreshToken": refresh_token}, "headers": base},
        # 4) Empty body, refresh-token in bearer header.
        {"name": "bearer_refresh_only", "body": {}, "headers": rt_bearer},
    ]
    if at_bearer:
        # 5) Body + bearer header carrying current ACCESS token (some OAuth flows).
        variants.append(
            {"name": "snake_body+bearer_access", "body": {"refresh_token": refresh_token}, "headers": at_bearer},
        )
    return variants


# Cache the first variant that ever returns 200 so we stop probing.
_privy_refresh_variant: str | None = None


async def _privy_attempt(client: httpx.AsyncClient, body: dict, headers: dict) -> tuple[int, str, dict | None]:
    resp = await client.post(PRIVY_SESSION_URL, json=body, headers=headers)
    status = resp.status_code
    body_preview = resp.text[:500]
    parsed: dict | None = None
    if status == 200:
        try:
            parsed = resp.json() if isinstance(resp.json(), dict) else None
        except Exception:
            parsed = None
    return status, body_preview, parsed


async def _apply_harvested_creds(update: dict) -> None:
    """Callback for harvester.harvest_session. Persists new creds to memory + caches."""
    global _tn_thread_current
    access = update.get("access_token") or ""
    refresh = update.get("refresh_token") or ""
    thread_id = update.get("thread_id") or ""
    if access:
        token_store["access"] = access
    if refresh:
        token_store["refresh"] = refresh
    if access or refresh:
        _save_token_cache(token_store["access"], token_store["refresh"])
    if thread_id:
        _tn_thread_current = thread_id
        save_cached_thread(thread_id)
    tn_state["thread_invalid"] = False
    tn_state["thread_invalid_reason"] = ""
    tn_state["boot_warning_posted"] = False
    print("[Harvester] applied harvested credentials to in-memory state")


async def run_cookie_harvest() -> dict:
    """Run the Playwright harvester once. Posts a Discord alert if the session is dead.

    Returns the raw status dict from harvester.harvest_session (or an equivalent).
    """
    if not TN_SESSION_COOKIES:
        return {"ok": False, "reason": "TN_SESSION_COOKIES not set", "applied": False}
    if _harvester is None:
        return {"ok": False, "reason": "harvester module failed to import", "applied": False}
    status = await _harvester.harvest_session(TN_SESSION_COOKIES, _apply_harvested_creds)
    if not status.get("ok"):
        await _alert_session_cookies_dead(status.get("reason") or "unknown")
    return status


async def _alert_session_cookies_dead(reason: str) -> None:
    """One post per hour if the harvester can't keep the session alive."""
    now = time.time()
    if now - tn_state.get("last_alert_at", 0.0) < ALERT_COOLDOWN_SEC:
        return
    tn_state["last_alert_at"] = now
    try:
        channel = bot.get_channel(CH["claude"])
        if not channel:
            return
        e = discord.Embed(
            title="⚠️ TrueNorth session cookies expired",
            description=(
                "The harvester could not restore an authenticated session. "
                f"`{reason[:400]}`\n\n"
                "Use `!setcreds` with fresh values or re-export TN_SESSION_COOKIES."
            ),
            color=COLOR_RISK,
        )
        e.set_footer(text=FOOTER)
        e.timestamp = datetime.now(IST)
        await channel.send(embed=e)
    except Exception as e:
        print(f"[Alert] session-cookies-dead send failed: {type(e).__name__}: {e}")


async def refresh_tn_token() -> bool:
    """Refresh TN access token via Privy. Tries multiple body/header shapes.

    Returns True on success. Logs each attempt's status + body preview so the
    next failure is fully diagnosable in Railway logs. Stops on the first 200
    and caches the working variant for subsequent refreshes.
    """
    global _privy_refresh_variant
    rt = token_store["refresh"]
    at = token_store.get("access", "")
    if not rt:
        print("[TokenRefresh] TN_REFRESH_TOKEN not set -- skipping")
        return False
    print(f"[TokenRefresh] POST {PRIVY_SESSION_URL} refresh={_mask(rt)} app_id={PRIVY_APP_ID}")
    all_variants = _privy_refresh_variants(rt, at)
    if _privy_refresh_variant:
        # Try the cached variant first; fall through to others if it stopped working.
        all_variants.sort(key=lambda v: 0 if v["name"] == _privy_refresh_variant else 1)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for variant in all_variants:
                name = variant["name"]
                try:
                    status, body_preview, data = await _privy_attempt(
                        client, variant["body"], variant["headers"],
                    )
                except Exception as e:
                    print(f"[TokenRefresh] variant={name} network error: {type(e).__name__}: {e}")
                    continue
                print(f"[TokenRefresh] variant={name} status={status} body={body_preview}")
                if status != 200 or not isinstance(data, dict):
                    continue
                print(f"[TokenRefresh] variant={name} response keys: {sorted(data.keys())}")
                new_access = (
                    data.get("token")
                    or data.get("access_token")
                    or data.get("identity_token")
                    or data.get("privy_access_token")
                )
                new_refresh = (
                    data.get("refresh_token")
                    or data.get("privy_refresh_token")
                )
                if not new_access:
                    print(f"[TokenRefresh] variant={name} 200 OK but no access-token field")
                    continue
                token_store["access"] = new_access
                if new_refresh:
                    token_store["refresh"] = new_refresh
                    print(f"[TokenRefresh] refresh token rotated: {_mask(new_refresh)}")
                _save_token_cache(token_store["access"], token_store["refresh"])
                _privy_refresh_variant = name
                exp = decode_jwt_exp(token_store["access"])
                print(f"[TokenRefresh] ok via variant={name}; access exp={exp}")
                return True
        print("[TokenRefresh] all variants failed; see status/body for each above")
        return False
    except Exception as e:
        print(f"[TokenRefresh] fatal: {type(e).__name__}: {e}")
        traceback.print_exc()
        return False

# --- Chat routing helpers ---
def check_rate_limit(user_id: int) -> bool:
    now = time.time()
    calls = user_sonnet_calls[user_id]
    calls[:] = [t for t in calls if now - t < RATE_LIMIT_WINDOW]
    if len(calls) >= RATE_LIMIT_MAX:
        return False
    calls.append(now)
    return True

def rate_limit_retry_min(user_id: int) -> int:
    calls = user_sonnet_calls[user_id]
    if not calls:
        return 0
    return max(1, int((RATE_LIMIT_WINDOW - (time.time() - calls[0])) / 60))

def get_tn_thread(conv_id: int) -> str | None:
    """Per-conversation thread id.

    Returns the shared validated thread for now — TrueNorth rejects unknown
    UUIDs (HTTP 200 + zero chunks), so generating a per-conv UUID here just
    breaks chat. Once a real thread-create endpoint is wired this can mint a
    fresh thread per conversation again.
    """
    return ensure_thread_id()

async def get_or_create_conv_thread(message: discord.Message, content: str) -> discord.abc.Messageable:
    if isinstance(message.channel, discord.Thread):
        return message.channel
    name = content.strip().splitlines()[0][:80] if content.strip() else "chat"
    return await message.create_thread(name=name or "chat", auto_archive_duration=60)

async def send_long(channel, text: str):
    text = text.strip() or "(empty response)"
    for i in range(0, len(text), 1900):
        await channel.send(text[i:i+1900])

async def handle_direct_tn(message: discord.Message, content: str):
    conv = await get_or_create_conv_thread(message, content)
    tn_id = get_tn_thread(conv.id)
    async with conv.typing():
        result = await query_truenorth(content, tn_id)
        if not result:
            await conv.send("⚠️ no response from truenorth. try again or run `!refreshtoken`.")
            return
        await send_long(conv, result)

async def run_sonnet_loop(query: str, tn_thread_id: str, max_iter: int = 5) -> str:
    messages = [{"role": "user", "content": query}]
    for _ in range(max_iter):
        response = await asyncio.to_thread(
            claude_client.messages.create,
            model=SONNET_MODEL,
            max_tokens=2048,
            system=[{"type": "text", "text": SONNET_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=SONNET_TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return "".join(b.text for b in response.content if b.type == "text")
        messages.append({"role": "assistant", "content": response.content})
        tool_ids: list[str] = []
        tool_coros = []
        tool_results: list[dict] = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "query_truenorth":
                    tool_ids.append(block.id)
                    tool_coros.append(query_truenorth(block.input["query"], tn_thread_id))
                else:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"unknown tool: {block.name}",
                        "is_error": True,
                    })
        if tool_coros:
            results = await asyncio.gather(*tool_coros, return_exceptions=True)
            for tid, r in zip(tool_ids, results):
                content = r if isinstance(r, str) and r else "no data"
                if isinstance(r, Exception):
                    content = f"tool error: {type(r).__name__}: {r}"
                tool_results.append({"type": "tool_result", "tool_use_id": tid, "content": content})
        messages.append({"role": "user", "content": tool_results})
    return "(sonnet exceeded tool-call iteration limit)"

async def handle_sonnet(message: discord.Message, query: str):
    if not check_rate_limit(message.author.id):
        wait_min = rate_limit_retry_min(message.author.id)
        await message.reply(
            f"⏱️ rate limit: {RATE_LIMIT_MAX} claude calls/hour. try again in ~{wait_min}m, or drop the `!` to query truenorth directly.",
            mention_author=False,
        )
        return
    conv = await get_or_create_conv_thread(message, query)
    tn_id = get_tn_thread(conv.id)
    async with conv.typing():
        try:
            text = await run_sonnet_loop(query, tn_id)
        except anthropic.BadRequestError as e:
            print(f"[SONNET 400] {e}")
            await conv.send(f"⚠️ claude rejected the request: {e.message[:300]}")
            return
        except anthropic.RateLimitError:
            await conv.send("⚠️ anthropic rate limit hit. try again in a minute.")
            return
        except Exception as e:
            print(f"[SONNET ERROR] {type(e).__name__}: {e}")
            traceback.print_exc()
            await conv.send(f"⚠️ claude error: {type(e).__name__}")
            return
        await send_long(conv, text)

# --- Response parsers ---
_TICKER_HEADER_RE = re.compile(r'\$[A-Z]{1,10}\s*\|\s*(LONG|SHORT)\b', re.IGNORECASE)
_TOKEN_TAG_RE = re.compile(r'<\s*Token\b[^>]*/?>', re.IGNORECASE)
_TOKEN_SYMBOL_ATTR_RE = re.compile(r'tokenSymbol\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_ANCHOR_TAG_RE = re.compile(r'<\s*Anchor\b[^>]*/?>', re.IGNORECASE)
# TrueNorth's "suggested prompts" markup: <sp p="deriv">…</sp>. Useless in Discord — drop entirely.
_SP_PAIRED_RE = re.compile(r'<\s*sp\b[^>]*>.*?<\s*/\s*sp\s*>', re.DOTALL | re.IGNORECASE)
_SP_OPEN_RE = re.compile(r'<\s*sp\b[^>]*/?>', re.IGNORECASE)
_SP_CLOSE_RE = re.compile(r'<\s*/\s*sp\s*>', re.IGNORECASE)
# Generic self-closing tag (any case).
_GENERIC_SELF_CLOSING_RE = re.compile(r'<[a-zA-Z][a-zA-Z0-9]*(?:\s+[^>]*)?\s*/>')
# Generic paired tag — keep inner text, drop wrapper. Names captured for backref.
_GENERIC_PAIRED_RE = re.compile(
    r'<([a-zA-Z][a-zA-Z0-9]*)(?:\s+[^>]*)?>(.*?)</\1>',
    re.DOTALL,
)
# Orphan tags left over after the paired pass: standalone </Foo> closes or
# <Bar attr="x"> opens whose match was outside the captured slice (e.g. when
# TrueNorth truncates mid-paragraph).
_ORPHAN_CLOSING_RE = re.compile(r'</[a-zA-Z][a-zA-Z0-9]*\s*>')
_ORPHAN_OPENING_RE = re.compile(r'<[a-zA-Z][a-zA-Z0-9]*(?:\s+[^>]*)?>')
_DUP_SPACE_RE = re.compile(r'[ \t]{2,}')


def strip_token_tags(text: str) -> str:
    """Replace <Token ... tokenSymbol="X" ... /> with $X; drop tags with no symbol."""
    def _sub(m: re.Match) -> str:
        sym = _TOKEN_SYMBOL_ATTR_RE.search(m.group(0))
        return f"${sym.group(1).upper()}" if sym else ""
    text = _TOKEN_TAG_RE.sub(_sub, text)
    # Drop any stray closing </Token>
    text = re.sub(r'</\s*Token\s*>', '', text, flags=re.IGNORECASE)
    return text


def sanitize_tn_text(text: str) -> str:
    """Single chokepoint: scrub all TrueNorth-specific markup before downstream consumers.

    Order matters: handle the named tags TrueNorth ships first, then the generic
    fallbacks so they don't accidentally swallow inner content of named tags.

    - <Token tokenSymbol="X" .../>            → $X
    - <Anchor annotationId="..." />           → removed (no user value)
    - <sp p="...">…</sp>                      → removed entirely (Claude tool-use markup)
    - other self-closing <tag .../>           → removed
    - other paired <tag>inner</tag>           → keep inner, drop wrapper
    - Stray </Token>                          → removed
    - Collapse intra-line whitespace, trim trailing spaces before newlines.
    """
    if not text:
        return text
    text = strip_token_tags(text)
    text = _ANCHOR_TAG_RE.sub("", text)
    # Drop entire <sp …>…</sp> elements first (paired). Apply twice in case of nesting.
    for _ in range(2):
        new = _SP_PAIRED_RE.sub("", text)
        if new == text:
            break
        text = new
    # Then drop any orphan <sp …> openings/closings that survived (truncated stream, etc).
    text = _SP_OPEN_RE.sub("", text)
    text = _SP_CLOSE_RE.sub("", text)
    # Generic self-closing tags (covers any leftover Capitalised or lowercase).
    text = _GENERIC_SELF_CLOSING_RE.sub("", text)
    # Generic paired tags: keep the inner text, drop the wrapper. Apply iteratively
    # to handle nesting like <a><b>x</b></a>.
    for _ in range(3):
        new = _GENERIC_PAIRED_RE.sub(lambda m: m.group(2), text)
        if new == text:
            break
        text = new
    # Orphan tags (no surviving partner) — must run AFTER paired-tag handling
    # so we don't accidentally chew away a valid wrapper before it's processed.
    text = _ORPHAN_CLOSING_RE.sub("", text)
    text = _ORPHAN_OPENING_RE.sub("", text)
    # Tidy up double spaces left by tag removal, but preserve newlines/indentation.
    text = _DUP_SPACE_RE.sub(" ", text)
    # Trim spaces before line breaks for cleaner table alignment.
    text = re.sub(r' +\n', '\n', text)
    return text


# --- SSE chunk stitching (Task 3 + 4) -----------------------------------------
TN_DEBUG = os.environ.get("TN_DEBUG", "").lower() in ("1", "true", "yes")


def stitch_sse_chunks(chunks: list[str]) -> str:
    """Join chunks while removing overlap between consecutive chunks.

    TrueNorth's SSE stream sometimes re-emits the tail of the previous frame at
    the head of the next frame (and occasionally the entire previous preamble).
    Plain "".join produces visible duplications. This walks chunk-by-chunk and
    drops the longest prefix of each new chunk that already matches the running
    output's suffix (capped at 400 chars to keep this O(N·k)).
    """
    out_parts: list[str] = []
    running: str = ""
    for raw in chunks:
        if not raw:
            continue
        if running:
            cap = min(len(running), len(raw), 400)
            overlap = 0
            for n in range(cap, 0, -1):
                if running.endswith(raw[:n]):
                    overlap = n
                    break
            if overlap:
                if TN_DEBUG:
                    print(f"[Stream] dropped {overlap}-char overlap from chunk len={len(raw)}")
                raw = raw[overlap:]
        if raw:
            out_parts.append(raw)
            running += raw
    return "".join(out_parts)


_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')


def dedupe_contiguous_lines(text: str) -> str:
    """Drop a line that immediately repeats the previous non-blank line."""
    if not text:
        return text
    out: list[str] = []
    last_non_blank: str | None = None
    for line in text.split("\n"):
        s = line.strip()
        if s and s == last_non_blank:
            continue
        out.append(line)
        if s:
            last_non_blank = s
    return "\n".join(out)


def dedupe_repeated_substrings(text: str, min_len: int = 30, max_len: int = 400) -> str:
    """Collapse adjacent identical substrings (helps when an entire preamble repeats)."""
    if not text or len(text) < 2 * min_len:
        return text
    i = 0
    out: list[str] = []
    n = len(text)
    while i < n:
        cap = min((n - i) // 2, max_len)
        match = 0
        if cap >= min_len:
            for k in range(cap, min_len - 1, -1):
                if text[i:i + k] == text[i + k:i + 2 * k]:
                    match = k
                    break
        if match:
            out.append(text[i:i + match])
            if TN_DEBUG:
                print(f"[Stream] collapsed {match}-char repeat at offset {i}")
            i += 2 * match
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def dedupe_tn_text(text: str) -> str:
    """Pipeline: substring repeats → contiguous-line repeats."""
    return dedupe_contiguous_lines(dedupe_repeated_substrings(text))


_HEADER_LINE_RE = re.compile(r'^\s*(?:\d+[\.\)]|#{1,6})\s')
# A line that is JUST a section number with no content (e.g. "5." or "5...."),
# usually left behind when the truncate cuts mid-header.
_DANGLING_HEADER_RE = re.compile(r'^\s*\d+[\.\)]+\s*\d*\s*$')


def _truncate_at_sentence(text: str, limit: int) -> str:
    """Truncate to <=limit chars, preferring the last sentence boundary.

    If the chosen cut would leave a dangling section-header line behind
    (e.g. ``"5."`` or ``"5...."`` from a numbered list), back up to the
    previous sentence boundary so the embed doesn't end with an orphan header.
    """
    if len(text) <= limit:
        return _strip_dangling_header(text)
    cut = text[: limit - 1]

    candidates: list[int] = []
    for term in (". ", "! ", "? ", ".\n", "!\n", "?\n"):
        idx = cut.rfind(term)
        if idx >= limit * 0.5:
            candidates.append(idx + 1)
    candidates.sort(reverse=True)

    for end_idx in candidates:
        snippet = cut[:end_idx].rstrip()
        # Reject snippets whose final line is a header (numbered or hashed) —
        # that means the truncate landed right after the header marker but
        # before the body, which renders as just "5." in Discord.
        last_line = snippet.rsplit("\n", 1)[-1]
        if _HEADER_LINE_RE.match(last_line):
            continue
        return _strip_dangling_header(snippet) + "…"

    return _strip_dangling_header(cut.rstrip()) + "…"


def _strip_dangling_header(text: str) -> str:
    """Remove a trailing line that is just a numbered header (no body)."""
    if not text:
        return text
    lines = text.rstrip().split("\n")
    while lines and _DANGLING_HEADER_RE.match(lines[-1]):
        lines.pop()
    return "\n".join(lines).rstrip()


def wrap_markdown_tables(text: str) -> str:
    """Wrap consecutive pipe-prefixed lines in fenced code blocks for monospace rendering."""
    if not text or "|" not in text:
        return text
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].lstrip().startswith("|"):
            j = i
            while j < n and lines[j].lstrip().startswith("|"):
                j += 1
            if j - i >= 2:
                out.append("```")
                out.extend(lines[i:j])
                out.append("```")
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _clean_price(s: str) -> str:
    """Strip leading $ and surrounding whitespace so the display layer can re-add $ once."""
    if not s:
        return ""
    return s.strip().lstrip("$").strip()


def _fmt_price(s: str) -> str:
    """Display-ready price with exactly one leading $."""
    cleaned = _clean_price(s)
    return f"${cleaned}" if cleaned else ""


def parse_trades_from_text(text: str) -> list[dict]:
    """Extract structured trades from TrueNorth markdown response."""
    text = strip_token_tags(text)
    trades = []
    blocks = re.split(r'\n-{3,}\n|\n(?=\$[A-Z])', text)
    for block in blocks:
        ticker_match = re.search(
            r'\$([A-Z]+)\s*\|?\s*(LONG|SHORT)\s*\|?\s*(.*)',
            block, re.IGNORECASE
        )
        if not ticker_match:
            continue
        trade = {
            "ticker": ticker_match.group(1).upper(),
            "direction": ticker_match.group(2).upper(),
            "conviction": ticker_match.group(3).strip().strip("|").strip() or "--",
            "entry": "", "sl": "", "tp": "", "rr": "",
            "notes": [],
        }
        for line in block.split("\n"):
            cells = [c.strip() for c in line.split("|") if c.strip()]
            if len(cells) >= 2:
                label = cells[0].lower()
                val = cells[1]
                if "entry" in label and "stop" not in label:
                    trade["entry"] = _clean_price(val)
                elif "stop" in label or "sl" == label:
                    trade["sl"] = _clean_price(val)
                elif "take profit" in label or "tp" == label or "target" in label:
                    trade["tp"] = _clean_price(val)
                elif "r:r" in label or "r/r" in label or ("risk" in label and "reward" in label):
                    trade["rr"] = val.strip()
            stripped = line.strip()
            if stripped and (stripped[0] in "•*-") and len(stripped) > 2:
                note = stripped.lstrip("•*- ").strip()
                if not note:
                    continue
                # Skip the ticker header itself (e.g. "$AAVE | LONG | ⚡ High Conviction").
                if _TICKER_HEADER_RE.search(note):
                    continue
                if "entry" in note.lower()[:6]:
                    continue
                trade["notes"].append(note)
        if not trade["entry"]:
            m = re.search(r'[Ee]ntry[:\s]*\$?([\d,.]+)', block)
            if m: trade["entry"] = m.group(1)
        if not trade["sl"]:
            m = re.search(r'[Ss]top\s*[Ll]oss[:\s]*\$?([\d,.]+)', block)
            if m: trade["sl"] = m.group(1)
        if not trade["tp"]:
            m = re.search(r'[Tt]ake\s*[Pp]rofit[:\s]*\$?([\d,.]+)', block)
            if m: trade["tp"] = m.group(1)
        if not trade["rr"]:
            m = re.search(r'[Rr][:/][Rr][:\s]*([\d.]+[:\s]*[\d.]*)', block)
            if m: trade["rr"] = m.group(1).strip()
        trades.append(trade)
    return trades

_RISK_HEADING_RE = re.compile(
    # Optional warning emoji, optional bold/italic markers, optional "Session"
    # prefix, "Risk Flag" with optional plural-s, then any combination of
    # trailing punctuation/whitespace (including newlines and bold-close stars).
    # We consume those trailing characters so they don't leak into the body.
    r'(?:⚠️?\s*)?\**\s*(?:[Ss]ession\s+)?[Rr]isk\s+[Ff]lag[s]?\b[\s:.\-*]*',
)
# Stop the body when a clear new section heading appears: a markdown atx heading
# (#…), a bold-on-its-own line, or "Trade Setups"/"Disclaimer" boundaries.
_RISK_BODY_TERMINATOR_RE = re.compile(
    r'\n(?:'
    r'#{1,6}\s+\S'                              # ## Heading
    r'|\*\*[A-Z][^\n]+\*\*\s*\n'                # **Bold Heading** alone
    r'|(?:Trade Setups?|Disclaimer|Notes?|Glossary)\s*[:*\n]'  # known section names
    r')',
    re.MULTILINE,
)


_HR_LINE_RE = re.compile(r'^\s*-{3,}\s*$')


def _strip_hr_edges(body: str) -> str:
    """Trim leading/trailing horizontal-rule lines (and surrounding blank lines)."""
    if not body:
        return body
    lines = body.split("\n")
    while lines and (not lines[0].strip() or _HR_LINE_RE.match(lines[0])):
        lines.pop(0)
    while lines and (not lines[-1].strip() or _HR_LINE_RE.match(lines[-1])):
        lines.pop()
    return "\n".join(lines)


def extract_risk_flag(text: str) -> str | None:
    """Pull out a Session Risk Flag section if present.

    Greedy on body, terminating only at the next clear heading or end of text.
    Tables inside the body are preserved so callers can code-block-wrap them.
    Leading and trailing ``---`` rule lines are stripped — they create an ugly
    empty line at the bottom of the embed.
    """
    if not text:
        return None
    m = _RISK_HEADING_RE.search(text)
    if not m:
        return None
    rest = text[m.end():]
    end = _RISK_BODY_TERMINATOR_RE.search(rest)
    body = rest[: end.start()] if end else rest
    body = _strip_hr_edges(body.strip())
    return body or None

# --- Embed builders ---
def build_trade_embeds(trades: list[dict]) -> list[discord.Embed]:
    """Build trade embeds."""
    rank_emoji = ["🥇", "🥈", "🥉"]
    embeds = []
    for i, t in enumerate(trades[:3]):
        color = COLOR_LONG if t["direction"] == "LONG" else COLOR_SHORT
        emoji = rank_emoji[i] if i < 3 else "📊"
        direction_dot = "🟢" if t["direction"] == "LONG" else "🔴"
        e = discord.Embed(
            title=f"{direction_dot} ${t['ticker']} {t['direction']}",
            color=color,
        )
        if t["conviction"] and t["conviction"] != "--":
            e.description = f"**{t['conviction']}**"
        if t["entry"]:
            e.add_field(name="Entry Price", value=_fmt_price(t["entry"]), inline=True)
        if t["sl"]:
            e.add_field(name="Stop/Loss", value=_fmt_price(t["sl"]), inline=True)
        if t["tp"]:
            e.add_field(name="Take Profit", value=_fmt_price(t["tp"]), inline=True)
        if t["rr"]:
            e.add_field(name="R:R", value=f"{t['rr']}", inline=True)
        if t["notes"]:
            notes_text = "\n".join(f"• {n}" for n in t["notes"][:4])
            e.add_field(name="Context", value=notes_text, inline=False)
        e.set_footer(text=FOOTER)
        embeds.append(e)
    return embeds

def _first_prose_paragraph(text: str) -> str:
    """Return the first non-empty paragraph that isn't a table or bullet list."""
    for para in re.split(r'\n\s*\n', text):
        s = para.strip()
        if not s:
            continue
        lines = [ln.strip() for ln in s.split("\n") if ln.strip()]
        if not lines:
            continue
        # Skip if this paragraph is entirely a table.
        if all(ln.startswith("|") for ln in lines):
            continue
        # Skip pure bullet lists.
        if all(ln[:1] in "-*•" for ln in lines):
            continue
        return s
    return ""


# Stale dates regularly leak from TN's training horizon (e.g. "Apr 19, 2025")
# even when the rest of the analysis is current. Strip them and rely on the
# embed title for the authoritative date.
_STALE_DATE_RE = re.compile(
    r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?,?\s+20\d{2}\b',
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r'\b20\d{2}-\d{2}-\d{2}\b')


def _strip_stale_dates(text: str, replacement: str = "") -> str:
    """Strip 'Mon DD, YYYY' and ISO date strings; collapse the empty lines they leave behind."""
    if not text:
        return text
    text = _STALE_DATE_RE.sub(replacement, text)
    text = _ISO_DATE_RE.sub(replacement, text)
    # Tidy up "Date: " orphans and lonely commas/parens left behind.
    text = re.sub(r'(?im)^\s*(?:as of|date)\s*:\s*\.?\s*$', '', text)
    text = re.sub(r'\(\s*\)', '', text)
    text = re.sub(r'\s+,', ',', text)
    text = re.sub(r' {2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def build_brief_embed(text: str, session: str, phase: str) -> discord.Embed:
    """Build a session brief embed (analysis portion, no trades)."""
    colors = {"asia": COLOR_ASIA, "london": COLOR_LONDON, "us": COLOR_US}
    labels = {"asia": "Asia", "london": "London", "us": "US"}
    color = colors.get(session, COLOR_INFO)
    label = labels.get(session, session.title())
    today = datetime.now(IST).strftime("%b %d, %Y")
    title = f"📊 {label} Session — {phase} · {today}"
    brief_text = text
    trade_start = re.search(r'\$[A-Z]+\s*\|?\s*(?:LONG|SHORT)', text, re.IGNORECASE)
    if trade_start:
        brief_text = text[:trade_start.start()].strip()
    risk_start = _RISK_HEADING_RE.search(brief_text)
    if risk_start:
        brief_text = brief_text[:risk_start.start()].strip()
    brief_text = _strip_stale_dates(brief_text)
    brief_text = dedupe_tn_text(brief_text)
    summary = _first_prose_paragraph(brief_text)
    body = wrap_markdown_tables(brief_text)
    body = _truncate_at_sentence(body, 4000)
    e = discord.Embed(title=title, description=body, color=color)
    if summary and len(summary) <= 1024 and not body.lstrip().startswith(summary[:60]):
        # Promote the regime summary to a top field when it isn't already at the top.
        e.add_field(name="Market Regime", value=summary, inline=False)
    e.set_footer(text=FOOTER)
    e.timestamp = datetime.now(IST)
    return e

def build_risk_embed(risk_text: str) -> discord.Embed:
    """Build a yellow risk flag embed with markdown tables wrapped in code blocks."""
    body = wrap_markdown_tables(risk_text)
    if len(body) > 4000:
        body = body[:3990] + "…"
    e = discord.Embed(
        title="⚠️ Session Risk Flag",
        description=body,
        color=COLOR_RISK,
    )
    e.set_footer(text=FOOTER)
    return e

def build_regime_embed(text: str) -> discord.Embed:
    """Build the daily regime outlook embed."""
    text = wrap_markdown_tables(text)
    if len(text) > 4000:
        text = text[:3990] + "…"
    e = discord.Embed(
        title="🌐 Daily Regime Outlook",
        description=text,
        color=COLOR_REGIME,
    )
    e.set_footer(text=FOOTER)
    e.timestamp = datetime.now(IST)
    return e

# --- Core session brief logic ---
def colors_map(session: str) -> int:
    return {"asia": COLOR_ASIA, "london": COLOR_LONDON, "us": COLOR_US}.get(session, COLOR_INFO)

async def run_session_brief(session: str, phase: str) -> bool:
    """
    Query TrueNorth for a session brief, then:
      - Post analysis embed -> session channel
      - Post trade embeds   -> #trades channel
      - Post risk flag      -> both channels (if present)

    Returns True if the brief was successfully posted to its session channel.
    """
    session_labels = {"asia": "Asia", "london": "London", "us": "US"}
    label = session_labels.get(session, session)
    ch_key = session
    session_channel = bot.get_channel(CH[ch_key])
    trades_channel = bot.get_channel(CH["trades"])
    if not session_channel:
        print(f"[SCHED] Cannot find #{ch_key} channel")
        return False
    prompt = (
        f"Give me a {phase.lower()} brief for the {label} trading session. "
        f"Include: current BTC price and trend, key support/resistance levels, "
        f"top movers with relative strength, volume analysis, and market regime assessment. "
        f"Then provide exactly 3 high-conviction trade setups. For each trade, include: "
        f"ticker with $ prefix, direction (LONG or SHORT), conviction level, "
        f"entry price, stop loss, take profit, and R:R ratio. "
        f"Format each trade as: $TICKER | DIRECTION | Conviction Level, "
        f"then a table with Entry, Stop Loss, Take Profit, R:R rows. "
        f"Add 2-3 bullet points of context per trade. "
        f"If there are major risk events, add a Session Risk Flag section at the end."
    )
    print(f"[SCHED] Querying TN for {label} {phase}...")
    result = await query_truenorth(prompt)
    if not result:
        print(f"[SCHED] No response from TrueNorth for {label} {phase}")
        e = discord.Embed(
            title=f"📊 {label} Session -- {phase}",
            description="⚠️ TrueNorth did not return data. Token may need refresh.\nUse !refreshtoken to check.",
            color=COLOR_RISK,
        )
        e.set_footer(text=FOOTER)
        await session_channel.send(embed=e)
        return False
    brief_embed = build_brief_embed(result, session, phase)
    await session_channel.send(embed=brief_embed)
    trades = parse_trades_from_text(result)
    if trades and trades_channel:
        header = discord.Embed(
            title=f"📈 {label} {phase} -- Trade Setups",
            description=f"3 high-conviction setups from the {label} {phase.lower()} brief.",
            color=colors_map(session),
        )
        header.set_footer(text=FOOTER)
        await trades_channel.send(embed=header)
        trade_embeds = build_trade_embeds(trades)
        for te in trade_embeds:
            await trades_channel.send(embed=te)
    elif trades_channel:
        await trades_channel.send(
            embed=discord.Embed(
                description=f"No structured trades extracted from {label} {phase}. Raw data posted in #{ch_key}.",
                color=COLOR_INFO,
            )
        )
    risk = extract_risk_flag(result)
    if risk:
        risk_embed = build_risk_embed(risk)
        await session_channel.send(embed=risk_embed)
        if trades_channel:
            await trades_channel.send(embed=risk_embed)
    print(f"[SCHED] {label} {phase} posted")
    return True

async def run_regime_update():
    """Daily regime outlook -> #regime-outlook."""
    channel = bot.get_channel(CH["regime"])
    if not channel:
        print("[SCHED] Cannot find #regime-outlook channel")
        return
    prompt = (
        "Give me a comprehensive 24-hour macro regime outlook for crypto and equity markets. "
        "Include: key economic data releases and times, central bank activity, "
        "geopolitical risks, crypto-specific catalysts (unlocks, upgrades, regulatory), "
        "BTC and ETH key levels, dominant market regime (risk-on/off/neutral), "
        "and any cross-asset correlations to watch (DXY, yields, gold, oil). "
        "Be specific with numbers and times."
    )
    print("[SCHED] Querying TN for daily regime outlook...")
    result = await query_truenorth(prompt)
    if not result:
        e = discord.Embed(
            title="🌐 Daily Regime Outlook",
            description="⚠️ TrueNorth did not return data. Token may need refresh.",
            color=COLOR_RISK,
        )
        e.set_footer(text=FOOTER)
        await channel.send(embed=e)
        return
    embed = build_regime_embed(result)
    await channel.send(embed=embed)
    print("[SCHED] Regime outlook posted")

# --- Scheduled jobs ---
SCHEDULE = [
    ("asia",   "Pre-Open Brief",   5,  15),
    ("asia",   "Post-Open Brief",  5,  45),
    ("asia",   "Pre-Close Brief",  13, 15),
    ("asia",   "Post-Close Brief", 13, 45),
    ("london", "Pre-Open Brief",   13, 15),
    ("london", "Post-Open Brief",  13, 45),
    ("london", "Pre-Close Brief",  21, 15),
    ("london", "Post-Close Brief", 21, 45),
    ("us",     "Pre-Open Brief",   18, 15),
    ("us",     "Post-Open Brief",  18, 45),
    ("us",     "Pre-Close Brief",  2,  15),
    ("us",     "Post-Close Brief", 2,  45),
]

def setup_scheduler():
    for session, phase, hour, minute in SCHEDULE:
        scheduler.add_job(
            run_session_brief,
            CronTrigger(hour=hour, minute=minute, timezone=IST),
            args=[session, phase],
            id=f"{session}_{phase.replace(' ', '_').lower()}",
            replace_existing=True,
        )
    scheduler.add_job(
        run_regime_update,
        CronTrigger(hour=5, minute=0, timezone=IST),
        id="daily_regime",
        replace_existing=True,
    )
    scheduler.add_job(
        refresh_tn_token,
        CronTrigger(hour="*/8", minute=0, timezone=IST),
        id="token_refresh",
        replace_existing=True,
    )
    if TN_SESSION_COOKIES and _harvester is not None:
        # Run the cookie harvester preemptively before Privy's 24h expiry.
        scheduler.add_job(
            run_cookie_harvest,
            IntervalTrigger(hours=20),
            id="cookie_harvest",
            replace_existing=True,
            next_run_time=None,  # don't double-run on boot; on_ready already handles boot.
        )

# --- Bot events ---
@bot.event
async def on_ready():
    print(f"DCT TrueNorth Bot online as {bot.user}")
    if not token_store["refresh"]:
        print("[BOOT] WARNING: TN_REFRESH_TOKEN is not set. Auto token refresh disabled — "
              "the bot will break when TN_TOKEN expires (~24h). "
              "Set TN_REFRESH_TOKEN in Railway to enable auto-refresh.")
    else:
        print("[BOOT] TN_REFRESH_TOKEN present; attempting startup refresh...")
        await refresh_tn_token()
    exp = decode_jwt_exp(token_store.get("access", ""))
    print(f"[BOOT] TN access token exp: {exp}")
    if TN_SESSION_COOKIES and _harvester is not None:
        print("[BOOT] TN_SESSION_COOKIES present; running cookie harvester…")
        await run_cookie_harvest()
    elif TN_SESSION_COOKIES and _harvester is None:
        print("[BOOT] TN_SESSION_COOKIES set but harvester module missing — skipping")
    # Acquire thread (env → cache → API create). Returns None when nothing works.
    boot_tid = await ensure_thread_id_async()
    if boot_tid:
        print(f"[BOOT] Validating TN thread id={boot_tid} via ping…")
        if await validate_thread(boot_tid, timeout_s=30.0):
            print(f"[BOOT] TN thread is valid: {boot_tid}")
        else:
            invalidate_thread(
                f"ping against {boot_tid} returned no chunks within 30s — "
                "TN does not recognise this thread"
            )
            await alert_thread_invalid()
    else:
        invalidate_thread(
            "TN_THREAD_ID is unset, no cached thread, and the API create probe "
            "found no working endpoint"
        )
        await alert_thread_invalid()
    setup_scheduler()
    scheduler.start()
    print(f"[SCHED] {len(SCHEDULE)} session briefs + regime + token refresh scheduled")
    ch = bot.get_channel(CH["claude"])
    if ch:
        e = discord.Embed(title="🤖 DCT TrueNorth Bot is online!", color=COLOR_INFO)
        e.description = "Connected to TrueNorth AI + Claude."
        e.add_field(
            name="💬 #claude-integration",
            value=(
                "Default: ask anything → streams direct from TrueNorth in a thread.\n"
                "Prefix `!` (e.g. `!btc vs eth 7d`) → Claude Sonnet pulls multiple TN angles and synthesizes (3/hr/user).\n"
                "Stay in the thread for follow-ups (context preserved)."
            ),
            inline=False,
        )
        e.add_field(
            name="📋 Session Channels",
            value="Auto briefs 15 min before/after each session open & close.",
            inline=False,
        )
        e.add_field(name="🌐 #regime-outlook", value="Daily macro & regime update at 5:00 AM IST.", inline=False)
        e.add_field(name="🎯 #trades", value="3 high-conviction setups per session brief.", inline=False)
        e.add_field(
            name="⌨️ Manual Commands",
            value="!brief asia/london/us · !regime · !trades · !refreshtoken · !health · !setcreds (owner)",
            inline=False,
        )
        e.set_footer(text=FOOTER)
        await ch.send(embed=e)

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    await bot.process_commands(message)

    in_claude = message.channel.id == CH["claude"]
    in_claude_thread = (
        isinstance(message.channel, discord.Thread)
        and message.channel.parent_id == CH["claude"]
    )
    if not (in_claude or in_claude_thread):
        return

    content = message.content.strip()
    if not content:
        return
    if any(content.lower().startswith(c) for c in KNOWN_COMMANDS):
        return

    if content.startswith("!"):
        free = content[1:].strip()
        if not free:
            return
        await handle_sonnet(message, free)
    else:
        await handle_direct_tn(message, content)

# --- Manual commands ---
def _channel_mention(channel_id: int) -> str:
    """Best-effort channel mention; falls back to a #name or raw id if cache miss."""
    ch = bot.get_channel(channel_id)
    if ch is None:
        return f"<#{channel_id}>"
    name = getattr(ch, "name", None)
    return f"<#{channel_id}>" if name is None else f"#{name}"


@bot.command(name="brief")
async def manual_brief(ctx: commands.Context, session: str = "all"):
    """Manually trigger a session brief. Usage: !brief asia/london/us/all

    Posts the analysis to the relevant session channel and trade setups to
    #trades. Sends a short confirmation back to the invoking channel.
    """
    session = session.lower()
    valid = ["asia", "london", "us"]
    if session == "all":
        targets = valid
    elif session in valid:
        targets = [session]
    else:
        await ctx.send("Usage: !brief asia/london/us/all")
        return
    label_map = {"asia": "Asia", "london": "London", "us": "US"}
    target_names = ", ".join(label_map[s] for s in targets)
    await ctx.send(f"⏳ Fetching {target_names} brief{'s' if len(targets) > 1 else ''}…")
    posted: list[str] = []
    failed: list[str] = []
    for s in targets:
        ok = await run_session_brief(s, "Manual Brief")
        if ok:
            posted.append(_channel_mention(CH[s]))
        else:
            failed.append(label_map.get(s, s))
    trades_mention = _channel_mention(CH["trades"])
    if posted:
        msg = f"✅ Brief posted to {', '.join(posted)} and {trades_mention}"
        if failed:
            msg += f"\n⚠️ Failed: {', '.join(failed)}. Run `!health` for diagnostics."
        await ctx.send(msg)
    else:
        await ctx.send(
            f"⚠️ No briefs were posted ({', '.join(failed) or 'unknown error'}). "
            "Run `!health` for diagnostics."
        )

@bot.command(name="trades")
async def manual_trades(ctx: commands.Context):
    """Manually trigger trade setups."""
    await ctx.send("⏳ Querying TrueNorth for trade setups...")
    prompt = (
        "Give me 3 high-conviction trade setups for right now based on current market conditions. "
        "For each trade provide: ticker with $ prefix, direction (LONG or SHORT), conviction level, "
        "entry price, stop loss, take profit, R:R ratio. "
        "Format each as: $TICKER | DIRECTION | Conviction Level, "
        "then Entry, Stop Loss, Take Profit, R:R as labeled rows. "
        "Add 2-3 context bullets per trade. "
        "Include a Session Risk Flag if relevant."
    )
    result = await query_truenorth(prompt)
    if not result:
        e = discord.Embed(
            title="📊 Trade Setups",
            description="⚠️ No response from TrueNorth. Token may need refresh.\nUse !refreshtoken to check.",
            color=COLOR_RISK,
        )
        e.set_footer(text=FOOTER)
        await ctx.send(embed=e)
        return
    trades = parse_trades_from_text(result)
    if trades:
        embeds = build_trade_embeds(trades)
        for e in embeds:
            await ctx.send(embed=e)
    else:
        e = discord.Embed(
            title="📊 Trade Setups",
            description=f"```\n{result[:3900]}\n```",
            color=COLOR_US,
        )
        e.set_footer(text=FOOTER)
        await ctx.send(embed=e)
    risk = extract_risk_flag(result)
    if risk:
        await ctx.send(embed=build_risk_embed(risk))

@bot.command(name="regime")
async def manual_regime(ctx: commands.Context):
    """Manually trigger regime outlook."""
    await ctx.send("⏳ Querying TrueNorth for regime outlook...")
    await run_regime_update()

@bot.command(name="winrate")
async def winrate(ctx: commands.Context):
    """Show win rate stats (placeholder)."""
    e = discord.Embed(
        title="📊 Win Rate Tracker",
        description="Trade tracking coming soon. Historical win rates will be calculated from closed positions.",
        color=COLOR_INFO,
    )
    e.set_footer(text=FOOTER)
    await ctx.send(embed=e)

async def _privy_refresh_verbose() -> tuple[bool, list[tuple[str, int, str]]]:
    """Run every refresh variant and report (ok, [(name, status, body_preview), ...])."""
    rt = token_store["refresh"]
    at = token_store.get("access", "")
    if not rt:
        return False, [("(no token)", 0, "TN_REFRESH_TOKEN not set")]
    attempts: list[tuple[str, int, str]] = []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for variant in _privy_refresh_variants(rt, at):
                name = variant["name"]
                try:
                    status, body_preview, data = await _privy_attempt(
                        client, variant["body"], variant["headers"],
                    )
                except Exception as e:
                    attempts.append((name, 0, f"{type(e).__name__}: {e}"))
                    continue
                attempts.append((name, status, body_preview))
                if status != 200 or not isinstance(data, dict):
                    continue
                new_access = (
                    data.get("token") or data.get("access_token")
                    or data.get("identity_token") or data.get("privy_access_token")
                )
                new_refresh = data.get("refresh_token") or data.get("privy_refresh_token")
                if not new_access:
                    continue
                token_store["access"] = new_access
                if new_refresh:
                    token_store["refresh"] = new_refresh
                _save_token_cache(token_store["access"], token_store["refresh"])
                global _privy_refresh_variant
                _privy_refresh_variant = name
                return True, attempts
    except Exception as e:
        attempts.append(("(fatal)", 0, f"{type(e).__name__}: {e}"))
    return False, attempts


_UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')


def _is_valid_thread_id(tid: str) -> bool:
    """Stricter than uuid.UUID() so we don't accept formatted-but-wrong ids."""
    return bool(tid) and bool(_UUID_RE.match(tid.strip()))


def _is_jwt_shape(token: str) -> tuple[bool, str]:
    """Check JWT-ish structure (3 dot-separated segments + decodable payload).

    Returns (ok, error_message). Non-empty error_message explains why it failed.
    """
    if not token or not isinstance(token, str):
        return False, "empty token"
    token = token.strip()
    parts = token.split(".")
    if len(parts) != 3:
        return False, f"expected 3 dot-separated segments, got {len(parts)}"
    payload_b64 = parts[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    try:
        json.loads(base64.urlsafe_b64decode(payload_b64.encode()))
    except Exception as e:
        return False, f"payload not valid base64-JSON: {type(e).__name__}"
    return True, ""


def _parse_setcreds_payload(raw: str) -> tuple[dict | None, str]:
    """Accept either JSON blob OR three space-separated values.

    Returns (dict with access_token/refresh_token/thread_id, error_message).
    """
    s = raw.strip()
    if not s:
        return None, "empty payload"
    if s.startswith("{"):
        try:
            obj = json.loads(s)
        except Exception as e:
            return None, f"JSON parse failed: {type(e).__name__}: {e}"
        if not isinstance(obj, dict):
            return None, "JSON must be an object"
        for key in ("access_token", "refresh_token", "thread_id"):
            if key not in obj or not isinstance(obj[key], str) or not obj[key].strip():
                return None, f"missing or empty field: {key}"
        return {k: obj[k].strip() for k in ("access_token", "refresh_token", "thread_id")}, ""
    # Space-separated form
    parts = s.split()
    if len(parts) < 3:
        return None, f"expected 3 space-separated values (access_token, refresh_token, thread_id); got {len(parts)}"
    return {
        "access_token": parts[0].strip(),
        "refresh_token": parts[1].strip(),
        "thread_id": parts[2].strip(),
    }, ""


def _is_owner(ctx_or_id) -> bool:
    """Return True iff the caller is the configured Pranay Discord ID."""
    if not PRANAY_DISCORD_ID:
        return False
    user_id = ctx_or_id.author.id if hasattr(ctx_or_id, "author") else ctx_or_id
    try:
        return int(user_id) == int(PRANAY_DISCORD_ID)
    except (TypeError, ValueError):
        return False


@bot.command(name="setcreds")
async def manual_setcreds(ctx: commands.Context, *, raw: str = ""):
    """Owner-only. Update TN access + refresh + thread_id at runtime.

    Usage:
      !setcreds <access_token> <refresh_token> <thread_id>
      !setcreds {"access_token":"...","refresh_token":"...","thread_id":"..."}

    The original message is deleted on success so JWTs don't sit in chat history.
    """
    if not _is_owner(ctx):
        try:
            await ctx.message.delete()
        except Exception:
            pass
        await ctx.send("⛔ `!setcreds` is restricted to the bot owner.")
        return

    payload, err = _parse_setcreds_payload(raw)
    if not payload:
        await _reply_and_try_delete(
            ctx, f"⚠️ `!setcreds` parse error: {err}. "
                 f"Use JSON blob or `!setcreds <access> <refresh> <thread_id>`.",
        )
        return

    access = payload["access_token"]
    refresh = payload["refresh_token"]
    thread_id = payload["thread_id"]

    # Validate inputs
    ok, jwt_err = _is_jwt_shape(access)
    if not ok:
        await _reply_and_try_delete(ctx, f"⚠️ access_token rejected: {jwt_err}")
        return
    if len(refresh) < 20:
        await _reply_and_try_delete(
            ctx, f"⚠️ refresh_token looks too short (len={len(refresh)}). "
                 "Make sure you copied the full `privy:refresh_token` value.",
        )
        return
    if not _is_valid_thread_id(thread_id):
        await _reply_and_try_delete(
            ctx, f"⚠️ thread_id `{thread_id[:40]}` is not a valid UUID. "
                 "Grab it from the /streams request body in DevTools.",
        )
        return

    # Delete the user's message FIRST (security — don't leave JWTs in chat).
    await _try_delete_message(ctx.message)

    # Apply to in-memory state and persist token+thread caches.
    global _tn_thread_current, _privy_refresh_variant
    token_store["access"] = access
    token_store["refresh"] = refresh
    _save_token_cache(access, refresh)
    _tn_thread_current = thread_id
    save_cached_thread(thread_id)
    tn_state["thread_invalid"] = False
    tn_state["thread_invalid_reason"] = ""
    tn_state["boot_warning_posted"] = False

    progress = await ctx.send("🔐 Credentials updated. Validating against TrueNorth…")
    ok_ping = await validate_thread(thread_id, timeout_s=30.0)
    exp = decode_jwt_exp(access)
    exp_str = exp.strftime("%Y-%m-%d %H:%M %Z") if exp else "unknown"

    if ok_ping:
        await progress.edit(
            content=f"✅ Credentials updated. Thread valid. Access token exp: `{exp_str}`."
        )
    else:
        # Ping failed — thread likely still invalid.
        invalidate_thread("ping against new thread_id still returned no chunks")
        await progress.edit(
            content=(
                "⚠️ Credentials stored but thread ping returned 0 chunks in 30s. "
                "Double-check the `thread_id` from the /streams request body. "
                f"Access token exp: `{exp_str}`."
            )
        )


async def _try_delete_message(message: discord.Message) -> bool:
    """Best-effort message delete. Returns True on success."""
    try:
        await message.delete()
        return True
    except discord.Forbidden:
        print("[setcreds] bot lacks Manage Messages permission to delete the creds message")
    except discord.HTTPException as e:
        print(f"[setcreds] delete failed: {e}")
    except Exception as e:
        print(f"[setcreds] delete unexpected error: {type(e).__name__}: {e}")
    return False


async def _reply_and_try_delete(ctx: commands.Context, message: str) -> None:
    """Send `message` and attempt to delete the originating command message."""
    await _try_delete_message(ctx.message)
    await ctx.send(message)


@bot.command(name="refreshtoken")
async def manual_refresh(ctx: commands.Context):
    """Manually refresh TrueNorth token and report every Privy variant attempt."""
    await ctx.send("🔄 Attempting token refresh (probing variants)…")
    ok, attempts = await _privy_refresh_verbose()

    diag = discord.Embed(
        title=("✅ Privy refresh OK" if ok else "⚠️ Privy refresh FAILED"),
        color=(COLOR_LONG if ok else COLOR_RISK),
    )
    diag.add_field(name="Refresh token", value=_mask(token_store["refresh"]), inline=True)
    if _privy_refresh_variant:
        diag.add_field(name="Variant in use", value=f"`{_privy_refresh_variant}`", inline=True)
    # One field per variant attempt; truncate body to keep embed under limits.
    for name, status, body in attempts[:5]:
        body_short = body[:300].replace("`", "ʼ")
        diag.add_field(
            name=f"{name} → {status}",
            value=f"```\n{body_short}\n```" if body else "(no body)",
            inline=False,
        )
    exp = decode_jwt_exp(token_store.get("access", ""))
    diag.add_field(
        name="Access token exp",
        value=(exp.strftime("%Y-%m-%d %H:%M %Z") if exp else "unknown"),
        inline=False,
    )
    diag.set_footer(text=FOOTER)
    await ctx.send(embed=diag)

    test = await query_truenorth("ping")
    if test:
        await ctx.send("✅ TrueNorth responded on ping -- token is valid.")
    else:
        await ctx.send(
            "❌ TrueNorth did not respond even after refresh.\n"
            "If Privy status was 401/400, `TN_REFRESH_TOKEN` is likely stale. "
            "Copy `privy:refresh_token` from localStorage at app.true-north.xyz "
            "into Railway env and redeploy. Run `!health` for more."
        )


def _format_delta(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    now = datetime.now(IST)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        return dt.strftime("%Y-%m-%d %H:%M %Z")
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m ago"
    return f"{secs // 86400}d ago"


@bot.command(name="health")
async def manual_health(ctx: commands.Context):
    """Report bot health diagnostics."""
    now = datetime.now(IST)
    uptime_secs = int((now - BOT_STARTED_AT).total_seconds())
    d, rem = divmod(uptime_secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    uptime_str = f"{d}d {h}h {m}m" if d else f"{h}h {m}m"

    exp = decode_jwt_exp(token_store.get("access", ""))
    if exp:
        exp_delta = int((exp - now).total_seconds())
        if exp_delta > 0:
            exp_str = f"{exp.strftime('%Y-%m-%d %H:%M %Z')} (in {exp_delta // 3600}h {(exp_delta % 3600) // 60}m)"
        else:
            exp_str = f"{exp.strftime('%Y-%m-%d %H:%M %Z')} (EXPIRED {(-exp_delta) // 60}m ago)"
    else:
        exp_str = "unknown (token missing or malformed)"

    e = discord.Embed(title="🩺 Bot Health", color=COLOR_INFO)
    e.add_field(name="Uptime", value=uptime_str, inline=True)
    e.add_field(name="Thread ID", value=f"`{_tn_thread_current or 'unset'}`", inline=True)
    e.add_field(name="Refresh token", value=("set" if token_store["refresh"] else "NOT SET"), inline=True)
    e.add_field(name="Access token exp", value=exp_str, inline=False)
    e.add_field(name="Last TN success", value=_format_delta(tn_state["last_success_at"]), inline=True)
    e.add_field(name="Last TN error", value=_format_delta(tn_state["last_error_at"]), inline=True)
    if tn_state["last_error"]:
        err_preview = str(tn_state["last_error"])[:400]
        e.add_field(name="Error detail", value=f"```\n{err_preview}\n```", inline=False)
    e.set_footer(text=FOOTER)
    e.timestamp = now
    await ctx.send(embed=e)

# --- Entry point ---
if __name__ == "__main__":
    print("[BOOT] Starting DCT TrueNorth Bot...")
    bot.run(DISCORD_TOKEN)
