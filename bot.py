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

# --- Embed colors ---
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
}


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
