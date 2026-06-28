"""
DCT TrueNorth Bot — Discord bridge for TrueNorth AI trading intelligence (v2).

TrueNorth v2 retired the conversational SSE agent the v1 bot was built on.
This rewrite uses TrueNorth's keyless agent-tools REST API (the same backend
the official `@truenorth-ai/cli` wraps) for raw market data, and Claude for
narrative synthesis:

    TN agent-tools (TA / derivatives / scanner / events)  ──▶  snapshot (JSON)
    snapshot  ──▶  Claude (claude-opus-4-8)  ──▶  session brief / regime text
    text      ──▶  Discord embed

What it does:
  * Posts a pre-open market brief 15 minutes before each market opens —
    Asia (Tokyo 09:00 JST), London (LSE 08:00 UK), US (NYSE 09:30 ET).
    Cron jobs run in each market's local timezone so DST is automatic.
  * Posts a macro regime outlook Mon/Wed/Fri 06:00 IST.
  * Manual commands: !brief, !regime, !health.
  * Serves GET /healthz on $PORT for Railway liveness.

Gone from v1 (TrueNorth v2 made them obsolete):
  * Privy token refresh, token/thread caches, Mac-local harvester,
    /credentials webhook — the agent-tools API needs no auth for crypto data.
  * #claude-integration chat + Sonnet middleman, #trades channel posting.
    Trade setups now live on the dashboard (DASHBOARD_URL), linked from briefs.

Channels (routed via env vars):
  #asia-session       · CH_ASIA_SESSION
  #london-session     · CH_LONDON_SESSION
  #us-session         · CH_US_SESSION
  #regime-outlook     · CH_REGIME_OUTLOOK
  ops alerts          · CH_OPS (optional — falls back to logs only)
"""

from __future__ import annotations

import os
import re
import json
import asyncio
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
import httpx
import anthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# aiohttp serves GET /healthz so Railway's web-service health check passes.
from aiohttp import web

# --- Config ---
DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

IST = ZoneInfo("Asia/Kolkata")

CH = {
    "asia":   int(os.environ["CH_ASIA_SESSION"]),
    "london": int(os.environ["CH_LONDON_SESSION"]),
    "us":     int(os.environ["CH_US_SESSION"]),
    "regime": int(os.environ["CH_REGIME_OUTLOOK"]),
}
# Optional ops channel for failure alerts. Unset → alerts go to logs only
# (the community channels never see plumbing noise).
CH_OPS = int(os.environ["CH_OPS"]) if os.environ.get("CH_OPS", "").strip() else None

# Public dashboard with top setups + screener. When set, every brief links to it.
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").strip()

# TrueNorth agent-tools REST API. Keyless for crypto tools; TN_AUTH_TOKEN
# (raw Authorization header value) unlocks app-gated tools (indices, Polymarket,
# KOL, meme) if/when the TN team issues one.
TN_TOOLS_BASE = os.environ.get("TN_TOOLS_BASE", "https://api.adventai.io/api/agent-tools").rstrip("/")
TN_AUTH_TOKEN = os.environ.get("TN_AUTH_TOKEN", "").strip()
TN_DEBUG = os.environ.get("TN_DEBUG", "").lower() in ("1", "true", "yes")

WEBHOOK_PORT = int(os.environ.get("PORT", "8080"))

SYNTH_MODEL = os.environ.get("SYNTH_MODEL", "claude-opus-4-8")

# Channels where !setup is allowed (comma-separated IDs). Empty ⇒ allowed in
# every channel; the 60s/user cooldown is the spam guard either way. Gating
# exists because each !setup is a paid Claude call.
SETUP_ALLOWED_CHANNELS: set[int] = {
    int(c) for c in os.environ.get("SETUP_ALLOWED_CHANNELS", "").replace(" ", "").split(",") if c
}

# Optional: after a brief posts to Discord, ghostwrite a tweet and save it as a
# Typefully DRAFT (never published/scheduled). Off unless AUTO_DRAFT_ENABLED is
# truthy AND a key is set. Failures here never affect the brief.
AUTO_DRAFT_ENABLED = os.environ.get("AUTO_DRAFT_ENABLED", "").lower() in ("1", "true", "yes")
TYPEFULLY_API_KEY = os.environ.get("TYPEFULLY_API_KEY", "").strip()

# Outcome tracking: !setup trades are logged to {CACHE_PATH}/setups.json and a
# background job resolves them to WIN/LOSS/EXPIRED for a public track record.
# Point CACHE_PATH at a mounted Railway volume to persist across restarts; if it
# isn't mounted, the file is ephemeral and history resets on redeploy (handled
# gracefully at startup with a warning).
CACHE_PATH = (os.environ.get("CACHE_PATH", ".").rstrip("/") or ".")
SETUPS_PATH = os.path.join(CACHE_PATH, "setups.json")
SETUP_EXPIRY_HOURS = 48

# --- Embed colors ---
COLOR_RISK   = 0xFFAA00
COLOR_ASIA   = 0x00BFFF
COLOR_LONDON = 0xFF8C00
COLOR_US     = 0x7B68EE
COLOR_REGIME = 0x9B59B6
COLOR_INFO   = 0x2F3136
COLOR_LONG   = 0x2ECC71
COLOR_SHORT  = 0xE74C3C
COLOR_SCAN   = 0x3498DB

FOOTER = "DCT TrueNorth Bot · Powered by TrueNorth AI"

# --- Bot setup ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
claude_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_KEY)
scheduler = AsyncIOScheduler(timezone=IST)

# --- Observability state ---
BOT_STARTED_AT: datetime = datetime.now(IST)
tn_state: dict = {
    "last_tool_success_at": None,   # datetime | None — any TN tool call OK
    "last_tool_error_at": None,     # datetime | None
    "last_tool_error": None,        # str | None
    "last_synth_error": None,       # str | None — Claude-side failure
    "last_brief_at": {},            # {"asia"|"london"|"us": datetime}
    "last_regime_at": None,         # datetime | None
    "drafts_created": 0,            # int — Typefully drafts made this process
    "last_draft_error": None,       # str | None — Typefully/ghostwriter failure
}

# In-memory authoritative list of tracked setups (mirrors setups.json on disk).
_SETUPS: list[dict] = []


# =============================================================================
# TrueNorth agent-tools client
# =============================================================================

class TNToolError(Exception):
    """A TrueNorth tool call failed or returned an error result."""

    def __init__(self, message: str, app_only: bool = False):
        super().__init__(message)
        self.app_only = app_only


def _parse_tool_envelope(payload) -> dict | list:
    """Unwrap the agent-tools /call response envelope. Pure — unit-tested.

    Success shape:  {"data": {"toolName": ..., "result": ..., "isError": false}}
    Failure shapes: {"success": false, "message": ...} / {"failed": true, ...}
    App-gated tools return a *successful* envelope whose result is
    {"status": "app_only", ...} — surfaced as TNToolError(app_only=True) so
    callers can degrade gracefully instead of feeding a stub to Claude.
    """
    if not isinstance(payload, dict):
        raise TNToolError(f"envelope is not a JSON object: {type(payload).__name__}")
    if payload.get("failed") or payload.get("success") is False:
        raise TNToolError(str(payload.get("message") or "API returned failure"))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise TNToolError("envelope missing 'data' object")
    if data.get("isError"):
        raise TNToolError(f"tool error: {json.dumps(data.get('result'))[:300]}")
    result = data.get("result")
    if isinstance(result, dict) and result.get("status") == "app_only":
        raise TNToolError(
            f"{data.get('toolName', 'tool')} is app-gated (needs TN_AUTH_TOKEN)",
            app_only=True,
        )
    return result


TN_CALL_ATTEMPTS = 2
TN_CALL_BACKOFF_S = 5.0


async def tn_call(tool: str, args: dict, timeout_read: float = 120.0):
    """Call one TrueNorth tool. Retries once on network errors / 5xx.

    A new httpx client per call — TN calls are infrequent (a handful per brief)
    so connection pooling buys nothing and per-call clients avoid shared state.
    """
    headers = {"Content-Type": "application/json"}
    if TN_AUTH_TOKEN:
        headers["Authorization"] = TN_AUTH_TOKEN
    body = {"toolName": tool, "arguments": args}
    last_exc: Exception | None = None
    for attempt in range(1, TN_CALL_ATTEMPTS + 1):
        if attempt > 1:
            await asyncio.sleep(TN_CALL_BACKOFF_S)
        try:
            timeout = httpx.Timeout(15.0, read=timeout_read)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{TN_TOOLS_BASE}/call", headers=headers, json=body)
            if resp.status_code >= 500:
                last_exc = TNToolError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                print(f"[TN] {tool} attempt={attempt} -> HTTP {resp.status_code}, retrying")
                continue
            if resp.status_code != 200:
                raise TNToolError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            result = _parse_tool_envelope(resp.json())
            tn_state["last_tool_success_at"] = datetime.now(IST)
            if TN_DEBUG:
                print(f"[TN] {tool} ok ({len(json.dumps(result, default=str))} bytes)")
            return result
        except TNToolError:
            raise
        except Exception as e:
            last_exc = e
            print(f"[TN] {tool} attempt={attempt} {type(e).__name__}: {e}")
    raise TNToolError(f"{tool} failed after {TN_CALL_ATTEMPTS} attempts: {last_exc}")


async def tn_call_safe(tool: str, args: dict, **kw):
    """tn_call that records failures and returns None instead of raising.

    Snapshot gathering tolerates individual tool failures — a brief built from
    4 of 5 sources beats no brief. app_only failures are logged quietly (they
    are expected until a TN_AUTH_TOKEN exists).
    """
    try:
        return await tn_call(tool, args, **kw)
    except TNToolError as e:
        if not e.app_only:
            tn_state["last_tool_error"] = f"{tool}: {e}"
            tn_state["last_tool_error_at"] = datetime.now(IST)
        print(f"[TN] {tool} unavailable: {e}")
        return None


# =============================================================================
# Market snapshot → Claude prompt
# =============================================================================

def compact_json(obj, limit: int) -> str:
    """Serialize a tool result for the prompt, hard-capped at `limit` chars."""
    if obj is None:
        return "(unavailable)"
    try:
        s = json.dumps(obj, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        s = str(obj)
    if len(s) > limit:
        return s[:limit] + "…[truncated]"
    return s


# Tool name → (args, char budget in the prompt). BTC carries the regime read so
# it gets the deepest data; the rest are context.
BRIEF_SOURCES = [
    ("basic_market_info",    {"token_address": "bitcoin"},                      1500),
    ("technical_analysis",   {"token_address": "bitcoin", "timeframe": "4h,1d"}, 7000),
    ("technical_analysis_eth", {"token_address": "ethereum", "timeframe": "4h"}, 4000),
    ("derivatives_analysis", {"token_address": "bitcoin"},                      5000),
    ("performance_scanner",  {"top": 10, "lookback_days": 1},                   4000),
    ("events",               {"query": "crypto", "time_window": "24h", "sort_by": "relevance"}, 3500),
]

# Regime outlook adds longer-horizon sources. market_index_price is app-gated
# today — tn_call_safe degrades it to "(unavailable)" until TN_AUTH_TOKEN lands.
REGIME_EXTRA_SOURCES = [
    ("market_index_price",   {"index": "all"},                                  1500),
    ("events_macro",         {"query": "macro economy fed rates", "time_window": "7d", "sort_by": "relevance"}, 3500),
    ("performance_scanner_7d", {"top": 10, "lookback_days": 7},                 4000),
]


def _source_tool_name(key: str) -> str:
    """Map a source key to the actual TN tool (keys may be suffixed for uniqueness)."""
    for suffix in ("_eth", "_macro", "_7d"):
        if key.endswith(suffix):
            return key[: -len(suffix)]
    return key


async def gather_snapshot(sources: list[tuple]) -> dict[str, str]:
    """Fetch all sources in parallel; return {key: compacted JSON or '(unavailable)'}."""
    keys = [k for k, _a, _b in sources]
    results = await asyncio.gather(
        *(tn_call_safe(_source_tool_name(k), a) for k, a, _b in sources)
    )
    return {
        key: compact_json(result, budget)
        for (key, _args, budget), result in zip(sources, results)
    }


SYNTH_SYSTEM = """You write market briefs for Corgi Calls, a crypto trading Discord community. You are given a JSON data snapshot pulled live from TrueNorth AI's market-data tools and must synthesize it into a brief.

Rules:
- Use ONLY numbers present in the snapshot. Never invent prices, levels, percentages, or events. If a data section is "(unavailable)", skip what depends on it without apologizing.
- Tone: direct, trader-native. Funding, OI, R:R, HTF, VWAP are fine. No marketing speak, no hedging filler, no "as an AI".
- Format for a Discord embed: short **bold** section headers, tight bullet points, plain prose. NO markdown tables. No preamble, no sign-off — start directly with the first section.
- Stay under 3300 characters."""


def build_brief_prompt(session: str, snapshot: dict[str, str]) -> str:
    """Prompt for one session's pre-open brief."""
    labels = {
        "asia": "Asia (Tokyo opens 09:00 JST)",
        "london": "London (LSE opens 08:00 UK)",
        "us": "US (NYSE opens 09:30 ET)",
    }
    now_utc = datetime.now(ZoneInfo("UTC"))
    sections = "\n".join(f"### {k}\n{v}" for k, v in snapshot.items())
    return (
        f"Write the pre-open market brief for the {labels.get(session, session)} trading session.\n"
        f"Current time: {now_utc.strftime('%Y-%m-%d %H:%M')} UTC. The market opens in ~15 minutes.\n\n"
        "Structure:\n"
        "1. **Regime** — one-line market regime read (risk-on/off/neutral and why).\n"
        "2. **BTC** — price, trend, momentum, and the key support/resistance levels from the TA data.\n"
        "3. **ETH** — same, shorter.\n"
        "4. **Positioning** — funding, open interest, liquidation zones from the derivatives data.\n"
        "5. **Movers** — top relative-strength names from the scanner, one line each, max 5.\n"
        "6. **Watch** — events/catalysts in the next 24h that matter for this session. Skip if nothing notable.\n\n"
        f"Live data snapshot:\n{sections}"
    )


def build_regime_prompt(snapshot: dict[str, str]) -> str:
    """Prompt for the Mon/Wed/Fri macro regime outlook."""
    now_utc = datetime.now(ZoneInfo("UTC"))
    sections = "\n".join(f"### {k}\n{v}" for k, v in snapshot.items())
    return (
        "Write the macro regime outlook for the next 24-48 hours, crypto-centric.\n"
        f"Current time: {now_utc.strftime('%Y-%m-%d %H:%M')} UTC.\n\n"
        "Structure:\n"
        "1. **Regime** — dominant market regime (risk-on/off/neutral) and the evidence.\n"
        "2. **BTC & ETH** — key levels and the higher-timeframe picture.\n"
        "3. **Cross-asset** — indices/DXY/yields read IF index data is present in the snapshot; otherwise skip entirely.\n"
        "4. **Rotation** — where 7-day relative strength is concentrating.\n"
        "5. **Catalysts** — dated events ahead (macro prints, crypto-specific). Only events from the snapshot.\n\n"
        f"Live data snapshot:\n{sections}"
    )


def response_text(content_blocks) -> str:
    """Join the text blocks of a Claude response (skips thinking blocks)."""
    return "".join(b.text for b in content_blocks if getattr(b, "type", "") == "text").strip()


async def synthesize(prompt: str) -> str:
    """One Claude call: snapshot prompt → brief text. Raises on failure."""
    try:
        resp = await claude_client.messages.create(
            model=SYNTH_MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=SYNTH_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        tn_state["last_synth_error"] = f"{type(e).__name__}: {e}"
        raise
    text = response_text(resp.content)
    if not text:
        tn_state["last_synth_error"] = f"empty response (stop_reason={resp.stop_reason})"
        raise RuntimeError(tn_state["last_synth_error"])
    return text


# =============================================================================
# Trade setup engine (!setup) — single-asset variant of the brief pipeline
# =============================================================================

# TrueNorth tools key off CoinGecko-style ids ("bitcoin"), not tickers ("BTC").
# A bare ticker returns an empty-but-"success" payload, so map the common ones
# and pass anything unrecognized through lowercased (lets power users type a
# full id like "curve-dao-token"). Unresolved input is caught downstream when
# basic_market_info returns without a price.
TICKER_TO_ID = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin",
    "XRP": "ripple", "ADA": "cardano", "DOGE": "dogecoin", "AVAX": "avalanche-2",
    "LINK": "chainlink", "DOT": "polkadot", "MATIC": "matic-network", "POL": "matic-network",
    "TON": "the-open-network", "TRX": "tron", "SUI": "sui", "APT": "aptos",
    "ARB": "arbitrum", "OP": "optimism", "INJ": "injective-protocol", "SEI": "sei-network",
    "LTC": "litecoin", "BCH": "bitcoin-cash", "ATOM": "cosmos", "NEAR": "near",
    "UNI": "uniswap", "AAVE": "aave", "CRV": "curve-dao-token", "MKR": "maker",
    "LDO": "lido-dao", "PENDLE": "pendle", "ENA": "ethena", "HYPE": "hyperliquid",
    "TAO": "bittensor", "FET": "fetch-ai", "RENDER": "render-token", "WLD": "worldcoin-wld",
    "FIL": "filecoin", "STX": "blockstack", "XLM": "stellar", "XMR": "monero",
    "ZEC": "zcash", "ONDO": "ondo-finance", "PAXG": "pax-gold", "JUP": "jupiter-exchange-solana",
    "PEPE": "pepe", "WIF": "dogwifcoin", "BONK": "bonk", "FARTCOIN": "fartcoin",
    "PUMP": "pump-fun", "SPX": "spx6900", "TRUMP": "official-trump",
}


def _resolve_token(arg: str) -> str:
    """Map a user ticker to a TrueNorth token id; pass ids through lowercased."""
    return TICKER_TO_ID.get(arg.strip().upper(), arg.strip().lower())


SETUP_SYSTEM = """You are a trade setup generator. Given the following market data for {ticker}, produce a structured trade setup with: Direction (LONG/SHORT), Entry Zone, Stop Loss, Take Profit 1, Take Profit 2, R:R ratio, Conviction (Low/Medium/High), and a 1-2 sentence reasoning. Use only the data provided. Never invent numbers. If the data doesn't support a clean setup, say 'No clear setup — ranging/choppy conditions.'

Output ONLY a single JSON object — no prose, no markdown fences — with exactly these keys:
{{"has_setup": boolean, "direction": "LONG" | "SHORT" | "NONE", "entry_zone": string, "stop_loss": string, "take_profit_1": string, "take_profit_2": string, "rr_ratio": string, "conviction": "Low" | "Medium" | "High" | "None", "reasoning": string}}

Price levels are strings in the asset's own price precision (e.g. "$62,400 – $62,900" or "$0.1840"). When there is no clean setup: set has_setup=false, direction="NONE", every price level and rr_ratio to "—", conviction="None", and put the ranging/choppy message in reasoning."""


def _parse_setup_json(text: str) -> dict:
    """Parse the model's JSON, tolerating ``` fences or stray prose around it."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.IGNORECASE).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            return json.loads(t[start : end + 1])
        raise


async def synthesize_setup(ticker: str, snapshot: dict[str, str]) -> dict:
    """One Claude call: single-asset snapshot → structured trade setup dict."""
    sections = "\n".join(f"### {k}\n{v}" for k, v in snapshot.items())
    prompt = f"Market data for {ticker}:\n{sections}"
    try:
        resp = await claude_client.messages.create(
            model=SYNTH_MODEL,
            max_tokens=2000,
            thinking={"type": "adaptive"},
            system=SETUP_SYSTEM.format(ticker=ticker),
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        tn_state["last_synth_error"] = f"{type(e).__name__}: {e}"
        raise
    text = response_text(resp.content)
    if not text:
        tn_state["last_synth_error"] = f"empty setup response (stop_reason={resp.stop_reason})"
        raise RuntimeError(tn_state["last_synth_error"])
    return _parse_setup_json(text)


def _setup_channel_allowed(channel_id: int) -> bool:
    """Gate !setup by SETUP_ALLOWED_CHANNELS; empty set ⇒ allowed everywhere."""
    return not SETUP_ALLOWED_CHANNELS or channel_id in SETUP_ALLOWED_CHANNELS


# =============================================================================
# Typefully auto-draft (optional) — a posted brief → a tweet draft to review
# =============================================================================

TWEET_SYSTEM = (
    "You are @corgil_'s tweet ghostwriter. Given this market brief data, write a "
    "single standalone tweet (under 260 chars). Style: lowercase, casual, crypto-native. "
    "Lead with the most interesting data point. End with a question to drive replies. "
    "No hashtags. No links. No emojis except sparingly. Example tone: 'btc holding 96k "
    "into US open but funding is starting to heat up. this is where the last 3 squeezes "
    "started. anyone else seeing this?'"
)

TYPEFULLY_DRAFTS_URL = "https://api.typefully.com/v1/drafts/"


def _clean_tweet(text: str) -> str:
    """Strip a model's stray quotes / code fences and cap to the platform limit."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
    if len(t) >= 2 and t[0] in "\"'" and t[-1] == t[0]:
        t = t[1:-1].strip()
    return t[:280]


async def synthesize_tweet(brief_text: str) -> str:
    """Ghostwrite one tweet from an already-synthesized brief (same model/key).

    The brief narrative already contains the regime read, BTC level, funding and
    the standout mover, so we hand it straight to the ghostwriter and let it pick
    the most interesting point — no brittle re-parsing of free text.
    """
    try:
        resp = await claude_client.messages.create(
            model=SYNTH_MODEL,
            max_tokens=600,
            system=TWEET_SYSTEM,
            messages=[{"role": "user", "content": f"Market brief data:\n{brief_text[:2000]}"}],
        )
    except anthropic.APIError as e:
        tn_state["last_synth_error"] = f"{type(e).__name__}: {e}"
        raise
    return _clean_tweet(response_text(resp.content))


async def create_typefully_draft(text: str) -> str | None:
    """POST a Typefully v1 draft. Returns a share URL/id, or None on any failure.

    Never raises and never schedules/publishes — the brief is the deliverable,
    the draft is a bonus. v1 auth is `X-API-KEY: Bearer <key>`; a few keys want
    the raw value, so on an auth rejection we retry once without the prefix.
    """
    raw = TYPEFULLY_API_KEY
    if raw.lower().startswith("bearer "):
        raw = raw[len("bearer "):].strip()
    body = {"content": text, "threadify": False}  # no schedule-date/share ⇒ stays a draft
    last_err = None
    for header_value in (f"Bearer {raw}", raw):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
                resp = await client.post(
                    TYPEFULLY_DRAFTS_URL,
                    headers={"X-API-KEY": header_value, "Content-Type": "application/json"},
                    json=body,
                )
            if resp.status_code in (200, 201):
                data = resp.json() if resp.content else {}
                draft_id = data.get("id")
                return (
                    data.get("share_url")
                    or data.get("url")
                    or (f"https://typefully.com/?d={draft_id}" if draft_id else "draft created")
                )
            if resp.status_code in (401, 403):
                last_err = f"HTTP {resp.status_code} (auth)"
                continue  # try the other X-API-KEY format
            last_err = f"HTTP {resp.status_code}: {resp.text[:160]}"
            break  # non-auth error — don't retry
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            break
    tn_state["last_draft_error"] = last_err
    print(f"[AUTODRAFT] Typefully draft failed: {last_err}")
    return None


async def maybe_autodraft(brief_text: str, label: str) -> None:
    """Best-effort: brief → ghostwritten tweet → Typefully draft. Fully gated and
    self-contained; any failure is logged and swallowed so the brief is unaffected."""
    if not AUTO_DRAFT_ENABLED:
        return
    if not TYPEFULLY_API_KEY:
        print("[AUTODRAFT] AUTO_DRAFT_ENABLED set but TYPEFULLY_API_KEY missing — skipping")
        return
    try:
        tweet = await synthesize_tweet(brief_text)
    except Exception as e:
        tn_state["last_draft_error"] = f"tweet gen: {type(e).__name__}"
        print(f"[AUTODRAFT] {label} tweet generation failed: {type(e).__name__}: {e}")
        return
    if not tweet:
        return
    url = await create_typefully_draft(tweet)
    if url:
        tn_state["drafts_created"] += 1
        print(f"[AUTODRAFT] {label} → draft created: {url}")


# =============================================================================
# Embed builders
# =============================================================================

_HEADER_LINE_RE = re.compile(r'^\s*(?:\d+[\.\)]|#{1,6})\s')
# A line that is JUST a section number with no content (e.g. "5." or "5...."),
# usually left behind when the truncate cuts mid-header.
_DANGLING_HEADER_RE = re.compile(r'^\s*\d+[\.\)]+\s*\d*\s*$')


def _strip_dangling_header(text: str) -> str:
    """Remove a trailing line that is just a numbered header (no body)."""
    if not text:
        return text
    lines = text.rstrip().split("\n")
    while lines and _DANGLING_HEADER_RE.match(lines[-1]):
        lines.pop()
    return "\n".join(lines).rstrip()


def _truncate_at_sentence(text: str, limit: int) -> str:
    """Truncate to <=limit chars, preferring the last sentence boundary.

    If the chosen cut would leave a dangling section-header line behind
    (e.g. ``"5."`` from a numbered list), back up to the previous sentence
    boundary so the embed doesn't end with an orphan header.
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
        last_line = snippet.rsplit("\n", 1)[-1]
        if _HEADER_LINE_RE.match(last_line):
            continue
        return _strip_dangling_header(snippet) + "…"

    return _strip_dangling_header(cut.rstrip()) + "…"


SESSION_COLORS = {"asia": COLOR_ASIA, "london": COLOR_LONDON, "us": COLOR_US}
SESSION_LABELS = {"asia": "Asia", "london": "London", "us": "US"}


def build_brief_embed(text: str, session: str, phase: str = "Pre-Open Brief") -> discord.Embed:
    """Session brief embed: synthesized narrative + dashboard link."""
    label = SESSION_LABELS.get(session, session.title())
    today = datetime.now(IST).strftime("%b %d, %Y")
    e = discord.Embed(
        title=f"📊 {label} Session — {phase} · {today}",
        description=_truncate_at_sentence(text, 4000),
        color=SESSION_COLORS.get(session, COLOR_INFO),
    )
    if DASHBOARD_URL:
        e.add_field(
            name="🎯 Top Setups",
            value=f"[Live setups & market screener]({DASHBOARD_URL})",
            inline=False,
        )
    e.set_footer(text=FOOTER)
    e.timestamp = datetime.now(IST)
    return e


def build_regime_embed(text: str) -> discord.Embed:
    e = discord.Embed(
        title="🌐 Macro Regime Outlook",
        description=_truncate_at_sentence(text, 4000),
        color=COLOR_REGIME,
    )
    if DASHBOARD_URL:
        e.add_field(
            name="🎯 Top Setups",
            value=f"[Live setups & market screener]({DASHBOARD_URL})",
            inline=False,
        )
    e.set_footer(text=FOOTER)
    e.timestamp = datetime.now(IST)
    return e


def _failure_embed(title: str, reason: str) -> discord.Embed:
    e = discord.Embed(title=title, description=f"⚠️ {reason[:1500]}", color=COLOR_RISK)
    e.set_footer(text=FOOTER)
    e.timestamp = datetime.now(IST)
    return e


def _fmt_spot(price: float) -> str:
    return f"${price:,.2f}" if price >= 1 else f"${price:.6g}"


def build_setup_embed(ticker: str, price: float, s: dict) -> discord.Embed:
    """Trade-setup embed: green LONG / red SHORT / amber no-setup, levels in fields."""
    direction = str(s.get("direction") or "NONE").upper()
    has_setup = bool(s.get("has_setup")) and direction in ("LONG", "SHORT")
    if direction == "LONG":
        color, tag = COLOR_LONG, "🟢 LONG"
    elif direction == "SHORT":
        color, tag = COLOR_SHORT, "🔴 SHORT"
    else:
        color, tag = COLOR_RISK, "🟡 No clear setup"
    e = discord.Embed(title=f"🎯 {ticker} Trade Setup", color=color)
    reasoning = str(s.get("reasoning") or "").strip()[:600]
    e.description = f"**{tag}** · Spot {_fmt_spot(price)}\n\n{reasoning}"
    if has_setup:
        e.add_field(name="Entry Zone", value=str(s.get("entry_zone", "—"))[:200], inline=True)
        e.add_field(name="Stop Loss", value=str(s.get("stop_loss", "—"))[:200], inline=True)
        e.add_field(name="R : R", value=str(s.get("rr_ratio", "—"))[:200], inline=True)
        e.add_field(name="Take Profit 1", value=str(s.get("take_profit_1", "—"))[:200], inline=True)
        e.add_field(name="Take Profit 2", value=str(s.get("take_profit_2", "—"))[:200], inline=True)
        e.add_field(name="Conviction", value=str(s.get("conviction", "—"))[:200], inline=True)
    e.set_footer(text=FOOTER + " · not financial advice")
    e.timestamp = datetime.now(IST)
    return e


def _clean_ticker(t: str | None) -> str:
    """'CRVUSDT' → 'CRV'. Strips the quote-currency / perp suffix."""
    t = (t or "?").upper()
    for suffix in ("USDT", "USD", "PERP"):
        if t.endswith(suffix) and len(t) > len(suffix):
            return t[: -len(suffix)]
    return t


def _scan_cell(val, width: int, pct: bool = True) -> str:
    """Right-aligned ANSI-colored number (green ≥0, red <0) for the scan table.

    Padding is applied before the color codes so the visible columns stay aligned
    (the escape sequences are zero-width when Discord renders the ansi block).
    """
    if val is None:
        return "—".rjust(width)
    s = (f"{val:+.1f}%" if pct else f"{val:+.1f}").rjust(width)
    code = "32" if val >= 0 else "31"
    return f"\x1b[{code}m{s}\x1b[0m"


def build_scan_embed(scanner: dict, count: int) -> discord.Embed:
    """Top-N relative-strength table (ANSI-colored) from performance_scanner.

    No Claude call — pure formatting of one TrueNorth tool result.
    """
    lb = (scanner or {}).get("leaderboard", [])[:count]
    header = f"{'#':>3}  {'Asset':<9}{'24h':>9}{'7d':>9}{'RS':>8}"
    lines = [header]
    for row in lb:
        tk = _clean_ticker(row.get("ticker") or row.get("token"))[:9]
        lines.append(
            f"{row.get('rank', 0):>3}  {tk:<9}"
            + _scan_cell(row.get("momentum1D"), 9)
            + _scan_cell(row.get("momentum7D"), 9)
            + _scan_cell(row.get("rsVsBenchmark"), 8, pct=False)
        )
    table = "```ansi\n" + "\n".join(lines) + "\n```"
    now = datetime.now(IST)
    e = discord.Embed(
        title=f"🔍 Market Scanner — Top {len(lb)} by Relative Strength",
        description="Top movers by 7-day strength vs BTC · 30 HL perps\n" + table,
        color=COLOR_SCAN,
    )
    e.set_footer(text=f"Data from TrueNorth | Refreshed {now:%b %d, %H:%M IST}")
    e.timestamp = now
    return e


async def alert_ops(title: str, detail: str) -> None:
    """Failure alert: CH_OPS channel if configured, Railway logs always."""
    print(f"[ALERT] {title}: {detail}")
    if CH_OPS is None:
        return
    try:
        channel = bot.get_channel(CH_OPS)
        if channel:
            await channel.send(embed=_failure_embed(title, detail))
    except Exception as e:
        print(f"[ALERT] ops send failed: {type(e).__name__}: {e}")


# =============================================================================
# Setup outcome tracking — public win/loss track record for !setup trades
# =============================================================================

def _extract_numbers(s: str) -> list[float]:
    """Pull numeric values out of a level string like '$62,400 – $62,900'."""
    out = []
    for tok in re.findall(r"\d[\d,]*(?:\.\d+)?", s or ""):
        try:
            out.append(float(tok.replace(",", "")))
        except ValueError:
            pass
    return out


def _parse_price(s: str) -> float | None:
    nums = _extract_numbers(s)
    return nums[0] if nums else None


def _parse_zone_midpoint(s: str) -> float | None:
    """Entry zones are ranges ('$62,400 – $62,900') → midpoint; singles → value."""
    nums = _extract_numbers(s)
    if not nums:
        return None
    return (nums[0] + nums[1]) / 2 if len(nums) >= 2 else nums[0]


def _levels_coherent(direction: str, entry: float, stop: float, tp1: float) -> bool:
    """Reject incoherent levels that would resolve instantly (e.g. LONG tp1<entry)."""
    if direction == "LONG":
        return stop < entry < tp1
    if direction == "SHORT":
        return tp1 < entry < stop
    return False


def _money(x: float | None) -> str:
    if x is None:
        return "—"
    if x >= 1000:
        return f"${x:,.0f}"
    if x >= 1:
        return f"${x:,.2f}"
    return f"${x:.4g}"


def _signed_pct(p: float | None) -> str:
    return "—" if p is None else f"{p:+.1f}%"


def _trade_pct(setup: dict, price: float | None) -> float | None:
    """Direction-adjusted P&L %: positive = trade in profit, negative = loss."""
    if price is None:
        return None
    raw = (price - setup["entry_price"]) / setup["entry_price"] * 100
    return raw if setup["direction"] == "LONG" else -raw


def _elapsed_str(start_iso: str, end_iso: str | None = None) -> str:
    try:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso) if end_iso else datetime.now(IST)
    except (TypeError, ValueError):
        return "—"
    secs = int((end - start).total_seconds())
    if secs < 3600:
        return f"{max(secs, 0) // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h"


def _persist_setups() -> None:
    """Atomic write of _SETUPS to disk (.tmp + fsync + os.replace). Best-effort."""
    try:
        os.makedirs(CACHE_PATH, exist_ok=True)
        tmp = SETUPS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"version": 1, "setups": _SETUPS}, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SETUPS_PATH)
    except Exception as e:
        print(f"[TRACK] WARNING: failed to persist setups: {type(e).__name__}: {e}")


def init_setups() -> None:
    """Load setups.json at startup. Missing file ⇒ start fresh + warn (covers an
    unmounted Railway volume where the file doesn't survive restarts)."""
    global _SETUPS
    try:
        if os.path.exists(SETUPS_PATH):
            with open(SETUPS_PATH) as f:
                data = json.load(f)
            _SETUPS = data.get("setups", []) if isinstance(data, dict) else (data or [])
            open_n = sum(1 for s in _SETUPS if s.get("status") == "OPEN")
            print(f"[TRACK] loaded {len(_SETUPS)} setups ({open_n} open) from {SETUPS_PATH}")
        else:
            _SETUPS = []
            print(f"[TRACK] WARNING: {SETUPS_PATH} not found — starting fresh. If CACHE_PATH "
                  f"is not a mounted volume, track record will reset on each restart.")
            _persist_setups()
    except Exception as e:
        _SETUPS = []
        print(f"[TRACK] WARNING: could not load setups ({type(e).__name__}: {e}) — starting fresh.")


def log_setup(setup: dict, ticker: str, coingecko_id: str, message) -> None:
    """Record a LONG/SHORT !setup for outcome tracking. No-ops on 'no clear setup'
    or unparseable/incoherent levels (the embed still posts; it's just not tracked)."""
    direction = str(setup.get("direction") or "NONE").upper()
    if not setup.get("has_setup") or direction not in ("LONG", "SHORT"):
        return
    entry = _parse_zone_midpoint(setup.get("entry_zone"))
    stop = _parse_price(setup.get("stop_loss"))
    tp1 = _parse_price(setup.get("take_profit_1"))
    tp2 = _parse_price(setup.get("take_profit_2"))
    if entry is None or stop is None or tp1 is None:
        print(f"[TRACK] {ticker}: levels unparseable — not tracking")
        return
    if not _levels_coherent(direction, entry, stop, tp1):
        print(f"[TRACK] {ticker}: incoherent {direction} levels (entry={entry} stop={stop} tp1={tp1}) — not tracking")
        return
    _SETUPS.append({
        "ticker": ticker, "coingecko_id": coingecko_id, "direction": direction,
        "entry_price": entry, "stop_loss": stop, "tp1": tp1, "tp2": tp2,
        "rr_ratio": str(setup.get("rr_ratio", "")), "conviction": str(setup.get("conviction", "")),
        "timestamp": datetime.now(IST).isoformat(),
        "discord_message_id": message.id, "discord_channel_id": message.channel.id,
        "status": "OPEN", "resolution_price": None, "resolved_at": None,
        "result_pct": None, "outcome_label": None,
    })
    _persist_setups()
    print(f"[TRACK] logged {ticker} {direction} entry={_money(entry)} stop={_money(stop)} tp1={_money(tp1)}")


def evaluate_setup(setup: dict, price: float | None):
    """Return (status, label) if an OPEN setup resolved, else None.
    WIN/LOSS need a price; EXPIRED is purely time-based (48h)."""
    if price is not None:
        if setup["direction"] == "LONG":
            if price >= setup["tp1"]:
                return ("WIN", "TP1")
            if price <= setup["stop_loss"]:
                return ("LOSS", "SL")
        else:  # SHORT
            if price <= setup["tp1"]:
                return ("WIN", "TP1")
            if price >= setup["stop_loss"]:
                return ("LOSS", "SL")
    try:
        age_h = (datetime.now(IST) - datetime.fromisoformat(setup["timestamp"])).total_seconds() / 3600
    except (TypeError, ValueError):
        age_h = 0
    if age_h >= SETUP_EXPIRY_HOURS:
        return ("EXPIRED", "EXPIRED")
    return None


COLOR_EXPIRED = 0x808080


def build_resolution_embed(s: dict) -> discord.Embed:
    """Follow-up embed posted as a reply when a tracked setup resolves."""
    d = s["direction"].title()  # Long / Short
    entry, res, pct = s["entry_price"], s.get("resolution_price"), s.get("result_pct")
    elapsed = _elapsed_str(s["timestamp"], s.get("resolved_at"))
    if s["status"] == "WIN":
        title = "✅ TP1 HIT"
        desc = f"**{s['ticker']} {d}** from {_money(entry)} → {_money(res)} ({_signed_pct(pct)})\n⏱ {elapsed} since entry"
        color = COLOR_LONG
    elif s["status"] == "LOSS":
        title = "❌ STOPPED"
        desc = f"**{s['ticker']} {d}** from {_money(entry)} → SL {_money(s['stop_loss'])} hit ({_signed_pct(pct)})\n⏱ {elapsed} since entry"
        color = COLOR_SHORT
    else:  # EXPIRED
        title = "⏰ EXPIRED"
        tail = f" (last {_signed_pct(pct)})" if pct is not None else ""
        desc = f"**{s['ticker']} {d}** from {_money(entry)} → no trigger in {SETUP_EXPIRY_HOURS}h{tail}"
        color = COLOR_EXPIRED
    e = discord.Embed(title=title, description=desc, color=color)
    e.set_footer(text=FOOTER + " · tracked setup · not financial advice")
    e.timestamp = datetime.now(IST)
    return e


async def _post_resolution(s: dict) -> None:
    """Reply to the original !setup message with the resolution; fall back to a
    plain channel post if the original message is gone."""
    embed = build_resolution_embed(s)
    channel = bot.get_channel(s["discord_channel_id"])
    if channel is None:
        print(f"[TRACK] channel {s['discord_channel_id']} not found — can't post {s['ticker']} resolution")
        return
    try:
        original = await channel.fetch_message(s["discord_message_id"])
        await original.reply(embed=embed)
    except Exception as ex:
        print(f"[TRACK] reply failed ({type(ex).__name__}) — posting unthreaded")
        try:
            await channel.send(embed=embed)
        except Exception as ex2:
            print(f"[TRACK] channel send also failed: {type(ex2).__name__}: {ex2}")


async def track_setups() -> None:
    """Every 15 min: price-check OPEN setups, resolve + announce any that hit."""
    open_setups = [s for s in _SETUPS if s.get("status") == "OPEN"]
    if not open_setups:
        return
    changed = False
    for s in open_setups:
        info = await tn_call_safe("basic_market_info", {"token_address": s["coingecko_id"]})
        price = (info or {}).get("market_data", {}).get("current_price")
        outcome = evaluate_setup(s, price)
        if outcome is None:
            continue
        status, label = outcome
        s["status"] = status
        s["outcome_label"] = label
        s["resolution_price"] = price
        s["resolved_at"] = datetime.now(IST).isoformat()
        s["result_pct"] = _trade_pct(s, price)
        changed = True
        print(f"[TRACK] {s['ticker']} {s['direction']} → {status} ({label}) at {_money(price)}")
        await _post_resolution(s)
    if changed:
        _persist_setups()


def compute_winrate(setups: list[dict]) -> dict:
    """Pure stats over the tracked setups for !winrate."""
    wins = [s for s in setups if s.get("status") == "WIN"]
    losses = [s for s in setups if s.get("status") == "LOSS"]
    expired = [s for s in setups if s.get("status") == "EXPIRED"]
    open_n = sum(1 for s in setups if s.get("status") == "OPEN")
    decided = len(wins) + len(losses)
    pcts = [s["result_pct"] for s in setups if s.get("result_pct") is not None]
    realized_rr = []
    for s in wins:
        risk = abs(s["entry_price"] - s["stop_loss"])
        if risk > 0 and s.get("resolution_price") is not None:
            realized_rr.append(abs(s["resolution_price"] - s["entry_price"]) / risk)
    return {
        "total": len(setups),
        "open": open_n,
        "wins": len(wins),
        "losses": len(losses),
        "expired": len(expired),
        "win_rate": (len(wins) / decided * 100) if decided else None,
        "best": max(pcts) if pcts else None,
        "worst": min(pcts) if pcts else None,
        "avg_rr": (sum(realized_rr) / len(realized_rr)) if realized_rr else None,
    }


def build_winrate_embed(stats: dict) -> discord.Embed:
    e = discord.Embed(title="📈 Setup Track Record", color=COLOR_SCAN)
    e.add_field(name="Tracked", value=str(stats["total"]), inline=True)
    e.add_field(name="Open", value=str(stats["open"]), inline=True)
    e.add_field(
        name="Win rate",
        value=(f"{stats['win_rate']:.0f}%  ({stats['wins']}W / {stats['losses']}L)"
               if stats["win_rate"] is not None else "— (no resolved trades yet)"),
        inline=True,
    )
    e.add_field(name="Wins", value=str(stats["wins"]), inline=True)
    e.add_field(name="Losses", value=str(stats["losses"]), inline=True)
    e.add_field(name="Expired", value=str(stats["expired"]), inline=True)
    e.add_field(name="Best trade", value=_signed_pct(stats["best"]), inline=True)
    e.add_field(name="Worst trade", value=_signed_pct(stats["worst"]), inline=True)
    e.add_field(
        name="Avg R:R realized",
        value=(f"{stats['avg_rr']:.2f}" if stats["avg_rr"] is not None else "—"),
        inline=True,
    )
    e.set_footer(text=FOOTER + " · win rate excludes expired")
    e.timestamp = datetime.now(IST)
    return e


# =============================================================================
# Scheduled jobs
# =============================================================================

async def run_session_brief(session: str, phase: str = "Pre-Open Brief") -> bool:
    """Gather snapshot → synthesize → post to the session channel."""
    label = SESSION_LABELS.get(session, session)
    channel = bot.get_channel(CH[session])
    if not channel:
        print(f"[SCHED] cannot find #{session} channel")
        return False
    print(f"[SCHED] gathering snapshot for {label} {phase}…")
    snapshot = await gather_snapshot(BRIEF_SOURCES)
    available = sum(1 for v in snapshot.values() if v != "(unavailable)")
    if available == 0:
        reason = tn_state.get("last_tool_error") or "every TrueNorth tool call failed"
        await channel.send(embed=_failure_embed(f"📊 {label} Session — {phase}", reason))
        await alert_ops(f"{label} brief failed", f"0/{len(BRIEF_SOURCES)} TN sources: {reason}")
        return False
    print(f"[SCHED] {available}/{len(BRIEF_SOURCES)} sources ok; synthesizing…")
    try:
        text = await synthesize(build_brief_prompt(session, snapshot))
    except Exception as e:
        reason = f"synthesis failed: {type(e).__name__}: {e}"
        await channel.send(embed=_failure_embed(f"📊 {label} Session — {phase}", reason))
        await alert_ops(f"{label} brief failed", reason)
        return False
    await channel.send(embed=build_brief_embed(text, session, phase))
    tn_state["last_brief_at"][session] = datetime.now(IST)
    print(f"[SCHED] {label} {phase} posted ({available}/{len(BRIEF_SOURCES)} sources)")
    await maybe_autodraft(text, f"{label} {phase}")  # bonus tweet draft; never fails the brief
    return True


async def run_regime_update() -> bool:
    """Mon/Wed/Fri macro outlook → #regime-outlook."""
    channel = bot.get_channel(CH["regime"])
    if not channel:
        print("[SCHED] cannot find #regime-outlook channel")
        return False
    print("[SCHED] gathering regime snapshot…")
    snapshot = await gather_snapshot(BRIEF_SOURCES + REGIME_EXTRA_SOURCES)
    available = sum(1 for v in snapshot.values() if v != "(unavailable)")
    if available == 0:
        reason = tn_state.get("last_tool_error") or "every TrueNorth tool call failed"
        await channel.send(embed=_failure_embed("🌐 Macro Regime Outlook", reason))
        await alert_ops("Regime outlook failed", reason)
        return False
    try:
        text = await synthesize(build_regime_prompt(snapshot))
    except Exception as e:
        reason = f"synthesis failed: {type(e).__name__}: {e}"
        await channel.send(embed=_failure_embed("🌐 Macro Regime Outlook", reason))
        await alert_ops("Regime outlook failed", reason)
        return False
    await channel.send(embed=build_regime_embed(text))
    tn_state["last_regime_at"] = datetime.now(IST)
    print("[SCHED] regime outlook posted")
    await maybe_autodraft(text, "Regime")  # bonus tweet draft; never fails the post
    return True


# Each brief fires 15 minutes before its market's local open, cron'd in the
# market's own timezone so UK/US daylight-saving shifts are handled for free.
# IST equivalents (summer): Asia 05:15, London 12:15, US 18:45.
SCHEDULE = [
    ("asia",   "Asia/Tokyo",       8, 45),   # Tokyo opens 09:00 JST
    ("london", "Europe/London",    7, 45),   # LSE opens 08:00 local
    ("us",     "America/New_York", 9, 15),   # NYSE opens 09:30 ET
]


def setup_scheduler() -> None:
    """Register the 3 session briefs + Mon/Wed/Fri regime. Idempotent."""
    for session, tz, hour, minute in SCHEDULE:
        scheduler.add_job(
            run_session_brief,
            CronTrigger(hour=hour, minute=minute, timezone=ZoneInfo(tz)),
            args=[session],
            id=f"{session}_pre_open",
            replace_existing=True,
        )
    scheduler.add_job(
        run_regime_update,
        CronTrigger(day_of_week="mon,wed,fri", hour=6, minute=0, timezone=IST),
        id="regime_mwf",
        replace_existing=True,
    )
    scheduler.add_job(
        track_setups,
        IntervalTrigger(minutes=15),
        id="setup_tracker",
        replace_existing=True,
    )


# =============================================================================
# Health endpoint (Railway liveness)
# =============================================================================

_web_runner: web.AppRunner | None = None


async def _handle_healthz(_request: web.Request) -> web.Response:
    return web.json_response({
        "ok": True,
        "last_tool_success_at": (
            tn_state["last_tool_success_at"].isoformat()
            if tn_state["last_tool_success_at"] else None
        ),
        "last_briefs": {
            k: v.isoformat() for k, v in tn_state["last_brief_at"].items()
        },
    })


async def start_webhook_server() -> None:
    """Bring up the aiohttp server on $PORT. Idempotent."""
    global _web_runner
    if _web_runner is not None:
        return
    app = web.Application()
    app.router.add_get("/healthz", _handle_healthz)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    _web_runner = runner
    print(f"[Webhook] /healthz listening on 0.0.0.0:{WEBHOOK_PORT}")


# =============================================================================
# Bot events + commands
# =============================================================================

@bot.event
async def on_ready():
    print(f"DCT TrueNorth Bot online as {bot.user}")
    await start_webhook_server()
    init_setups()  # load tracked setups before the tracker job can fire
    setup_scheduler()
    if not scheduler.running:
        scheduler.start()
    print(f"[SCHED] {len(SCHEDULE)} session briefs + regime_mwf scheduled")
    for job in scheduler.get_jobs():
        print(f"[SCHED]   {job.id}: next run {job.next_run_time}")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏱️ Cooldown — try again in {int(error.retry_after)}s.")
        return
    print(f"[CMD ERROR] {ctx.command}: {type(error).__name__}: {error}")
    traceback.print_exception(type(error), error, error.__traceback__)
    await ctx.send(f"⚠️ Command failed: {type(error).__name__}")


@bot.command(name="brief")
@commands.cooldown(1, 300, commands.BucketType.guild)
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
    names = ", ".join(SESSION_LABELS[s] for s in targets)
    await ctx.send(f"⏳ Building {names} brief{'s' if len(targets) > 1 else ''}…")
    posted, failed = [], []
    for s in targets:
        ok = await run_session_brief(s, "Manual Brief")
        (posted if ok else failed).append(SESSION_LABELS[s])
    msg = ""
    if posted:
        msg += f"✅ Posted: {', '.join(posted)}."
    if failed:
        msg += f" ⚠️ Failed: {', '.join(failed)} — run `!health`."
    await ctx.send(msg.strip())


@bot.command(name="regime")
@commands.cooldown(1, 300, commands.BucketType.guild)
async def manual_regime(ctx: commands.Context):
    """Manually trigger the macro regime outlook."""
    await ctx.send("⏳ Building regime outlook…")
    ok = await run_regime_update()
    await ctx.send("✅ Posted to the regime channel." if ok else "⚠️ Failed — run `!health`.")


@bot.command(name="setup")
@commands.cooldown(1, 60, commands.BucketType.user)
async def trade_setup(ctx: commands.Context, ticker: str = ""):
    """Generate a trade setup for one asset. Usage: !setup BTC"""
    if not _setup_channel_allowed(ctx.channel.id):
        ctx.command.reset_cooldown(ctx)  # don't burn the user's cooldown on a wrong-channel try
        await ctx.send("❌ `!setup` isn't enabled in this channel.")
        return
    ticker = (ticker or "").strip().upper()
    if not ticker or len(ticker) > 24 or not re.match(r"^[A-Z0-9\-]+$", ticker):
        ctx.command.reset_cooldown(ctx)
        await ctx.send("Usage: `!setup BTC` — a ticker (BTC, ETH, SOL…) or a CoinGecko id.")
        return

    token_id = _resolve_token(ticker)
    status = await ctx.send(f"⏳ Building **{ticker}** setup…")

    # Same TN pattern as the brief engine, scoped to one asset.
    info, ta, derivs = await asyncio.gather(
        tn_call_safe("basic_market_info", {"token_address": token_id}),
        tn_call_safe("technical_analysis", {"token_address": token_id, "timeframe": "4h,1d"}),
        tn_call_safe("derivatives_analysis", {"token_address": token_id}),
    )
    price = (info or {}).get("market_data", {}).get("current_price")
    if price is None:
        await status.edit(content=(
            f"⚠️ Couldn't find market data for `{ticker}`. "
            f"Try `BTC`/`ETH`/`SOL`, or a CoinGecko id (e.g. `!setup bitcoin`)."
        ))
        return

    snapshot = {
        "basic_market_info": compact_json(info, 1500),
        "technical_analysis": compact_json(ta, 8000),
        "derivatives_analysis": compact_json(derivs, 5000),
    }
    try:
        setup = await synthesize_setup(ticker, snapshot)
    except Exception as e:
        print(f"[SETUP] {ticker} failed: {type(e).__name__}: {e}")
        await status.edit(content=f"⚠️ Setup generation failed ({type(e).__name__}). Run `!health`.")
        return

    await status.edit(content=None, embed=build_setup_embed(ticker, float(price), setup))
    log_setup(setup, ticker, token_id, status)  # track LONG/SHORT outcomes (no-ops otherwise)


@bot.command(name="scan")
@commands.cooldown(1, 60, commands.BucketType.user)
async def market_scan(ctx: commands.Context, count: str = "10"):
    """Top movers by 7-day relative strength vs BTC. Usage: !scan [1-25]"""
    if not _setup_channel_allowed(ctx.channel.id):
        ctx.command.reset_cooldown(ctx)
        await ctx.send("❌ `!scan` isn't enabled in this channel.")
        return
    try:
        n = max(1, min(25, int(count)))
    except (TypeError, ValueError):
        ctx.command.reset_cooldown(ctx)
        await ctx.send("Usage: `!scan` or `!scan 20` (1–25).")
        return
    status = await ctx.send("🔍 Scanning the market…")
    # One keyless TN call, no Claude — the cheapest command in the bot.
    scanner = await tn_call_safe(
        "performance_scanner", {"top": n, "lookback_days": 7, "universe_size": 100}
    )
    if not scanner or not scanner.get("leaderboard"):
        await status.edit(content="⚠️ Scanner returned no data — run `!health`.")
        return
    await status.edit(content=None, embed=build_scan_embed(scanner, n))


@bot.command(name="winrate")
async def winrate(ctx: commands.Context):
    """Public track record of !setup trades. Usage: !winrate"""
    if not _setup_channel_allowed(ctx.channel.id):
        await ctx.send("❌ `!winrate` isn't enabled in this channel.")
        return
    if not _SETUPS:
        await ctx.send("No setups tracked yet — use !setup to generate one.")
        return
    await ctx.send(embed=build_winrate_embed(compute_winrate(_SETUPS)))


def _format_delta(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    secs = int((datetime.now(IST) - dt).total_seconds())
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
    """Diagnostics: uptime, TN tool state, last posts, next scheduled runs."""
    now = datetime.now(IST)
    uptime_secs = int((now - BOT_STARTED_AT).total_seconds())
    d, rem = divmod(uptime_secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)

    e = discord.Embed(title="🩺 Bot Health", color=COLOR_INFO)
    e.add_field(name="Uptime", value=(f"{d}d {h}h {m}m" if d else f"{h}h {m}m"), inline=True)
    e.add_field(name="Engine", value=f"TN agent-tools + `{SYNTH_MODEL}`", inline=True)
    e.add_field(name="TN auth", value=("token set" if TN_AUTH_TOKEN else "keyless (crypto tools only)"), inline=True)
    e.add_field(name="Last TN tool success", value=_format_delta(tn_state["last_tool_success_at"]), inline=True)
    e.add_field(name="Last TN tool error", value=_format_delta(tn_state["last_tool_error_at"]), inline=True)
    if tn_state["last_tool_error"]:
        e.add_field(name="Error detail", value=f"```\n{str(tn_state['last_tool_error'])[:400]}\n```", inline=False)
    if tn_state["last_synth_error"]:
        e.add_field(name="Last synthesis error", value=f"```\n{str(tn_state['last_synth_error'])[:400]}\n```", inline=False)
    briefs = ", ".join(
        f"{SESSION_LABELS[s]} {_format_delta(dt)}" for s, dt in tn_state["last_brief_at"].items()
    ) or "none this process"
    e.add_field(name="Last briefs", value=briefs, inline=False)
    e.add_field(name="Last regime", value=_format_delta(tn_state["last_regime_at"]), inline=True)
    e.add_field(name="Dashboard link", value=(DASHBOARD_URL or "unset"), inline=True)
    e.add_field(
        name="!setup allowed",
        value=(f"{len(SETUP_ALLOWED_CHANNELS)} channel(s)" if SETUP_ALLOWED_CHANNELS else "all channels"),
        inline=True,
    )
    e.add_field(
        name="Auto-draft",
        value=(f"enabled · {tn_state['drafts_created']} this session" if AUTO_DRAFT_ENABLED else "disabled"),
        inline=True,
    )
    if tn_state["last_draft_error"]:
        e.add_field(name="Last draft error", value=f"```\n{str(tn_state['last_draft_error'])[:300]}\n```", inline=False)
    _ts = compute_winrate(_SETUPS)
    e.add_field(
        name="Tracked setups",
        value=(f"{_ts['open']} open / {_ts['total'] - _ts['open']} resolved "
               f"({_ts['wins']} wins, {_ts['losses']} losses, {_ts['expired']} expired)"),
        inline=False,
    )
    if scheduler.running:
        nxt = "\n".join(
            f"`{j.id}` → {j.next_run_time.astimezone(IST).strftime('%a %H:%M IST')}"
            for j in scheduler.get_jobs()
        )
        e.add_field(name="Next runs", value=nxt or "—", inline=False)
    e.set_footer(text=FOOTER)
    e.timestamp = now
    await ctx.send(embed=e)


# --- Entry point ---
if __name__ == "__main__":
    print("[BOOT] Starting DCT TrueNorth Bot (v2 — agent-tools engine)…")
    bot.run(DISCORD_TOKEN)
