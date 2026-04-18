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
from zoneinfo import ZoneInfo

# --- Config ---
DISCORD_TOKEN    = os.environ["DISCORD_BOT_TOKEN"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
TN_TOKEN         = os.environ.get("TN_TOKEN", "")
TN_REFRESH       = os.environ.get("TN_REFRESH_TOKEN", "")
PRIVY_APP_ID     = os.environ.get("PRIVY_APP_ID", "cm6afcumv0688a6x3r78jkx7v")

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
KNOWN_COMMANDS = ("!brief", "!trades", "!regime", "!winrate", "!refreshtoken", "!health")

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
    "last_success_at": None,   # datetime | None
    "last_error_at": None,     # datetime | None
    "last_error": None,        # str | None
    "last_alert_at": 0.0,      # time.time() float
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

def ensure_thread_id() -> str:
    """Return the current TrueNorth thread id, falling back to env → cache → new UUID."""
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
    _tn_thread_current = str(uuid.uuid4())
    save_cached_thread(_tn_thread_current)
    print(f"[Thread] Created new thread id: {_tn_thread_current}")
    return _tn_thread_current

def rotate_thread_id() -> str:
    """Discard the current shared thread and mint a new one (persisted)."""
    global _tn_thread_current
    _tn_thread_current = str(uuid.uuid4())
    save_cached_thread(_tn_thread_current)
    print(f"[Thread] Rotated shared thread to: {_tn_thread_current}")
    return _tn_thread_current

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
async def _tn_call_once(prompt: str, thread_id: str, timeout_read: float) -> tuple[str, int, str]:
    """One SSE call. Returns (text, status_code, error_snippet). Raises on network errors."""
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
    timeout = httpx.Timeout(30.0, read=timeout_read)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", TN_ENDPOINT, headers=headers, json=body) as resp:
            status = resp.status_code
            print(f"[TN HTTP] status={status}")
            if status != 200:
                body_text = ""
                async for chunk in resp.aiter_text():
                    body_text += chunk
                    if len(body_text) > 500:
                        break
                err_snippet = body_text[:500]
                print(f"[TN HTTP] error body: {err_snippet}")
                return "", status, err_snippet
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        obj = json.loads(data)
                        if obj.get("event_type") == "llm_output":
                            inner = obj.get("data", {})
                            content = inner.get("content", "")
                            if content:
                                chunks.append(content)
                        elif "content" in obj:
                            chunks.append(obj["content"])
                        elif "detail" in obj:
                            snippet = json.dumps(obj["detail"])[:500]
                            if not err_snippet:
                                err_snippet = snippet
                            print(f"[TN SSE] API error: {snippet[:300]}")
                    except json.JSONDecodeError as e:
                        print(f"[TN SSE] JSON error: {e} | raw: {data[:100]}")
    return "".join(chunks).strip(), status, err_snippet


def _classify_error(status: int, err_snippet: str) -> tuple[bool, bool]:
    """Return (is_auth_error, is_thread_error) from a status/error snippet."""
    low = (err_snippet or "").lower()
    auth = status in (401, 403) or any(m in low for m in AUTH_ERROR_MARKERS)
    thread = any(m in low for m in THREAD_ERROR_MARKERS) or (
        "access" in low and "thread" in low
    )
    return auth, thread


async def query_truenorth(
    prompt: str,
    thread_id: str | None = None,
    timeout_read: float = 180.0,
) -> str:
    """Send prompt to TrueNorth; auto-refresh token and rotate thread on recoverable errors."""
    caller_pinned_thread = thread_id is not None
    tid = thread_id or ensure_thread_id()

    text, status, err, exc = "", 0, "", None
    try:
        text, status, err = await _tn_call_once(prompt, tid, timeout_read)
    except Exception as e:
        exc = e
        print(f"[TN ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()

    if not text:
        auth_fail, thread_fail = _classify_error(status, err)
        should_retry = False
        if auth_fail:
            print("[TN] auth failure detected -- attempting token refresh")
            if await refresh_tn_token():
                should_retry = True
            else:
                print("[TN] token refresh failed")
        if thread_fail:
            if caller_pinned_thread:
                tid = str(uuid.uuid4())
                print(f"[TN] thread denied -- retrying with fresh per-call uuid {tid}")
            else:
                tid = rotate_thread_id()
            should_retry = True
        if should_retry:
            try:
                text, status, err = await _tn_call_once(prompt, tid, timeout_read)
                exc = None
            except Exception as e:
                exc = e
                print(f"[TN ERROR retry] {type(e).__name__}: {e}")

    if text:
        tn_state["last_success_at"] = datetime.now(IST)
        print(f"[TN RESULT] {len(text)} chars (last_success_at updated)")
        return text

    reason = err or (f"{type(exc).__name__}: {exc}" if exc else f"http status={status}")
    tn_state["last_error"] = reason
    tn_state["last_error_at"] = datetime.now(IST)
    await alert_tn_failure(reason)
    return ""


async def alert_tn_failure(reason: str) -> None:
    """Post a rate-limited alert embed to #claude-integration on unrecovered TN failure."""
    now = time.time()
    if now - tn_state["last_alert_at"] < ALERT_COOLDOWN_SEC:
        return
    tn_state["last_alert_at"] = now
    try:
        channel = bot.get_channel(CH["claude"])
        if not channel:
            return
        e = discord.Embed(
            title="🚨 TrueNorth failure (auto-recovery did not succeed)",
            description=f"```\n{reason[:1500]}\n```",
            color=COLOR_RISK,
        )
        e.add_field(name="Thread", value=(_tn_thread_current or "—"), inline=True)
        exp = decode_jwt_exp(token_store.get("access", ""))
        e.add_field(
            name="Token exp",
            value=(exp.strftime("%Y-%m-%d %H:%M %Z") if exp else "unknown"),
            inline=True,
        )
        e.set_footer(text=f"{FOOTER} · alert rate-limited 1/hr")
        e.timestamp = datetime.now(IST)
        await channel.send(embed=e)
    except Exception as e:
        print(f"[Alert] send failed: {type(e).__name__}: {e}")

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
def _mask(tok: str) -> str:
    """Return a truncated token for log safety (first 8 + last 4 chars)."""
    if not tok:
        return "<empty>"
    if len(tok) < 16:
        return f"{tok[:4]}…"
    return f"{tok[:8]}…{tok[-4:]} (len={len(tok)})"


async def refresh_tn_token() -> bool:
    """Refresh TN access token via Privy. Returns True on success, False otherwise.

    Logs the HTTP status, response keys, and (truncated) body on every path so
    the next failure is fully diagnosable in Railway logs.
    """
    rt = token_store["refresh"]
    if not rt:
        print("[TokenRefresh] TN_REFRESH_TOKEN not set -- skipping")
        return False
    print(f"[TokenRefresh] POST {PRIVY_SESSION_URL} refresh={_mask(rt)} app_id={PRIVY_APP_ID}")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                PRIVY_SESSION_URL,
                json={"refresh_token": rt},
                headers={
                    "privy-app-id": PRIVY_APP_ID,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "privy-client": "react-auth:2.0.0",
                },
            )
            status = resp.status_code
            body_preview = resp.text[:500]
            print(f"[TokenRefresh] status={status} body={body_preview}")
            if status != 200:
                return False
            try:
                data = resp.json()
            except Exception as e:
                print(f"[TokenRefresh] JSON parse failed: {e}")
                return False
            if not isinstance(data, dict):
                print(f"[TokenRefresh] unexpected response type: {type(data).__name__}")
                return False
            print(f"[TokenRefresh] response keys: {sorted(data.keys())}")
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
                print("[TokenRefresh] 200 OK but no access-token field found in response")
                return False
            token_store["access"] = new_access
            if new_refresh:
                token_store["refresh"] = new_refresh
                print(f"[TokenRefresh] refresh token rotated: {_mask(new_refresh)}")
            _save_token_cache(token_store["access"], token_store["refresh"])
            exp = decode_jwt_exp(token_store["access"])
            print(f"[TokenRefresh] ok; access exp={exp}")
            return True
    except Exception as e:
        print(f"[TokenRefresh] {type(e).__name__}: {e}")
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

def get_tn_thread(conv_id: int) -> str:
    tid = tn_thread_map.get(conv_id)
    if not tid:
        tid = str(uuid.uuid4())
        tn_thread_map[conv_id] = tid
    return tid

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


def strip_token_tags(text: str) -> str:
    """Replace <Token ... tokenSymbol="X" ... /> with $X; drop tags with no symbol."""
    def _sub(m: re.Match) -> str:
        sym = _TOKEN_SYMBOL_ATTR_RE.search(m.group(0))
        return f"${sym.group(1).upper()}" if sym else ""
    text = _TOKEN_TAG_RE.sub(_sub, text)
    # Drop any stray closing </Token>
    text = re.sub(r'</\s*Token\s*>', '', text, flags=re.IGNORECASE)
    return text


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

def extract_risk_flag(text: str) -> str | None:
    """Pull out a risk flag section if present."""
    patterns = [
        r'(?:⚠️?\s*)?[Ss]ession\s+[Rr]isk\s+[Ff]lag[:\s]*(.*?)(?:\n-{3,}|\Z)',
        r'(?:⚠️?\s*)?[Rr]isk\s+[Ff]lag[:\s]*(.*?)(?:\n-{3,}|\Z)',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.DOTALL)
        if m:
            return m.group(1).strip()
    return None

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

def build_brief_embed(text: str, session: str, phase: str) -> discord.Embed:
    """Build a session brief embed (analysis portion, no trades)."""
    colors = {"asia": COLOR_ASIA, "london": COLOR_LONDON, "us": COLOR_US}
    labels = {"asia": "Asia", "london": "London", "us": "US"}
    color = colors.get(session, COLOR_INFO)
    label = labels.get(session, session.title())
    title = f"📊 {label} Session -- {phase}"
    brief_text = text
    trade_start = re.search(r'\$[A-Z]+\s*\|?\s*(?:LONG|SHORT)', text, re.IGNORECASE)
    if trade_start:
        brief_text = text[:trade_start.start()].strip()
    risk_start = re.search(r'(?:⚠️?\s*)?[Ss]ession\s+[Rr]isk\s+[Ff]lag', brief_text)
    if risk_start:
        brief_text = brief_text[:risk_start.start()].strip()
    if len(brief_text) > 4000:
        brief_text = brief_text[:3990] + "…"
    e = discord.Embed(title=title, description=brief_text, color=color)
    e.set_footer(text=FOOTER)
    e.timestamp = datetime.now(IST)
    return e

def build_risk_embed(risk_text: str) -> discord.Embed:
    """Build a yellow risk flag embed."""
    e = discord.Embed(
        title="⚠️ Session Risk Flag",
        description=risk_text[:4000],
        color=COLOR_RISK,
    )
    e.set_footer(text=FOOTER)
    return e

def build_regime_embed(text: str) -> discord.Embed:
    """Build the daily regime outlook embed."""
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

async def run_session_brief(session: str, phase: str):
    """
    Query TrueNorth for a session brief, then:
      - Post analysis embed -> session channel
      - Post trade embeds   -> #trades channel
      - Post risk flag      -> both channels (if present)
    """
    session_labels = {"asia": "Asia", "london": "London", "us": "US"}
    label = session_labels.get(session, session)
    ch_key = session
    session_channel = bot.get_channel(CH[ch_key])
    trades_channel = bot.get_channel(CH["trades"])
    if not session_channel:
        print(f"[SCHED] Cannot find #{ch_key} channel")
        return
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
        return
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
    print(f"[BOOT] TN thread id: {ensure_thread_id()}")
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
            value="!brief asia/london/us · !regime · !trades · !refreshtoken · !health",
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
@bot.command(name="brief")
async def manual_brief(ctx: commands.Context, session: str = "all"):
    """Manually trigger a session brief. Usage: !brief asia/london/us/all"""
    session = session.lower()
    valid = ["asia", "london", "us"]
    if session == "all":
        targets = valid
    elif session in valid:
        targets = [session]
    else:
        await ctx.send("Usage: !brief asia/london/us/all")
        return
    await ctx.send(f"⏳ Fetching brief{'s' if len(targets) > 1 else ''}...")
    for s in targets:
        await run_session_brief(s, "Manual Brief")

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

async def _privy_refresh_verbose() -> tuple[bool, int, str, list[str]]:
    """Run refresh once and return (ok, status, body_preview, response_keys) for Discord display."""
    rt = token_store["refresh"]
    if not rt:
        return False, 0, "TN_REFRESH_TOKEN not set", []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                PRIVY_SESSION_URL,
                json={"refresh_token": rt},
                headers={
                    "privy-app-id": PRIVY_APP_ID,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "privy-client": "react-auth:2.0.0",
                },
            )
    except Exception as e:
        return False, 0, f"{type(e).__name__}: {e}", []
    status = resp.status_code
    body = resp.text[:400]
    keys: list[str] = []
    if status == 200:
        try:
            data = resp.json()
            if isinstance(data, dict):
                keys = sorted(data.keys())
                new_access = (
                    data.get("token")
                    or data.get("access_token")
                    or data.get("identity_token")
                    or data.get("privy_access_token")
                )
                new_refresh = data.get("refresh_token") or data.get("privy_refresh_token")
                if new_access:
                    token_store["access"] = new_access
                    if new_refresh:
                        token_store["refresh"] = new_refresh
                    _save_token_cache(token_store["access"], token_store["refresh"])
                    return True, status, body, keys
        except Exception as e:
            body = f"(json decode failed: {type(e).__name__}: {e}) {body}"
    return False, status, body, keys


@bot.command(name="refreshtoken")
async def manual_refresh(ctx: commands.Context):
    """Manually refresh TrueNorth token and report the Privy response verbatim."""
    await ctx.send("🔄 Attempting token refresh...")
    ok, status, body, keys = await _privy_refresh_verbose()

    diag = discord.Embed(
        title=("✅ Privy refresh OK" if ok else "⚠️ Privy refresh FAILED"),
        color=(COLOR_LONG if ok else COLOR_RISK),
    )
    diag.add_field(name="HTTP status", value=str(status) if status else "no response", inline=True)
    diag.add_field(name="Refresh token", value=_mask(token_store["refresh"]), inline=True)
    if keys:
        diag.add_field(name="Response keys", value=f"`{', '.join(keys)}`", inline=False)
    if body:
        diag.add_field(name="Body (first 400 chars)", value=f"```\n{body}\n```", inline=False)
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
