"""
DCT TrueNorth Bot — Discord bridge for TrueNorth AI trading intelligence (v3).

v3 is fully deterministic: everything is composed rule-based from TrueNorth's
keyless agent-tools REST API (the backend the official `@truenorth-ai/cli`
wraps). No LLM anywhere — zero API spend, no credits to expire, and output
that is 100% numbers-from-source:

    TN agent-tools (TA / derivatives / scanner / events / indices)
        ──▶  raw dicts  ──▶  rule engines (briefs · setups · regime · tweets)
        ──▶  Discord embeds / Typefully drafts

What it does:
  * Posts a pre-open market brief 15 minutes before each market opens —
    Asia (Tokyo 09:00 JST), London (LSE 08:00 UK), US (NYSE 09:30 ET).
    Cron jobs run in each market's local timezone so DST is automatic.
  * Posts a macro regime outlook Mon/Wed/Fri 06:00 IST, and real-time
    regime-shift alerts between briefs.
  * !setup <ticker>: ATR/level-based trade setups with outcome tracking
    (!winrate); !scan: relative-strength screener.
  * Optional Typefully template-tweet drafts after each brief.
  * Serves GET /healthz on $PORT for Railway liveness.

History: v1 rode TrueNorth's authed conversational agent (Privy tokens,
Mac-local harvester) — killed by the TN v2 upgrade. v2 paired the keyless
tools API with Claude synthesis — killed by API credit burn. v3 keeps the
v2 data layer and replaces the LLM with rule engines (see git history for
both prior stacks).

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
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# aiohttp serves GET /healthz so Railway's web-service health check passes.
from aiohttp import web

# --- Config ---
# v3: fully deterministic — no LLM anywhere. Briefs, setups, and tweet drafts
# are built rule-based from TrueNorth data. Zero API spend; nothing to expire.
DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]

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

# Channels where !setup is allowed (comma-separated IDs). Empty ⇒ allowed in
# every channel; the 60s/user cooldown is the spam guard either way.
SETUP_ALLOWED_CHANNELS: set[int] = {
    int(c) for c in os.environ.get("SETUP_ALLOWED_CHANNELS", "").replace(" ", "").split(",") if c
}

# Optional: after a brief posts to Discord, build a template tweet and save it
# as a Typefully DRAFT (never published/scheduled). Off unless AUTO_DRAFT_ENABLED
# is truthy AND a key is set. Failures here never affect the brief.
AUTO_DRAFT_ENABLED = os.environ.get("AUTO_DRAFT_ENABLED", "").lower() in ("1", "true", "yes")
TYPEFULLY_API_KEY = os.environ.get("TYPEFULLY_API_KEY", "").strip()
# Typefully v2 social set (default = @Corgil_). Override via env if it changes.
TYPEFULLY_SOCIAL_SET_ID = os.environ.get("TYPEFULLY_SOCIAL_SET_ID", "208154").strip()

# Outcome tracking: !setup trades are logged to {CACHE_PATH}/setups.json and a
# background job resolves them to WIN/LOSS/EXPIRED for a public track record.
# Point CACHE_PATH at a mounted Railway volume to persist across restarts; if it
# isn't mounted, the file is ephemeral and history resets on redeploy (handled
# gracefully at startup with a warning).
CACHE_PATH = (os.environ.get("CACHE_PATH", ".").rstrip("/") or ".")
SETUPS_PATH = os.path.join(CACHE_PATH, "setups.json")
REGIME_PATH = os.path.join(CACHE_PATH, "last_regime.json")
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
scheduler = AsyncIOScheduler(timezone=IST)

# --- Observability state ---
BOT_STARTED_AT: datetime = datetime.now(IST)
tn_state: dict = {
    "last_tool_success_at": None,   # datetime | None — any TN tool call OK
    "last_tool_error_at": None,     # datetime | None
    "last_tool_error": None,        # str | None
    "last_synth_error": None,       # str | None — brief/setup builder failure
    "last_brief_at": {},            # {"asia"|"london"|"us": datetime}
    "last_regime_at": None,         # datetime | None
    "drafts_created": 0,            # int — Typefully drafts made this process
    "last_draft_error": None,       # str | None — Typefully/ghostwriter failure
}

# In-memory authoritative list of tracked setups (mirrors setups.json on disk).
_SETUPS: list[dict] = []

# Most recent rule-based regime read (mirrors last_regime.json on disk).
_LAST_REGIME: dict = {"regime": "UNKNOWN", "timestamp": None, "reasons": []}


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
# Deterministic brief engine — raw TN data → embed text, no LLM
# =============================================================================

# key → (tool, args). Fetched in parallel; each result stays a raw dict.
BRIEF_SOURCES = [
    ("info_btc", "basic_market_info",    {"token_address": "bitcoin"}),
    ("ta_btc",   "technical_analysis",   {"token_address": "bitcoin", "timeframe": "1d"}),
    ("info_eth", "basic_market_info",    {"token_address": "ethereum"}),
    ("ta_eth",   "technical_analysis",   {"token_address": "ethereum", "timeframe": "1d"}),
    ("derivs",   "derivatives_analysis", {"token_address": "bitcoin"}),
    ("scanner",  "performance_scanner",  {"top": 5, "lookback_days": 1}),
    ("events",   "events",               {"query": "crypto", "time_window": "24h", "sort_by": "relevance"}),
]

# Regime outlook adds longer-horizon sources (all keyless, indices included).
REGIME_EXTRA_SOURCES = [
    ("indices",    "market_index_price",  {"index": "all"}),
    ("events_7d",  "events",              {"query": "macro economy fed rates", "time_window": "7d", "sort_by": "relevance"}),
    ("scanner_7d", "performance_scanner", {"top": 5, "lookback_days": 7}),
]


async def gather_raw(sources: list[tuple]) -> dict:
    """Fetch all sources in parallel; return {key: raw dict or None}."""
    results = await asyncio.gather(*(tn_call_safe(tool, args) for _k, tool, args in sources))
    return {key: result for (key, _t, _a), result in zip(sources, results)}


def _ind(ta: dict | None) -> dict:
    return (ta or {}).get("technical_indicators") or {}


def _sr(ta: dict | None) -> dict:
    return (ta or {}).get("support_resistance") or {}


def _pct_s(x, digits: int = 1) -> str:
    return "—" if x is None else f"{x:+.{digits}f}%"


def _nearest_levels(ta: dict | None, price: float | None):
    """(support_str, resistance_str) from the S/R channels nearest to price."""
    channels = (_sr(ta).get("support and resistance channel") or {}).get("channels") or []
    if not channels or price is None:
        return None, None
    sup = [c for c in channels if c.get("hi") is not None and c["hi"] < price]
    res = [c for c in channels if c.get("lo") is not None and c["lo"] > price]
    sup_str = res_str = None
    if sup:
        c = max(sup, key=lambda c: c["hi"])
        sup_str = f"{_money(c['lo'])}–{_money(c['hi'])} (str {c.get('strength', '?')})"
    if res:
        c = min(res, key=lambda c: c["lo"])
        res_str = f"{_money(c['lo'])}–{_money(c['hi'])} (str {c.get('strength', '?')})"
    return sup_str, res_str


_EVENT_JUNK = ("not related", "not crypto", "cannot generate", "no direct crypto", "no crypto implication")


def clean_event_titles(events: dict | None, limit: int = 3) -> list[str]:
    """Top event titles with the LLM-filler junk items dropped."""
    out: list[str] = []
    for item in (events or {}).get("results") or []:
        title = (item.get("title") or "").strip()
        if not title or any(j in title.lower() for j in _EVENT_JUNK):
            continue
        if title not in out:
            out.append(title)
        if len(out) >= limit:
            break
    return out


def _trend_line(ta: dict | None) -> str | None:
    """'below 20d ($62,941) & 50d ($69,454) MA · MACD bull rising · RSI 34 rising'"""
    ind = _ind(ta)
    parts = []
    s20, s50 = ind.get("sma20") or {}, ind.get("sma50") or {}
    pos = []
    for label, s in (("20d", s20), ("50d", s50)):
        if s.get("state") == "price_above":
            pos.append((label, s.get("value"), True))
        elif s.get("state") == "price_below":
            pos.append((label, s.get("value"), False))
    if pos:
        above = [p for p in pos if p[2]]
        below = [p for p in pos if not p[2]]
        seg = []
        if above:
            seg.append("above " + " & ".join(f"{l} ({_money(v)})" for l, v, _ in above))
        if below:
            seg.append("below " + " & ".join(f"{l} ({_money(v)})" for l, v, _ in below))
        parts.append(" / ".join(seg) + " MA")
    macd = ind.get("macd_12_26_9") or {}
    if macd.get("state"):
        parts.append(f"MACD {macd['state']}" + (f" {macd['momentum']}" if macd.get("momentum") else ""))
    rsi = ind.get("rsi14") or {}
    if rsi.get("value") is not None:
        parts.append(f"RSI {rsi['value']:.0f}" + (f" {rsi['momentum']}" if rsi.get("momentum") else ""))
    return " · ".join(parts) if parts else None


def _range_line(ta: dict | None) -> str | None:
    ind, sr = _ind(ta), _sr(ta)
    hl = (sr.get("recent_high_low") or {}).get("calendar") or {}
    parts = []
    if hl.get("low_24h") is not None and hl.get("high_24h") is not None:
        parts.append(f"24h {_money(hl['low_24h'])}–{_money(hl['high_24h'])}")
    if hl.get("low_7d") is not None:
        parts.append(f"7d low {_money(hl['low_7d'])}")
    atr = ind.get("atr14") or {}
    if atr.get("value") is not None:
        pct = f" ({atr['atr_pct'] * 100:.1f}%)" if atr.get("atr_pct") is not None else ""
        parts.append(f"ATR {_money(atr['value'])}{pct}")
    vol = ind.get("volume") or {}
    if vol.get("vs_ma20") is not None:
        parts.append(f"volume {vol['vs_ma20'] * 100:+.0f}% vs 20d avg")
    return " · ".join(parts) if parts else None


def _positioning_lines(derivs: dict | None) -> list[str]:
    dd = (derivs or {}).get("derivative_data", {})
    sections = next((v for v in dd.values() if isinstance(v, dict)), {}) if isinstance(dd, dict) else {}
    lines = []
    fund = next((s for n, s in sections.items() if isinstance(s, dict) and "funding" in n.lower()), {})
    oi = next((s for n, s in sections.items() if isinstance(s, dict) and "open interest" in n.lower()), {})
    seg = []
    if fund.get("current_funding_rate_in_percentage") is not None:
        f = fund["current_funding_rate_in_percentage"]
        pctile = fund.get("current_funding_percentile_7d")
        seg.append(f"Funding {f:+.4f}%" + (f" ({pctile:.0f}th pctile 7d)" if pctile is not None else ""))
    if oi.get("current_open_interest") is not None:
        chg = (oi.get("rolling_changes") or {}).get("oi_change_1d_abs")
        pctile = (oi.get("percentile_analysis") or {}).get("current_oi_percentile_7d")
        s = f"OI ${oi['current_open_interest'] / 1e9:.1f}B"
        if chg is not None:
            s += f" ({'+' if chg >= 0 else '-'}${abs(chg) / 1e6:.0f}M 24h"
            s += f", {pctile:.0f}th pctile)" if pctile is not None else ")"
        seg.append(s)
    if seg:
        lines.append(" · ".join(seg))
    liq = next((s for n, s in sections.items() if isinstance(s, dict) and "liquidation" in n.lower()), {})
    pts = liq.get("max_liquidation_points") or {}
    seg = []
    for label, key in (("shorts", "max_short_liquidation_point"), ("longs", "max_long_liquidation_point")):
        cands = pts.get(key) or []
        if cands:
            p = min(cands, key=lambda c: abs(c.get("distance_pct", 9e9)))
            seg.append(f"${p['liq_usd'] / 1e6:.0f}M {label} @ {_money(p['price'])} ({p['distance_pct']:+.1f}%)")
    if seg:
        lines.append("Liq magnets: " + " · ".join(seg))
    return lines


def _mover_line(scanner: dict | None) -> str | None:
    rows = (scanner or {}).get("leaderboard") or []
    if not rows:
        return None
    return " · ".join(
        f"{_clean_ticker(r.get('ticker') or r.get('token'))} {_pct_s(r.get('momentum7D') if r.get('momentum7D') is not None else r.get('momentum1D'))}"
        f" (RS {_pct_s(r.get('rsVsBenchmark'))})"
        for r in rows[:5]
    )


def _asset_header(name: str, info: dict | None) -> str | None:
    md = (info or {}).get("market_data") or {}
    if md.get("current_price") is None:
        return None
    return (
        f"**{name} {_money(md['current_price'])}**  "
        f"{_pct_s(md.get('price_change_percentage_24h'))} 24h · "
        f"{_pct_s(md.get('price_change_percentage_7d'))} 7d · "
        f"{_pct_s(md.get('price_change_percentage_30d'))} 30d"
    )


def build_rule_brief(session: str, d: dict) -> str:
    """Compose the session brief embed text from raw tool results. Pure, no LLM.

    Sections degrade independently: any missing source just drops its lines.
    Raises only if BOTH BTC price and BTC TA are missing (nothing to say).
    """
    if (d.get("info_btc") or {}).get("market_data") is None and not _ind(d.get("ta_btc")):
        raise RuntimeError("no BTC data available")
    price_btc = ((d.get("info_btc") or {}).get("market_data") or {}).get("current_price")
    regime, reasons = detect_regime(d.get("ta_btc"), d.get("derivs"))
    out: list[str] = []
    if regime != "UNKNOWN":
        out.append(f"**Regime — {REGIME_DISPLAY.get(regime, regime)}**  ·  " + " · ".join(reasons[:3]))
    hdr = _asset_header("BTC", d.get("info_btc"))
    if hdr:
        out.append(hdr)
    for line in (_trend_line(d.get("ta_btc")), _range_line(d.get("ta_btc"))):
        if line:
            out.append(f"• {line}")
    sup, res = _nearest_levels(d.get("ta_btc"), price_btc)
    if sup or res:
        seg = [s for s in (f"support {sup}" if sup else None, f"resistance {res}" if res else None) if s]
        out.append("• Levels: " + " · ".join(seg))
    hdr = _asset_header("ETH", d.get("info_eth"))
    if hdr:
        out.append(hdr)
        line = _trend_line(d.get("ta_eth"))
        if line:
            out.append(f"• {line}")
    pos = _positioning_lines(d.get("derivs"))
    if pos:
        out.append("**Positioning**")
        out.extend(f"• {p}" for p in pos)
    movers = _mover_line(d.get("scanner"))
    if movers:
        out.append("**Movers (24h, RS vs BTC)**")
        out.append(f"• {movers}")
    titles = clean_event_titles(d.get("events"))
    if titles:
        out.append("**Watch**")
        out.extend(f"• {t}" for t in titles)
    return "\n".join(out)


def _indices_line(indices: dict | None) -> str | None:
    wanted = {"gspc": "SP500", "ixic": "NASDAQ", "vix": "VIX", "dxy": "DXY", "tnx": "US10Y"}
    parts = []
    for p in (indices or {}).get("prices") or []:
        label = wanted.get(str(p.get("index")).lower())
        latest = p.get("latest") or {}
        if label and latest.get("close") is not None:
            close = latest["close"]
            val = f"{close:,.0f}" if close >= 1000 else f"{close:,.2f}"  # US10Y 4.53, not "4"
            chg = latest.get("change_percentage")
            parts.append(f"{label} {val}" + (f" ({_pct_s(chg)})" if chg is not None else ""))
    return " · ".join(parts) if parts else None


def build_rule_regime_outlook(d: dict) -> str:
    """Mon/Wed/Fri macro outlook, composed from raw data. Pure, no LLM."""
    regime, reasons = detect_regime(d.get("ta_btc"), d.get("derivs"), d.get("indices"))
    out: list[str] = []
    if regime != "UNKNOWN":
        out.append(f"**Regime — {REGIME_DISPLAY.get(regime, regime)}**  ·  " + " · ".join(reasons[:4]))
    for name, info_k, ta_k in (("BTC", "info_btc", "ta_btc"), ("ETH", "info_eth", "ta_eth")):
        hdr = _asset_header(name, d.get(info_k))
        if hdr:
            out.append(hdr)
            line = _trend_line(d.get(ta_k))
            if line:
                out.append(f"• {line}")
    idx = _indices_line(d.get("indices"))
    if idx:
        out.append("**Cross-asset**")
        out.append(f"• {idx}")
    movers = _mover_line(d.get("scanner_7d"))
    if movers:
        out.append("**Rotation (7d RS vs BTC)**")
        out.append(f"• {movers}")
    titles = clean_event_titles(d.get("events_7d"))
    if titles:
        out.append("**Catalysts**")
        out.extend(f"• {t}" for t in titles)
    if not out:
        raise RuntimeError("no data available for regime outlook")
    return "\n".join(out)


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


# Signal weights for the rule-based setup engine. Direction needs |score| ≥ 2.5;
# conviction is High at |score| ≥ 4. Tuned to demand multi-signal alignment.
SETUP_SCORE_MIN = 2.5
SETUP_SCORE_HIGH = 4.0


def _setup_score(ta: dict | None) -> tuple[float, list[str], list[str]]:
    """(score, bull_signals, bear_signals) from daily TA. Positive ⇒ bullish."""
    ind = _ind(ta)
    score, bull, bear = 0.0, [], []

    def _hit(cond_bull, cond_bear, weight, btext, betext):
        nonlocal score
        if cond_bull:
            score += weight
            bull.append(btext)
        elif cond_bear:
            score -= weight
            bear.append(betext)

    s20 = (ind.get("sma20") or {}).get("state")
    s50 = (ind.get("sma50") or {}).get("state")
    _hit(s20 == "price_above", s20 == "price_below", 1.0, "above SMA20", "below SMA20")
    _hit(s50 == "price_above", s50 == "price_below", 1.0, "above SMA50", "below SMA50")
    macd = ind.get("macd_12_26_9") or {}
    _hit(macd.get("state") == "bull", macd.get("state") == "bear", 1.0, "MACD bull", "MACD bear")
    _hit(macd.get("momentum") == "rising", macd.get("momentum") == "falling", 0.5,
         "MACD momentum rising", "MACD momentum falling")
    rsi = (ind.get("rsi14") or {}).get("value")
    if rsi is not None:
        _hit(rsi > 55, rsi < 45, 1.0, f"RSI {rsi:.0f} strong", f"RSI {rsi:.0f} weak")
    rsi_m = (ind.get("rsi14") or {}).get("momentum")
    _hit(rsi_m == "rising", rsi_m == "falling", 0.5, "RSI rising", "RSI falling")
    boll = ind.get("boll_20_2") or {}
    _hit(boll.get("mid_relation") == "above_mid", boll.get("mid_relation") == "below_mid", 0.5,
         "above BB mid", "below BB mid")
    return score, bull, bear


NO_SETUP = {
    "has_setup": False, "direction": "NONE", "entry_zone": "—", "stop_loss": "—",
    "take_profit_1": "—", "take_profit_2": "—", "rr_ratio": "—", "conviction": "None",
}


def build_rule_setup(ticker: str, info: dict | None, ta: dict | None, derivs: dict | None) -> dict:
    """Deterministic trade setup from TN data — ATR-based levels, no LLM.

    Same output shape the Claude generator produced, so the embed builder and
    outcome tracking are untouched. Direction requires multi-signal alignment
    (|score| ≥ 2.5 of ±5.5 possible); levels are ATR-derived and coherent by
    construction: stop 1.5×ATR from entry, TP1/TP2 at 1.5R/3R.
    """
    price = ((info or {}).get("market_data") or {}).get("current_price")
    atr = (_ind(ta).get("atr14") or {}).get("value")
    score, bull, bear = _setup_score(ta)
    fund = _btc_funding(derivs)
    fund_note = f"funding {fund:+.4f}%" if fund is not None else None

    if price is None or atr is None or not atr > 0:
        return {**NO_SETUP, "reasoning": "No clear setup — price/ATR data unavailable."}
    if abs(score) < SETUP_SCORE_MIN:
        detail = ", ".join((bull + bear)[:3]) or "signals flat"
        return {**NO_SETUP,
                "reasoning": f"No clear setup — ranging/choppy conditions (signal score {score:+.1f}: {detail})."}

    direction = "LONG" if score > 0 else "SHORT"
    sign = 1 if direction == "LONG" else -1
    entry_lo, entry_hi = price - 0.25 * atr, price + 0.25 * atr
    stop = price - sign * 1.5 * atr
    risk = abs(price - stop)
    tp1 = price + sign * 1.5 * risk
    tp2 = price + sign * 3.0 * risk
    signals = bull if direction == "LONG" else bear
    reasoning = f"{'Bullish' if sign > 0 else 'Bearish'} alignment (score {score:+.1f}): " + ", ".join(signals[:4])
    if fund_note:
        reasoning += f"; {fund_note}"
    reasoning += ". Levels are 1.5×ATR risk with 1.5R/3R targets."
    return {
        "has_setup": True,
        "direction": direction,
        "entry_zone": f"{_money(entry_lo)} – {_money(entry_hi)}",
        "stop_loss": _money(stop),
        "take_profit_1": _money(tp1),
        "take_profit_2": _money(tp2),
        "rr_ratio": "1.5 (TP1) / 3.0 (TP2)",
        "conviction": "High" if abs(score) >= SETUP_SCORE_HIGH else "Medium",
        "reasoning": reasoning,
    }


def _setup_channel_allowed(channel_id: int) -> bool:
    """Gate !setup by SETUP_ALLOWED_CHANNELS; empty set ⇒ allowed everywhere."""
    return not SETUP_ALLOWED_CHANNELS or channel_id in SETUP_ALLOWED_CHANNELS


# =============================================================================
# Typefully auto-draft (optional) — a posted brief → a tweet draft to review
# =============================================================================

def _typefully_drafts_url() -> str:
    return f"https://api.typefully.com/v2/social-sets/{TYPEFULLY_SOCIAL_SET_ID}/drafts"


def _fmt_price_short(x: float) -> str:
    """Casual tweet-style price: 96k, 62.4k, $1,604, $0.19."""
    if x >= 10_000:
        return f"{x / 1000:.0f}k" if x >= 100_000 else f"{x / 1000:.1f}k".replace(".0k", "k")
    if x >= 1:
        return f"${x:,.0f}"
    return f"${x:.2g}"


def build_template_tweet(session: str, d: dict) -> str | None:
    """Deterministic draft tweet from the brief's raw data — lowercase,
    crypto-native, ends on a question. It's a DRAFT: you edit before posting.

    Picks the most notable data point: hot mover (|RS| ≥ 8%) > funding extreme
    (≥60th/≤10th pctile) > regime + level read. Returns None if there's no BTC
    price (nothing worth drafting).
    """
    md = ((d.get("info_btc") or {}).get("market_data") or {})
    price = md.get("current_price")
    if price is None:
        return None
    p = _fmt_price_short(price)
    sess = {"asia": "asia open", "london": "london open", "us": "us open"}.get(session, "the open")
    regime, _reasons = detect_regime(d.get("ta_btc"), d.get("derivs"))
    ind = _ind(d.get("ta_btc"))
    rsi = (ind.get("rsi14") or {}).get("value")
    s20 = (ind.get("sma20") or {}).get("state")
    trend = "under the 20d" if s20 == "price_below" else "over the 20d" if s20 == "price_above" else "at the 20d"

    rows = (d.get("scanner") or {}).get("leaderboard") or []
    hot = max(rows, key=lambda r: abs(r.get("rsVsBenchmark") or 0), default=None)
    dd = (d.get("derivs") or {}).get("derivative_data", {})
    sections = next((v for v in dd.values() if isinstance(v, dict)), {}) if isinstance(dd, dict) else {}
    fund = next((s for n, s in sections.items() if isinstance(s, dict) and "funding" in n.lower()), {})
    fund_pctile = fund.get("current_funding_percentile_7d")

    if hot and abs(hot.get("rsVsBenchmark") or 0) >= 8:
        tk = _clean_ticker(hot.get("ticker") or hot.get("token")).lower()
        rs = hot.get("rsVsBenchmark")
        chg = hot.get("momentum7D") if hot.get("momentum7D") is not None else hot.get("momentum1D")
        return (f"{tk} {'+' if (chg or 0) >= 0 else ''}{chg:.1f}% while btc sits {trend} at {p} — "
                f"that's {rs:+.0f}% relative strength into {sess}. rotation or head-fake?")[:280]
    if fund_pctile is not None and (fund_pctile >= 60 or fund_pctile <= 10):
        f = fund.get("current_funding_rate_in_percentage")
        heat = "heating up" if fund_pctile >= 60 else "washed out"
        return (f"btc {p} into {sess} and funding is {heat} "
                f"({f:+.4f}%, {fund_pctile:.0f}th pctile this week). rsi {rsi:.0f}. "
                f"who's positioned for this?")[:280] if rsi is not None else None
    mood = {"RISK-ON": "risk-on tape", "RISK-OFF": "risk-off tape", "NEUTRAL": "two-sided tape"}.get(regime, "mixed tape")
    rsi_s = f", rsi {rsi:.0f}" if rsi is not None else ""
    return f"btc {trend} at {p} into {sess}{rsi_s} — {mood}. what's your lean here?"[:280]


async def create_typefully_draft(text: str) -> str | None:
    """POST a Typefully v2 draft for the configured social set. Returns a share
    URL/id, or None on any failure.

    Never raises and never publishes/schedules — no `publish_at` ⇒ it stays a
    draft. Auth is a plain `Authorization: Bearer <key>` (a key pasted with the
    'Bearer ' prefix already is de-duplicated, not double-prefixed).
    """
    key = TYPEFULLY_API_KEY
    if key.lower().startswith("bearer "):
        key = key[len("bearer "):].strip()
    body = {
        "platforms": {"x": {"enabled": True, "posts": [{"text": text}]}},
        "draft_title": "Auto-draft from brief",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.post(
                _typefully_drafts_url(),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=body,
            )
        if resp.status_code in (200, 201):
            data = resp.json() if resp.content else {}
            draft = data.get("draft") if isinstance(data.get("draft"), dict) else data
            draft_id = draft.get("id") if isinstance(draft, dict) else None
            return (
                (draft.get("share_url") if isinstance(draft, dict) else None)
                or (draft.get("url") if isinstance(draft, dict) else None)
                or (f"https://typefully.com/?d={draft_id}" if draft_id else "draft created")
            )
        tn_state["last_draft_error"] = f"HTTP {resp.status_code}: {resp.text[:160]}"
    except Exception as e:
        tn_state["last_draft_error"] = f"{type(e).__name__}: {e}"
    print(f"[AUTODRAFT] Typefully draft failed: {tn_state['last_draft_error']}")
    return None


async def maybe_autodraft(tweet: str | None, label: str) -> None:
    """Best-effort: push a pre-built template tweet to Typefully as a draft.
    Fully gated and self-contained; failures are logged and swallowed so the
    brief is unaffected."""
    if not AUTO_DRAFT_ENABLED or not tweet:
        return
    if not TYPEFULLY_API_KEY:
        print("[AUTODRAFT] AUTO_DRAFT_ENABLED set but TYPEFULLY_API_KEY missing — skipping")
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
# Regime-shift detection — rule-based, piggybacks on each session brief
# =============================================================================

COLOR_REGIME_SHIFT = 0xFF9500
REGIME_DISPLAY = {"RISK-ON": "Risk-On", "RISK-OFF": "Risk-Off", "NEUTRAL": "Neutral", "UNKNOWN": "Unknown"}


def _btc_funding(derivs: dict) -> float | None:
    """Current funding % from a derivatives_analysis result (robust to symbol/section naming)."""
    dd = (derivs or {}).get("derivative_data", {})
    if isinstance(dd, dict):
        for sections in dd.values():
            if isinstance(sections, dict):
                for name, sec in sections.items():
                    if isinstance(sec, dict) and "funding" in name.lower():
                        v = sec.get("current_funding_rate_in_percentage")
                        if v is not None:
                            return v
    return None


def _vix_value(indices: dict) -> float | None:
    """VIX close from a market_index_price result, if present (briefs usually omit it)."""
    if isinstance(indices, dict):
        for p in indices.get("prices", []):
            if isinstance(p, dict) and str(p.get("index")).lower() == "vix" and isinstance(p.get("latest"), dict):
                return p["latest"].get("close")
    return None


def detect_regime(ta: dict, derivs: dict, indices: dict | None = None):
    """Rule-based regime from raw TN data → (regime, reasons). No LLM.

    RISK-OFF if 2+ of: BTC below 20d/50d MA, funding negative, VIX>20, RSI<40.
    RISK-ON  if 2+ of: BTC above 20d AND 50d MA, funding positive, VIX<18, RSI>55.
    Returns UNKNOWN if fewer than 2 signals are evaluable (e.g. data outage), so a
    missing-data check never masquerades as a regime flip.
    """
    ind = (ta or {}).get("technical_indicators") or {}
    s20 = (ind.get("sma20") or {}).get("state") if isinstance(ind.get("sma20"), dict) else None
    s50 = (ind.get("sma50") or {}).get("state") if isinstance(ind.get("sma50"), dict) else None
    v20 = (ind.get("sma20") or {}).get("value") if isinstance(ind.get("sma20"), dict) else None
    rsi = (ind.get("rsi14") or {}).get("value") if isinstance(ind.get("rsi14"), dict) else None
    funding = _btc_funding(derivs)
    vix = _vix_value(indices)

    ma_known = s20 is not None or s50 is not None
    evaluable = (1 if ma_known else 0) + sum(x is not None for x in (rsi, funding, vix))
    if evaluable < 2:
        return "UNKNOWN", []

    off, on = [], []
    below = [w for w, st in (("20d", s20), ("50d", s50)) if st == "price_below"]
    if below:
        lvl = f" (${v20:,.0f})" if (s20 == "price_below" and isinstance(v20, (int, float))) else ""
        off.append(f"BTC below {'/'.join(below)} MA{lvl}")
    elif s20 == "price_above" and s50 == "price_above":
        on.append("BTC above 20d & 50d MA")
    if funding is not None and funding != 0:
        if funding < 0:
            off.append(f"funding negative ({funding:+.3f}%)")
        else:
            on.append(f"funding positive ({funding:+.3f}%)")
    if vix is not None:
        if vix > 20:
            off.append(f"VIX {vix:.0f} (>20)")
        elif vix < 18:
            on.append(f"VIX {vix:.0f} (<18)")
    if rsi is not None:
        if rsi < 40:
            off.append(f"RSI {rsi:.0f} (<40)")
        elif rsi > 55:
            on.append(f"RSI {rsi:.0f} (>55)")

    if len(off) >= 2:
        return "RISK-OFF", off
    if len(on) >= 2:
        return "RISK-ON", on
    return "NEUTRAL", (off + on) or ["signals balanced"]


def _persist_regime() -> None:
    """Atomic write of _LAST_REGIME. Best-effort."""
    try:
        os.makedirs(CACHE_PATH, exist_ok=True)
        tmp = REGIME_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_LAST_REGIME, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, REGIME_PATH)
    except Exception as e:
        print(f"[REGIME] WARNING: failed to persist: {type(e).__name__}: {e}")


def init_regime() -> None:
    """Load last_regime.json at startup; missing ⇒ baseline UNKNOWN (first brief sets
    it with no alert). Covers an unmounted volume where the file doesn't persist."""
    global _LAST_REGIME
    try:
        if os.path.exists(REGIME_PATH):
            with open(REGIME_PATH) as f:
                _LAST_REGIME = json.load(f)
            print(f"[REGIME] loaded baseline: {_LAST_REGIME.get('regime')}")
        else:
            _LAST_REGIME = {"regime": "UNKNOWN", "timestamp": None, "reasons": []}
            print(f"[REGIME] {REGIME_PATH} not found — baseline UNKNOWN (first brief sets it, no alert)")
            _persist_regime()
    except Exception as e:
        _LAST_REGIME = {"regime": "UNKNOWN", "timestamp": None, "reasons": []}
        print(f"[REGIME] WARNING: load failed ({type(e).__name__}) — baseline UNKNOWN")


async def _post_regime_shift(session: str, prev: str, regime: str, reasons: list[str]) -> None:
    channel = bot.get_channel(CH["regime"])
    if channel is None:
        print("[REGIME] #regime-outlook channel not found — can't post shift")
        return
    now = datetime.now(IST)
    e = discord.Embed(
        title=f"⚠️ REGIME SHIFT: {REGIME_DISPLAY.get(prev, prev)} → {REGIME_DISPLAY.get(regime, regime)}",
        description=" • ".join(reasons[:3]) if reasons else "Signals shifted.",
        color=COLOR_REGIME_SHIFT,
    )
    e.set_footer(text=f"Auto-detected from {SESSION_LABELS.get(session, session)} brief data | {now:%b %d, %H:%M IST}")
    e.timestamp = now
    try:
        await channel.send(embed=e)
    except Exception as ex:
        print(f"[REGIME] failed to post shift: {type(ex).__name__}: {ex}")


async def check_regime_shift(session: str, ta: dict | None = None, derivs: dict | None = None) -> None:
    """Derive the regime by rule and alert #regime-outlook on a flip vs the stored
    baseline. Piggybacks on a brief run; never raises.

    The brief passes its own daily TA + derivatives in; standalone callers can
    omit them and fresh data is fetched.
    """
    try:
        if ta is None or derivs is None:
            ta, derivs = await asyncio.gather(
                tn_call_safe("technical_analysis", {"token_address": "bitcoin", "timeframe": "1d"}),
                tn_call_safe("derivatives_analysis", {"token_address": "bitcoin"}),
            )
        regime, reasons = detect_regime(ta, derivs)
        if regime == "UNKNOWN":
            print(f"[REGIME] {session}: insufficient data — skipping")
            return
        prev = _LAST_REGIME.get("regime", "UNKNOWN")
        now = datetime.now(IST)
        _LAST_REGIME.update({"regime": regime, "timestamp": now.isoformat(), "reasons": reasons})
        _persist_regime()  # baseline updated every check, shift or not
        if prev not in (None, "UNKNOWN") and prev != regime:
            await _post_regime_shift(session, prev, regime, reasons)
            print(f"[REGIME] SHIFT {prev} -> {regime} ({session}) — alerted")
        else:
            print(f"[REGIME] {session}: {regime} (prev {prev}) — no alert")
    except Exception as e:
        print(f"[REGIME] check failed: {type(e).__name__}: {e}")


# =============================================================================
# Scheduled jobs
# =============================================================================

async def run_session_brief(session: str, phase: str = "Pre-Open Brief") -> bool:
    """Gather raw data → rule-based compose → post to the session channel."""
    label = SESSION_LABELS.get(session, session)
    channel = bot.get_channel(CH[session])
    if not channel:
        print(f"[SCHED] cannot find #{session} channel")
        return False
    print(f"[SCHED] gathering data for {label} {phase}…")
    d = await gather_raw(BRIEF_SOURCES)
    available = sum(1 for v in d.values() if v is not None)
    if available == 0:
        reason = tn_state.get("last_tool_error") or "every TrueNorth tool call failed"
        await channel.send(embed=_failure_embed(f"📊 {label} Session — {phase}", reason))
        await alert_ops(f"{label} brief failed", f"0/{len(BRIEF_SOURCES)} TN sources: {reason}")
        return False
    # Regime-shift alert reuses the brief's own daily TA + derivs (no refetch).
    await check_regime_shift(session, ta=d.get("ta_btc"), derivs=d.get("derivs"))
    print(f"[SCHED] {available}/{len(BRIEF_SOURCES)} sources ok; composing…")
    try:
        text = build_rule_brief(session, d)
    except Exception as e:
        tn_state["last_synth_error"] = f"{type(e).__name__}: {e}"
        reason = f"brief build failed: {type(e).__name__}: {e}"
        await channel.send(embed=_failure_embed(f"📊 {label} Session — {phase}", reason))
        await alert_ops(f"{label} brief failed", reason)
        return False
    await channel.send(embed=build_brief_embed(text, session, phase))
    tn_state["last_brief_at"][session] = datetime.now(IST)
    print(f"[SCHED] {label} {phase} posted ({available}/{len(BRIEF_SOURCES)} sources)")
    await maybe_autodraft(build_template_tweet(session, d), f"{label} {phase}")
    return True


async def run_regime_update() -> bool:
    """Mon/Wed/Fri macro outlook → #regime-outlook."""
    channel = bot.get_channel(CH["regime"])
    if not channel:
        print("[SCHED] cannot find #regime-outlook channel")
        return False
    print("[SCHED] gathering regime data…")
    d = await gather_raw(BRIEF_SOURCES + REGIME_EXTRA_SOURCES)
    available = sum(1 for v in d.values() if v is not None)
    if available == 0:
        reason = tn_state.get("last_tool_error") or "every TrueNorth tool call failed"
        await channel.send(embed=_failure_embed("🌐 Macro Regime Outlook", reason))
        await alert_ops("Regime outlook failed", reason)
        return False
    try:
        text = build_rule_regime_outlook(d)
    except Exception as e:
        tn_state["last_synth_error"] = f"{type(e).__name__}: {e}"
        reason = f"outlook build failed: {type(e).__name__}: {e}"
        await channel.send(embed=_failure_embed("🌐 Macro Regime Outlook", reason))
        await alert_ops("Regime outlook failed", reason)
        return False
    await channel.send(embed=build_regime_embed(text))
    tn_state["last_regime_at"] = datetime.now(IST)
    print("[SCHED] regime outlook posted")
    await maybe_autodraft(build_template_tweet("us", d), "Regime")
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
    init_regime()  # load the regime baseline before any brief checks it
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

    # 4h timeframe: signals and ATR-sized levels that fit the 48h tracking window.
    info, ta, derivs = await asyncio.gather(
        tn_call_safe("basic_market_info", {"token_address": token_id}),
        tn_call_safe("technical_analysis", {"token_address": token_id, "timeframe": "4h"}),
        tn_call_safe("derivatives_analysis", {"token_address": token_id}),
    )
    price = (info or {}).get("market_data", {}).get("current_price")
    if price is None:
        await status.edit(content=(
            f"⚠️ Couldn't find market data for `{ticker}`. "
            f"Try `BTC`/`ETH`/`SOL`, or a CoinGecko id (e.g. `!setup bitcoin`)."
        ))
        return

    try:
        setup = build_rule_setup(ticker, info, ta, derivs)  # deterministic, no LLM
    except Exception as e:
        tn_state["last_synth_error"] = f"{type(e).__name__}: {e}"
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
    e.add_field(name="Engine", value="TN agent-tools · rule-based (no LLM)", inline=True)
    e.add_field(name="TN auth", value=("token set" if TN_AUTH_TOKEN else "keyless (crypto tools only)"), inline=True)
    e.add_field(name="Last TN tool success", value=_format_delta(tn_state["last_tool_success_at"]), inline=True)
    e.add_field(name="Last TN tool error", value=_format_delta(tn_state["last_tool_error_at"]), inline=True)
    if tn_state["last_tool_error"]:
        e.add_field(name="Error detail", value=f"```\n{str(tn_state['last_tool_error'])[:400]}\n```", inline=False)
    if tn_state["last_synth_error"]:
        e.add_field(name="Last build error", value=f"```\n{str(tn_state['last_synth_error'])[:400]}\n```", inline=False)
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
    _reg = _LAST_REGIME.get("regime", "UNKNOWN")
    _reg_ts = _LAST_REGIME.get("timestamp")
    if _reg == "UNKNOWN" or not _reg_ts:
        _reg_val = "UNKNOWN (not yet checked)"
    else:
        try:
            _reg_val = f"{_reg} (last checked {_format_delta(datetime.fromisoformat(_reg_ts))})"
        except (TypeError, ValueError):
            _reg_val = _reg
    e.add_field(name="Regime", value=_reg_val, inline=True)
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
