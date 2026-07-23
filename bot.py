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
from datetime import datetime, timedelta
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
def _storage_status(path: str | None = None, setups_file: str | None = None) -> str:
    """One-line persistence diagnostic for !health. Ephemeral CACHE_PATH means
    the track record resets on every redeploy — surface it loudly so a missing
    volume mount can't silently wipe history unnoticed. Defaults resolve at call
    time (CACHE_PATH is defined later in the module)."""
    path = CACHE_PATH if path is None else path
    setups_file = SETUPS_PATH if setups_file is None else setups_file
    if path in (".", "", "./"):
        return "⚠️ EPHEMERAL (CACHE_PATH unset — track record resets on redeploy; mount a volume + set CACHE_PATH)"
    exists = os.path.exists(setups_file)
    writable = os.access(path, os.W_OK) if os.path.isdir(path) else False
    flag = "persistent" if writable else "⚠️ not writable"
    return f"{flag} · CACHE_PATH={path} · setups.json {'present' if exists else 'not yet created'}"


def _optional_channel_id(name: str) -> int | None:
    """Parse an optional channel-ID env var. A malformed value (e.g. a webhook
    URL pasted by mistake) must degrade to 'unset' with a loud log — an optional
    feature's config error can never be allowed to crash-loop the whole bot."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        print(f"[CONFIG] WARNING: {name} is not a numeric channel ID "
              f"(got {raw[:40]!r}...) — treating as unset. Use the Discord "
              f"channel ID (right-click channel → Copy Channel ID), not a webhook URL.")
        return None


# Optional ops channel for failure alerts. Unset → alerts go to logs only
# (the community channels never see plumbing noise).
CH_OPS = _optional_channel_id("CH_OPS")

# Optional auto-signal channel. Unset → the 4h auto-signal scan never runs.
CH_SIGNALS = _optional_channel_id("CH_SIGNALS")

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
    ("options",  "options_report",       {"token_address": "bitcoin"}),
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
    opt = _options_line(d.get("options"))
    if opt:
        out.append("**Options (BTC)**")
        out.append(f"• {opt}")
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


def _options_line(opts: dict | None) -> str | None:
    """'bullish · negative gamma · ATM IV 40.8% · max pain $75,000 · P/C 0.515 · RR bearish (-5.69%)'

    Composed from options_report's summary block (Derive BTC options). Any
    missing piece is dropped; returns None if nothing useful is present.
    """
    summary = (opts or {}).get("summary") or {}
    sig = summary.get("signal_summary") or {}
    levels = summary.get("key_levels") or {}
    parts = []
    if summary.get("sentiment"):
        parts.append(str(summary["sentiment"]))
    if sig.get("gex_regime"):
        parts.append(str(sig["gex_regime"]))
    atm_iv = (opts or {}).get("atm_iv")
    if atm_iv is not None:
        parts.append(f"ATM IV {atm_iv:.1f}%")
    if levels.get("max_pain") is not None:
        parts.append(f"max pain {_money(levels['max_pain'])}")
    if sig.get("put_call_ratio"):
        parts.append(f"P/C {str(sig['put_call_ratio']).replace(' by OI', '')} (OI)")
    if sig.get("risk_reversal"):
        parts.append(f"RR {sig['risk_reversal']}")
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
    opt = _options_line(d.get("options"))
    if opt:
        out.append("**Options (BTC)**")
        out.append(f"• {opt}")
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


# =============================================================================
# Cross-asset support (stocks + commodities) — additive, crypto path untouched.
# TN has NO indicator tool for non-crypto (technical_analysis / _v3 are crypto-
# only), so we compute the SAME indicator dict shape technical_analysis returns
# from raw historical_bars OHLCV. build_rule_setup / _setup_score then work
# unchanged. Stocks/commodities have no 4h bars → these run on the daily.
# =============================================================================

COMMODITY_ALIASES = {"GOLD", "SILVER", "OIL", "GAS", "NATGAS", "COPPER",
                     "PLATINUM", "PALLADIUM", "WTI", "BRENT"}

# Per-asset-class setup config: (setup timeframe, resolution timeframe, expiry h).
# Crypto keeps 4h/15m/48h; non-crypto runs daily with a longer window because a
# daily setup can't resolve inside 48h.
ASSET_CFG = {
    "crypto":    {"tf": "4h", "res_tf": "15m", "expiry_h": 48},
    "stock":     {"tf": "1d", "res_tf": "1d",  "expiry_h": 168},
    "commodity": {"tf": "1d", "res_tf": "1d",  "expiry_h": 168},
}


def classify_asset(ticker: str) -> tuple[str, str]:
    """(asset_class, instrument) for a user ticker. Crypto stays the default so
    existing behavior is byte-identical; only recognized commodity aliases and
    plausible stock tickers route to the new path.
    ponytail: an obscure crypto ticker not in TICKER_TO_ID and not hyphenated
    could misroute to 'stock' — user can pass the CoinGecko id to force crypto."""
    t = ticker.strip().upper()
    if t in TICKER_TO_ID:
        return "crypto", TICKER_TO_ID[t]
    if t in COMMODITY_ALIASES:
        return "commodity", t.lower()
    # Hyphen or lowercase input ⇒ a CoinGecko id (e.g. "curve-dao-token") ⇒ crypto.
    if "-" in ticker or ticker != t:
        return "crypto", ticker.strip().lower()
    # A bare 1–5 letter uppercase symbol we don't know as crypto ⇒ treat as a stock.
    if t.isalpha() and 1 <= len(t) <= 5:
        return "stock", t
    return "crypto", t.lower()


def _ema(values: list[float], span: int) -> list[float]:
    """Standard EMA series (SMA seed on the first `span` values)."""
    if len(values) < span:
        return []
    k = 2 / (span + 1)
    ema = sum(values[:span]) / span
    out = [ema]
    for v in values[span:]:
        ema = v * k + ema * (1 - k)
        out.append(ema)
    return out


def _rsi_series(closes: list[float], length: int = 14) -> list[float]:
    """Wilder RSI series (one value per bar from index `length` on)."""
    if len(closes) <= length:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_g = sum(gains[:length]) / length
    avg_l = sum(losses[:length]) / length
    out = []
    for i in range(length, len(gains) + 1):
        if i > length:
            avg_g = (avg_g * (length - 1) + gains[i - 1]) / length
            avg_l = (avg_l * (length - 1) + losses[i - 1]) / length
        rs = (avg_g / avg_l) if avg_l > 0 else float("inf")
        out.append(100.0 if avg_l == 0 else 100 - 100 / (1 + rs))
    return out


def compute_indicators_from_bars(bars: list[dict]) -> dict | None:
    """Return technical_analysis-compatible {'technical_indicators': {...}} from
    raw OHLCV bars, so build_rule_setup / _setup_score consume it unchanged.
    None if there aren't enough clean bars (need ≥ 51 for SMA50 + momentum)."""
    closes, highs, lows = [], [], []
    for b in sorted(bars or [], key=lambda c: c.get("t") or ""):
        try:
            closes.append(float(b["close"]))
            highs.append(float(b["high"]))
            lows.append(float(b["low"]))
        except (KeyError, TypeError, ValueError):
            continue
    if len(closes) < 51:
        return None
    price = closes[-1]
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50

    macd_line = [f - s for f, s in zip(_ema(closes, 12)[-len(_ema(closes, 26)):], _ema(closes, 26))]
    signal = _ema(macd_line, 9)
    hist = [m - s for m, s in zip(macd_line[-len(signal):], signal)] if signal else []
    macd_state = macd_momo = None
    if signal:
        macd_state = "bull" if macd_line[-1] > signal[-1] else "bear"
    if len(hist) >= 2 and hist[-1] != hist[-2]:   # tie ⇒ neutral, not "falling"
        macd_momo = "rising" if hist[-1] > hist[-2] else "falling"

    rsi = _rsi_series(closes, 14)
    rsi_val = rsi[-1] if rsi else None
    rsi_momo = None
    if len(rsi) >= 2 and rsi[-1] != rsi[-2]:      # tie ⇒ neutral
        rsi_momo = "rising" if rsi[-1] > rsi[-2] else "falling"

    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
           for i in range(1, len(closes))]
    atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else None

    return {"technical_indicators": {
        "sma20": {"state": "price_above" if price > sma20 else "price_below", "value": sma20},
        "sma50": {"state": "price_above" if price > sma50 else "price_below", "value": sma50},
        "macd_12_26_9": {"state": macd_state, "momentum": macd_momo},
        "rsi14": {"value": rsi_val, "momentum": rsi_momo},
        "boll_20_2": {"mid_relation": "above_mid" if price > sma20 else "below_mid"},
        "atr14": {"value": atr},
    }}


async def _none_coro():
    """Awaitable that yields None — lets asyncio.gather skip a branch cleanly."""
    return None


async def gather_setup_inputs(ticker: str) -> dict:
    """Route a ticker to its asset class and return everything build_rule_setup
    needs, in a uniform shape. Crypto path is byte-identical to the old inline
    gather; stocks/commodities fetch daily bars and compute indicators locally."""
    asset_class, instrument = classify_asset(ticker)
    cfg = ASSET_CFG[asset_class]
    if asset_class == "crypto":
        info, ta, derivs = await asyncio.gather(
            tn_call_safe("basic_market_info", {"token_address": instrument}),
            tn_call_safe("technical_analysis", {"token_address": instrument, "timeframe": cfg["tf"]}),
            tn_call_safe("derivatives_analysis", {"token_address": instrument}),
        )
        return {"asset_class": asset_class, "instrument": instrument, "timeframe": cfg["tf"],
                "info": info, "ta": ta, "derivs": derivs, "bars": None}
    # Stocks / commodities: daily bars → local indicators, price from last close.
    start = (datetime.now(IST) - timedelta(days=260)).astimezone(ZoneInfo("UTC")).isoformat()
    raw = await tn_call_safe("historical_bars", {
        "instruments": [instrument], "asset_class": asset_class,
        "timeframe": cfg["tf"], "start": start,
    })
    bars = ((raw or {}).get("data") or {}).get(instrument) or []
    ta = compute_indicators_from_bars(bars)
    price = None
    if bars:
        try:
            price = float(sorted(bars, key=lambda c: c.get("t") or "")[-1]["close"])
        except (KeyError, TypeError, ValueError):
            price = None
    info = {"market_data": {"current_price": price}} if price is not None else None
    return {"asset_class": asset_class, "instrument": instrument, "timeframe": cfg["tf"],
            "info": info, "ta": ta, "derivs": None, "bars": bars}


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


def build_rule_setup(ticker: str, info: dict | None, ta: dict | None, derivs: dict | None,
                     min_score: float = SETUP_SCORE_MIN,
                     tp_rs: tuple[float, float] = (2.5, 4.0)) -> dict:
    """Deterministic trade setup from TN data — ATR-based levels, no LLM.

    Same output shape the Claude generator produced, so the embed builder and
    outcome tracking are untouched. Direction requires multi-signal alignment
    (|score| ≥ min_score of ±5.5 possible); levels are ATR-derived and coherent
    by construction: stop 1.5×ATR from entry, TP1/TP2 at tp_rs R-multiples.
    Default is 2.5R/4R — the house minimum-1:2.5 standard shared by !setup, the
    auto-signal scan, and the TN app agent's memory.
    """
    price = ((info or {}).get("market_data") or {}).get("current_price")
    atr = (_ind(ta).get("atr14") or {}).get("value")
    score, bull, bear = _setup_score(ta)
    fund = _btc_funding(derivs)
    fund_note = f"funding {fund:+.4f}%" if fund is not None else None

    if price is None or atr is None or not atr > 0:
        return {**NO_SETUP, "reasoning": "No clear setup — price/ATR data unavailable."}
    if abs(score) < min_score:
        detail = ", ".join((bull + bear)[:3]) or "signals flat"
        return {**NO_SETUP,
                "reasoning": f"No clear setup — ranging/choppy conditions (signal score {score:+.1f}: {detail})."}

    tp1_r, tp2_r = tp_rs
    direction = "LONG" if score > 0 else "SHORT"
    sign = 1 if direction == "LONG" else -1
    entry_lo, entry_hi = price - 0.25 * atr, price + 0.25 * atr
    stop = price - sign * 1.5 * atr
    risk = abs(price - stop)
    tp1 = price + sign * tp1_r * risk
    tp2 = price + sign * tp2_r * risk
    signals = bull if direction == "LONG" else bear
    reasoning = f"{'Bullish' if sign > 0 else 'Bearish'} alignment (score {score:+.1f}): " + ", ".join(signals[:4])
    if fund_note:
        reasoning += f"; {fund_note}"
    reasoning += f". Levels are 1.5×ATR risk with {tp1_r:g}R/{tp2_r:g}R targets."
    return {
        "has_setup": True,
        "direction": direction,
        "entry_zone": f"{_money(entry_lo)} – {_money(entry_hi)}",
        "stop_loss": _money(stop),
        "take_profit_1": _money(tp1),
        "take_profit_2": _money(tp2),
        "rr_ratio": f"{tp1_r} (TP1) / {tp2_r} (TP2)",
        "conviction": "High" if abs(score) >= SETUP_SCORE_HIGH else "Medium",
        "reasoning": reasoning,
    }


UNLOCK_WARN_DAYS = 7


def _unlock_warning(unlock: dict | None, now_ts: float) -> str | None:
    """'unlock in 5d: ~$7.3M (30.8% of supply already unlocked)' or None.

    token_unlock returns {"unlock_data": null} for assets with no vesting
    schedule (BTC/ETH/SOL…), and next_unlock_date may fall outside the
    requested window — so the ≤7d check happens here, not in the API args.
    """
    data = (unlock or {}).get("unlock_data")
    if not isinstance(data, dict):
        return None
    next_ts = data.get("next_unlock_date")
    if not isinstance(next_ts, (int, float)):
        return None
    days = (next_ts - now_ts) / 86400
    if not 0 <= days <= UNLOCK_WARN_DAYS:
        return None
    usd = sum(
        d.get("tokenAmountUsd") or 0
        for d in (data.get("next_unlocked_detail") or [])
        if isinstance(d, dict)
    )
    out = f"unlock in {max(1, round(days))}d"
    if usd > 0:
        out += f": ~${usd / 1e6:.1f}M" if usd >= 1e6 else f": ~${usd / 1e3:.0f}K"
    pct = data.get("total_unlocked_percentage")
    if isinstance(pct, (int, float)):
        out += f" ({pct:.1f}% of supply already unlocked)"
    return out


def _setup_channel_allowed(channel_id: int) -> bool:
    """Gate !setup by SETUP_ALLOWED_CHANNELS; empty set ⇒ allowed everywhere."""
    return not SETUP_ALLOWED_CHANNELS or channel_id in SETUP_ALLOWED_CHANNELS


# =============================================================================
# Auto-signal scan — the !setup engine run unattended on a 4h cron
# =============================================================================
# Pipeline (mirrors the strategy stored in the TN app's agent memory — keep the
# two in sync): regime read → RS rotation candidates aligned with regime →
# per-asset RVWAP structural bias (365d/90d rolling VWAP) → 4h signal score
# gated at SETUP_SCORE_HIGH → 2.5R/4R levels → post to CH_SIGNALS + track.

AUTO_SIGNAL_UNIVERSE = 15     # scanner universe size (ranked, full list returned)
AUTO_SIGNAL_PER_SIDE = 3      # leaders considered for longs / laggards for shorts
AUTO_TP_RS = (2.5, 4.0)       # min 1:2.5 R:R — the house standard (now also build_rule_setup's default)
RVWAP_WINDOWS = (7, 30, 90, 365)

# Fixed cross-asset watchlist for auto-signals: stocks/commodities have no RS
# scanner, so we scan a named list each run. Gate = RVWAP bias + score ≥ 4.0
# (no BTC regime — RVWAP structural bias is the trend filter for these).
# ponytail: per-asset-class lead-instrument gating (SPX→stocks, DXY→metals) is a
# future refinement; instrument-own RVWAP already encodes each one's trend.
SIGNALS_WATCHLIST = [
    t.strip() for t in os.environ.get("SIGNALS_WATCHLIST", "NVDA,AAPL,TSLA,GOLD,OIL").split(",") if t.strip()
]


def compute_rvwaps(candles: list[dict], windows: tuple = RVWAP_WINDOWS) -> dict[int, float]:
    """Rolling VWAP over the trailing N daily candles: Σ(close·volume)/Σvolume.

    Live candles may carry string OHLC values; unparseable rows are skipped.
    A token younger than the window yields the VWAP since listing (the natural
    anchored reading). Windows with zero total volume are omitted.
    """
    rows = []
    for c in candles or []:
        try:
            rows.append((float(c["close"]), float(c.get("volume") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    out: dict[int, float] = {}
    for w in windows:
        tail = rows[-w:]
        vol = sum(v for _c, v in tail)
        if vol > 0:
            out[w] = sum(c * v for c, v in tail) / vol
    return out


def rvwap_bias(price: float | None, rv: dict[int, float]) -> str:
    """Structural bias per the stored strategy: price above both 365d & 90d
    RVWAP ⇒ LONG, below both ⇒ SHORT, else MIXED (or missing data)."""
    lo, hi = rv.get(90), rv.get(365)
    if price is None or lo is None or hi is None:
        return "MIXED"
    if price > lo and price > hi:
        return "LONG"
    if price < lo and price < hi:
        return "SHORT"
    return "MIXED"


async def _emit_auto_signal(ticker: str, instrument: str, asset_class: str,
                            price: float, setup: dict, basis: str) -> bool:
    """Post one auto-signal embed to CH_SIGNALS and log it. Returns True if sent."""
    channel = bot.get_channel(CH_SIGNALS)
    if channel is None:
        print(f"[AUTOSIG] channel {CH_SIGNALS} not found")
        return False
    embed = build_setup_embed(ticker, float(price), setup)
    embed.title = f"🤖 {ticker} Auto Signal"
    embed.add_field(name="Basis", value=basis, inline=False)
    if asset_class != "crypto":
        embed.set_footer(text=f"{asset_class} · daily · " + FOOTER + " · not financial advice")
    msg = await channel.send(embed=embed)
    log_setup(setup, ticker, instrument, msg, source="auto", asset_class=asset_class)
    return True


async def _scan_watchlist() -> int:
    """Non-crypto leg of the auto-signal scan: each SIGNALS_WATCHLIST symbol on
    the daily, gated by its own RVWAP bias + score ≥ 4.0. Returns count posted."""
    posted = 0
    for sym in SIGNALS_WATCHLIST:
        asset_class, instrument = classify_asset(sym)
        if asset_class == "crypto":
            continue  # crypto is covered by the RS-rotation leg
        if _find_recent_open(instrument):
            continue
        g = await gather_setup_inputs(sym)
        price = ((g.get("info") or {}).get("market_data") or {}).get("current_price")
        if price is None or g.get("ta") is None:
            continue
        bias = rvwap_bias(price, compute_rvwaps(g.get("bars") or []))
        setup = build_rule_setup(sym, g["info"], g["ta"], None,
                                 min_score=SETUP_SCORE_HIGH, tp_rs=AUTO_TP_RS)
        if not setup.get("has_setup") or bias == "MIXED" or setup["direction"] != bias:
            continue
        if await _emit_auto_signal(sym, instrument, asset_class, float(price), setup,
                                   f"RVWAP bias {bias} · {asset_class} · daily score ≥ 4.0"):
            posted += 1
    return posted


async def auto_signal_scan() -> None:
    """Every 4h (UTC candle close): post high-conviction setups to CH_SIGNALS —
    crypto via regime + RS rotation + RVWAP, plus a fixed cross-asset watchlist
    (stocks/commodities) via RVWAP + score. Silence is the normal outcome."""
    if CH_SIGNALS is None:
        return
    d = await gather_raw([
        ("ta_btc",  "technical_analysis",   {"token_address": "bitcoin", "timeframe": "1d"}),
        ("derivs",  "derivatives_analysis", {"token_address": "bitcoin"}),
        ("indices", "market_index_price",   {"index": "all"}),
        ("scanner", "performance_scanner",  {"universe_size": AUTO_SIGNAL_UNIVERSE,
                                             "top_n": None, "lookback_days": 7}),
    ])
    regime, _reasons = detect_regime(d.get("ta_btc"), d.get("derivs"), d.get("indices"))
    rows = (d.get("scanner") or {}).get("leaderboard") or []
    if regime == "UNKNOWN" or not rows:
        print(f"[AUTOSIG] skipped — regime={regime}, scanner rows={len(rows)}")
        return
    # Leaders are long candidates unless RISK-OFF; laggards short unless RISK-ON.
    candidates: list[tuple[dict, str]] = []
    if regime != "RISK-OFF":
        candidates += [(r, "LONG") for r in rows[:AUTO_SIGNAL_PER_SIDE]]
    if regime != "RISK-ON":
        candidates += [(r, "SHORT") for r in rows[-AUTO_SIGNAL_PER_SIDE:]]
    posted = 0
    for row, want in candidates:
        ticker = _clean_ticker(row.get("ticker") or row.get("token"))
        if not ticker:
            continue
        token_id = _resolve_token(ticker)
        if _find_recent_open(token_id):
            continue  # already tracked — never double-signal
        start = (datetime.now(IST) - timedelta(days=RVWAP_WINDOWS[-1] + 5))
        info, ta4, bars = await asyncio.gather(
            tn_call_safe("basic_market_info", {"token_address": token_id}),
            tn_call_safe("technical_analysis", {"token_address": token_id, "timeframe": "4h"}),
            tn_call_safe("historical_bars", {
                "instruments": [token_id], "asset_class": "crypto", "timeframe": "1d",
                "start": start.astimezone(ZoneInfo("UTC")).isoformat(),
            }),
        )
        price = ((info or {}).get("market_data") or {}).get("current_price")
        candles = ((bars or {}).get("data") or {}).get(token_id) or []
        bias = rvwap_bias(price, compute_rvwaps(candles))
        setup = build_rule_setup(ticker, info, ta4, None,
                                 min_score=SETUP_SCORE_HIGH, tp_rs=AUTO_TP_RS)
        if not setup.get("has_setup") or setup["direction"] != want or bias != want:
            continue
        if await _emit_auto_signal(ticker, token_id, "crypto", float(price), setup,
                                   f"Regime {regime} · RVWAP bias {bias} · 7d RS rotation"):
            posted += 1
    wl_posted = await _scan_watchlist()
    print(f"[AUTOSIG] {regime}: {len(candidates)} crypto candidates → {posted} · "
          f"watchlist → {wl_posted} signal(s)")


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


def log_setup(setup: dict, ticker: str, coingecko_id: str, message, source: str = "manual",
              asset_class: str = "crypto") -> None:
    """Record a LONG/SHORT setup for outcome tracking. No-ops on 'no clear setup'
    or unparseable/incoherent levels (the embed still posts; it's just not tracked).
    source: "manual" (!setup) or "auto" (auto-signal scan) — kept per-trade so the
    public record can report the two engines separately. asset_class routes outcome
    resolution to the right bars (crypto 15m vs stock/commodity daily)."""
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
        "source": source, "asset_class": asset_class,
        "timestamp": datetime.now(IST).isoformat(),
        "discord_message_id": message.id, "discord_channel_id": message.channel.id,
        "status": "OPEN", "resolution_price": None, "resolved_at": None,
        "result_pct": None, "outcome_label": None,
    })
    _persist_setups()
    print(f"[TRACK] logged {ticker} {direction} entry={_money(entry)} stop={_money(stop)} tp1={_money(tp1)}")


def _expiry_hours(setup: dict) -> int:
    """Per-asset-class no-trigger expiry — crypto 48h, stocks/commodities longer
    (a daily setup can't resolve inside 48h)."""
    return ASSET_CFG.get(setup.get("asset_class", "crypto"), ASSET_CFG["crypto"])["expiry_h"]


def evaluate_setup(setup: dict, price: float | None):
    """Return (status, label) if an OPEN setup resolved, else None.
    WIN/LOSS need a price; EXPIRED is purely time-based (per asset class)."""
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
    if age_h >= _expiry_hours(setup):
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


def _parse_ts(x: str | None):
    """ISO timestamp → aware datetime. Handles both UTC 'Z' (TN candles) and
    +05:30 offsets (our records)."""
    if not isinstance(x, str) or not x:
        return None
    try:
        return datetime.fromisoformat(x.replace("Z", "+00:00"))
    except ValueError:
        return None


def evaluate_setup_candles(setup: dict, candles: list[dict]):
    """Wick-accurate resolution: scan 15m candles chronologically from entry time;
    the first candle whose high/low crosses a level decides the outcome.

    A spot check every 15 min misses intra-interval wicks — a stop that gets
    tagged and recovers would silently count as still-open (overstating the
    public win rate). Candle highs/lows close that hole. If ONE candle spans
    both stop and TP1, we can't know intra-candle order → conservative LOSS.

    Returns (status, label, resolution_price, resolved_at_iso) or None.
    Fills are assumed at the level itself (stop/TP1), not the candle close.
    """
    entry_ts = _parse_ts(setup.get("timestamp"))
    if entry_ts is None:
        return None
    long = setup["direction"] == "LONG"
    stop, tp1 = setup["stop_loss"], setup["tp1"]
    for c in sorted(candles, key=lambda c: c.get("t") or ""):
        ct = _parse_ts(c.get("t_close") or c.get("t"))
        if ct is None or ct <= entry_ts:
            continue  # candle closed before (or at) entry — pre-trade action
        try:
            hi, lo = float(c["high"]), float(c["low"])
        except (KeyError, TypeError, ValueError):
            continue
        hit_stop = lo <= stop if long else hi >= stop
        hit_tp = hi >= tp1 if long else lo <= tp1
        if hit_stop:  # both-in-one-candle also lands here → conservative LOSS
            return ("LOSS", "SL", stop, ct.astimezone(IST).isoformat())
        if hit_tp:
            return ("WIN", "TP1", tp1, ct.astimezone(IST).isoformat())
    return None


async def track_setups() -> None:
    """Every 15 min: resolve OPEN setups against candle wicks, announce + persist
    any that hit. Open setups are grouped by asset class so each gets the right
    bars (crypto 15m, stocks/commodities daily); one batched historical_bars call
    per class. Spot-check fallback + per-class expiry apply either way."""
    open_setups = [s for s in _SETUPS if s.get("status") == "OPEN"]
    if not open_setups:
        return
    changed = False
    by_class: dict[str, list[dict]] = {}
    for s in open_setups:
        by_class.setdefault(s.get("asset_class", "crypto"), []).append(s)

    for asset_class, group in by_class.items():
        cfg = ASSET_CFG.get(asset_class, ASSET_CFG["crypto"])
        ids = sorted({s["coingecko_id"] for s in group})
        oldest = min(_parse_ts(s["timestamp"]) or datetime.now(IST) for s in group)
        bars = await tn_call_safe("historical_bars", {
            "instruments": ids, "asset_class": asset_class, "timeframe": cfg["res_tf"],
            "start": oldest.astimezone(ZoneInfo("UTC")).isoformat(), "align": "outer",
        })
        series = (bars or {}).get("data") or {}
        for s in group:
            candles = series.get(s["coingecko_id"]) or []
            hit = evaluate_setup_candles(s, candles)
            if hit:
                status, label, res_price, resolved_at = hit
            else:
                # No wick trigger. Reference price: last candle close, else spot
                # (crypto only — basic_market_info doesn't cover stocks/commodities).
                ref = None
                if candles:
                    try:
                        ref = float(candles[-1]["close"])
                    except (KeyError, TypeError, ValueError):
                        ref = None
                if ref is None and asset_class == "crypto":
                    info = await tn_call_safe("basic_market_info", {"token_address": s["coingecko_id"]})
                    ref = (info or {}).get("market_data", {}).get("current_price")
                if ref is not None:  # keep a live price for the dashboard's open board
                    s["last_price"] = ref
                    changed = True
                outcome = evaluate_setup(s, ref)  # spot-level WIN/LOSS (fallback) + expiry
                if outcome is None:
                    continue
                status, label = outcome
                res_price, resolved_at = ref, datetime.now(IST).isoformat()
            s["status"] = status
            s["outcome_label"] = label
            s["resolution_price"] = res_price
            s["resolved_at"] = resolved_at
            s["result_pct"] = _trade_pct(s, res_price)
            changed = True
            print(f"[TRACK] {s['ticker']} {s['direction']} → {status} ({label}) at {_money(res_price)}")
            await _post_resolution(s)
    if changed:
        _persist_setups()


SETUP_REUSE_HOURS = 6


def _find_recent_open(coingecko_id: str, hours: float = SETUP_REUSE_HOURS) -> dict | None:
    """An OPEN setup for this asset younger than `hours`, if any — !setup reuses
    it instead of double-logging near-identical trades into the track record."""
    now = datetime.now(IST)
    for s in reversed(_SETUPS):  # newest first
        if s.get("status") != "OPEN" or s.get("coingecko_id") != coingecko_id:
            continue
        ts = _parse_ts(s.get("timestamp"))
        if ts and (now - ts).total_seconds() < hours * 3600:
            return s
    return None


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
    # Manual (!setup) vs auto (4h scan) breakdown — only surfaced once any
    # auto signal exists, so the embed is unchanged until the feature is used.
    auto_line = None
    autos = [s for s in setups if s.get("source") == "auto"]
    if autos:
        def _wl(pool):
            return (sum(1 for s in pool if s.get("status") == "WIN"),
                    sum(1 for s in pool if s.get("status") == "LOSS"))
        aw, al = _wl(autos)
        mw, ml = _wl([s for s in setups if s.get("source") != "auto"])
        auto_line = f"manual {mw}W/{ml}L · auto {aw}W/{al}L"
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
        "auto_line": auto_line,
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
    if stats.get("auto_line"):
        e.add_field(name="By engine", value=stats["auto_line"], inline=False)
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
    src = f"{SESSION_LABELS[session]} brief data" if session in SESSION_LABELS else "15-min market check"
    e.set_footer(text=f"Auto-detected from {src} | {now:%b %d, %H:%M IST}")
    e.timestamp = now
    try:
        await channel.send(embed=e)
    except Exception as ex:
        print(f"[REGIME] failed to post shift: {type(ex).__name__}: {ex}")


# A regime flip must be seen on this many CONSECUTIVE checks before alerting.
# At 15-min cadence a metric hovering on a rule boundary (RSI ≈ 40, price at
# the 20d MA) would otherwise flap NEUTRAL↔RISK-OFF alerts all day.
REGIME_CONFIRM_CHECKS = 2


async def check_regime_shift(session: str, ta: dict | None = None, derivs: dict | None = None) -> None:
    """Derive the regime by rule and alert #regime-outlook on a CONFIRMED flip vs
    the stored baseline. Runs every 15 min (interval job) and on every brief
    (which passes its own daily TA + derivatives in). Never raises.

    Confirmation: a reading that differs from the baseline is held as pending;
    only REGIME_CONFIRM_CHECKS consecutive identical differing reads alert and
    move the baseline. A read matching the baseline clears any pending flip.
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
        now = datetime.now(IST)
        prev = _LAST_REGIME.get("regime", "UNKNOWN")
        if prev in (None, "UNKNOWN"):
            _LAST_REGIME.update({"regime": regime, "timestamp": now.isoformat(), "reasons": reasons,
                                 "pending_regime": None, "pending_count": 0})
            _persist_regime()
            print(f"[REGIME] {session}: baseline set to {regime} (no alert)")
            return
        if regime == prev:
            _LAST_REGIME.update({"timestamp": now.isoformat(), "reasons": reasons,
                                 "pending_regime": None, "pending_count": 0})
            _persist_regime()
            print(f"[REGIME] {session}: {regime} (stable) — no alert")
            return
        # Differs from baseline — confirm across consecutive checks before alerting.
        count = (_LAST_REGIME.get("pending_count", 0) + 1
                 if _LAST_REGIME.get("pending_regime") == regime else 1)
        if count >= REGIME_CONFIRM_CHECKS:
            _LAST_REGIME.update({"regime": regime, "timestamp": now.isoformat(), "reasons": reasons,
                                 "pending_regime": None, "pending_count": 0})
            _persist_regime()
            await _post_regime_shift(session, prev, regime, reasons)
            print(f"[REGIME] SHIFT {prev} -> {regime} ({session}, confirmed ×{count}) — alerted")
        else:
            _LAST_REGIME.update({"timestamp": now.isoformat(),
                                 "pending_regime": regime, "pending_count": count})
            _persist_regime()
            print(f"[REGIME] {session}: {regime} pending ({count}/{REGIME_CONFIRM_CHECKS}) — baseline stays {prev}")
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
    _DASH_CACHE["brief"] = {"session": SESSION_LABELS.get(session, session),
                            "text": text, "at": datetime.now(IST).isoformat()}
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
    scheduler.add_job(
        check_regime_shift,
        IntervalTrigger(minutes=15),
        args=["interval"],
        id="regime_15m",
        replace_existing=True,
    )
    if CH_SIGNALS is not None:
        # 4h candles close on UTC boundaries; :10 lets the closed candle settle.
        scheduler.add_job(
            auto_signal_scan,
            CronTrigger(hour="0,4,8,12,16,20", minute=10, timezone=ZoneInfo("UTC")),
            id="auto_signals",
            replace_existing=True,
        )
    scheduler.add_job(
        refresh_dashboard_cache,
        IntervalTrigger(minutes=15),
        id="dashboard_cache",
        next_run_time=datetime.now(IST),   # warm the cache at startup, not 15 min later
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
        "last_build_error": tn_state["last_synth_error"],
        "regime": _LAST_REGIME.get("regime", "UNKNOWN"),
    })


TRACK_RECORD_RECENT_N = 10


# =============================================================================
# Dashboard feed — /api/live: open signals + cross-asset lead-lag map + brief
# =============================================================================
# The bot is the source of truth (it already computes scores/levels/bias); the
# dashboard renders. Cross-asset map + brief are cached (refreshed on a job) so
# the endpoint stays fast; open setups come straight from in-memory _SETUPS.

CROSS_ASSET_CRYPTO = [("BTC", "bitcoin"), ("ETH", "ethereum"), ("SOL", "solana")]
CROSS_ASSET_STOCKS = ["NVDA", "AAPL", "TSLA"]
CROSS_ASSET_COMMOD = [("GOLD", "gold"), ("OIL", "oil")]
CROSS_ASSET_INDICES = {"gspc": "SPX", "ndx": "NDX", "dxy": "DXY", "tnx": "US10Y", "vix": "VIX"}

# Cached dashboard snapshot (refreshed every 15 min by refresh_dashboard_cache).
_DASH_CACHE: dict = {"crossasset": None, "brief": None}


def _pct_change(bars: list, n: int = 7) -> float | None:
    """% change over the trailing n daily bars (last close vs n bars back)."""
    closes = []
    for b in sorted(bars or [], key=lambda c: c.get("t") or ""):
        try:
            closes.append(float(b["close"]))
        except (KeyError, TypeError, ValueError):
            continue
    if len(closes) < n + 1 or closes[-1 - n] == 0:
        return None
    return (closes[-1] / closes[-1 - n] - 1) * 100


async def build_cross_asset_map() -> dict:
    """Skew-equivalent lead-lag read across crypto / equities / commodities /
    macro, composed from keyless tools (no proprietary agent). 7d % for anything
    with daily bars; indices show 24h from market_index_price. Returns tiles +
    a one-line lead/lag summary. Degrades per-group on tool failure."""
    start = (datetime.now(IST) - timedelta(days=14)).astimezone(ZoneInfo("UTC")).isoformat()
    crypto, stocks, commod, indices = await asyncio.gather(
        tn_call_safe("historical_bars", {"instruments": [i for _l, i in CROSS_ASSET_CRYPTO],
                                          "asset_class": "crypto", "timeframe": "1d", "start": start}),
        tn_call_safe("historical_bars", {"instruments": CROSS_ASSET_STOCKS,
                                         "asset_class": "stock", "timeframe": "1d", "start": start}),
        tn_call_safe("historical_bars", {"instruments": [i for _l, i in CROSS_ASSET_COMMOD],
                                         "asset_class": "commodity", "timeframe": "1d", "start": start}),
        tn_call_safe("market_index_price", {"index": "all"}),
    )
    tiles = []
    for group, series, members in (
        ("Crypto", (crypto or {}).get("data") or {}, CROSS_ASSET_CRYPTO),
        ("Commodities", (commod or {}).get("data") or {}, CROSS_ASSET_COMMOD),
    ):
        for label, inst in members:
            pct = _pct_change(series.get(inst) or [])
            if pct is not None:
                tiles.append({"group": group, "label": label, "pct": round(pct, 1), "horizon": "7d"})
    stock_series = (stocks or {}).get("data") or {}
    for sym in CROSS_ASSET_STOCKS:
        pct = _pct_change(stock_series.get(sym) or [])
        if pct is not None:
            tiles.append({"group": "Equities", "label": sym, "pct": round(pct, 1), "horizon": "7d"})
    by_index = {p.get("index"): p for p in (indices or {}).get("prices") or []}
    for idx, label in CROSS_ASSET_INDICES.items():
        p = by_index.get(idx) or {}
        latest = p.get("latest") or {}
        if latest.get("change_percentage") is not None:
            tiles.append({"group": "Macro", "label": label,
                          "pct": round(latest["change_percentage"], 1), "horizon": "24h",
                          "level": latest.get("close")})

    # Lead/lag read over the 7d groups (Macro is context, not ranked).
    groups = {}
    for t in tiles:
        if t["horizon"] == "7d":
            groups.setdefault(t["group"], []).append(t["pct"])
    avgs = {g: sum(v) / len(v) for g, v in groups.items() if v}
    lead = None
    if avgs:
        top = max(avgs, key=avgs.get)
        bot_g = min(avgs, key=avgs.get)
        lead = f"{top} leading ({avgs[top]:+.1f}% 7d)" + (
            f" · {bot_g} lagging ({avgs[bot_g]:+.1f}%)" if bot_g != top else "")
        dxy = next((t["pct"] for t in tiles if t["label"] == "DXY"), None)
        if dxy is not None and abs(dxy) >= 0.3:
            lead += f" · DXY {dxy:+.1f}% ({'headwind' if dxy > 0 else 'tailwind'} for risk)"
    return {"tiles": tiles, "lead": lead, "at": datetime.now(IST).isoformat()}


async def refresh_dashboard_cache() -> None:
    """Recompute the cross-asset map into the cache (every 15 min). Best-effort."""
    try:
        _DASH_CACHE["crossasset"] = await build_cross_asset_map()
        print(f"[DASH] cross-asset map refreshed ({len((_DASH_CACHE['crossasset'] or {}).get('tiles', []))} tiles)")
    except Exception as e:
        print(f"[DASH] map refresh failed: {type(e).__name__}: {e}")


def build_live_payload() -> dict:
    """Public JSON for the dashboard's live panels: open setups (from memory),
    the cached cross-asset map, and the latest session brief snapshot."""
    open_setups = [{
        "ticker": s.get("ticker"),
        "asset_class": s.get("asset_class", "crypto"),
        "direction": s.get("direction"),
        "source": s.get("source", "manual"),
        "conviction": s.get("conviction"),
        "entry_price": s.get("entry_price"),
        "stop_loss": s.get("stop_loss"),
        "tp1": s.get("tp1"),
        "tp2": s.get("tp2"),
        "rr_ratio": s.get("rr_ratio"),
        "opened_at": s.get("timestamp"),
        "last_price": s.get("last_price"),
    } for s in _SETUPS if s.get("status") == "OPEN"]
    open_setups.sort(key=lambda s: s.get("opened_at") or "", reverse=True)
    return {
        "generated_at": datetime.now(IST).isoformat(),
        "open_setups": open_setups,
        "crossasset": _DASH_CACHE.get("crossasset"),
        "brief": _DASH_CACHE.get("brief"),
    }


def build_track_record_payload() -> dict:
    """Public JSON for the dashboard: aggregate stats + recent resolved calls.
    Same numbers !winrate shows — one source of truth for the track record."""
    resolved = [s for s in _SETUPS if s.get("status") in ("WIN", "LOSS", "EXPIRED")]
    resolved.sort(key=lambda s: s.get("resolved_at") or "", reverse=True)
    recent = [{
        "ticker": s.get("ticker"),
        "direction": s.get("direction"),
        "status": s.get("status"),
        "entry_price": s.get("entry_price"),
        "resolution_price": s.get("resolution_price"),
        "result_pct": s.get("result_pct"),
        "conviction": s.get("conviction"),
        "opened_at": s.get("timestamp"),
        "resolved_at": s.get("resolved_at"),
    } for s in resolved[:TRACK_RECORD_RECENT_N]]
    return {
        "generated_at": datetime.now(IST).isoformat(),
        "stats": compute_winrate(_SETUPS),
        "recent": recent,
    }


async def _handle_track_record(_request: web.Request) -> web.Response:
    return web.json_response(build_track_record_payload(), headers={
        "Access-Control-Allow-Origin": "*",         # public data, dashboard consumes it
        "Cache-Control": "public, max-age=300",
    })


async def _handle_live(_request: web.Request) -> web.Response:
    return web.json_response(build_live_payload(), headers={
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=120",
    })


async def start_webhook_server() -> None:
    """Bring up the aiohttp server on $PORT. Idempotent."""
    global _web_runner
    if _web_runner is not None:
        return
    app = web.Application()
    app.router.add_get("/healthz", _handle_healthz)
    app.router.add_get("/api/track-record", _handle_track_record)
    app.router.add_get("/api/live", _handle_live)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    _web_runner = runner
    print(f"[Webhook] /healthz + /api/track-record listening on 0.0.0.0:{WEBHOOK_PORT}")


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

    asset_class, instrument = classify_asset(ticker)

    # Dedupe: an open tracked setup for this asset < 6h old is reused, not
    # re-generated — repeated !setup calls must not stack near-identical
    # trades into the public track record.
    existing = _find_recent_open(instrument)
    if existing:
        jump = ""
        if ctx.guild:
            jump = (f" · [original](https://discord.com/channels/{ctx.guild.id}/"
                    f"{existing['discord_channel_id']}/{existing['discord_message_id']})")
        await ctx.send(
            f"♻️ **{existing['ticker']} {existing['direction']}** already open from "
            f"{_format_delta(_parse_ts(existing['timestamp']))} — entry {_money(existing['entry_price'])}, "
            f"SL {_money(existing['stop_loss'])}, TP1 {_money(existing['tp1'])}. "
            f"Tracking continues; `!winrate` for the record{jump}."
        )
        return

    status = await ctx.send(f"⏳ Building **{ticker}** setup…")

    # Router: crypto = 4h TN TA; stocks/commodities = daily bars → local indicators.
    now_ts = datetime.now(IST).timestamp()
    g, unlock = await asyncio.gather(
        gather_setup_inputs(ticker),
        tn_call_safe("token_unlock", {
            "token_address": instrument,
            "event_after_timestamp": int(now_ts),
            "event_before_timestamp": int(now_ts) + UNLOCK_WARN_DAYS * 86400,
        }) if asset_class == "crypto" else _none_coro(),
    )
    info, ta, derivs = g["info"], g["ta"], g["derivs"]
    price = (info or {}).get("market_data", {}).get("current_price")
    if price is None:
        hint = ("Try `BTC`/`ETH`/`SOL`, or a CoinGecko id (e.g. `!setup bitcoin`)."
                if asset_class == "crypto" else
                f"No daily data for `{ticker}` as a {asset_class}. Check the symbol "
                f"(stocks like `NVDA`, commodities like `GOLD`/`OIL`).")
        await status.edit(content=f"⚠️ Couldn't find market data for `{ticker}`. {hint}")
        return

    try:
        setup = build_rule_setup(ticker, info, ta, derivs)  # deterministic, no LLM
    except Exception as e:
        tn_state["last_synth_error"] = f"{type(e).__name__}: {e}"
        print(f"[SETUP] {ticker} failed: {type(e).__name__}: {e}")
        await status.edit(content=f"⚠️ Setup generation failed ({type(e).__name__}). Run `!health`.")
        return

    embed = build_setup_embed(ticker, float(price), setup)
    if asset_class != "crypto":
        embed.set_footer(text=f"{asset_class} · daily · " + FOOTER + " · not financial advice")
    # Unlock cliff inside the tracking horizon is a risk flag, never a signal —
    # it must not touch the deterministic score (mirrored in TN app memory).
    warn = _unlock_warning(unlock, now_ts)
    if warn:
        embed.add_field(name="⚠️ Unlock risk", value=warn[:200], inline=False)
    await status.edit(content=None, embed=embed)
    log_setup(setup, ticker, instrument, status, asset_class=asset_class)


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
    e.add_field(name="Storage", value=_storage_status(), inline=False)
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
