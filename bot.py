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

import os
import re
import json
import asyncio
import traceback
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

TN_ENDPOINT = "https://api.adventai.io/api/discovery-agents/sse/v2/streams"
TN_THREAD   = os.environ.get("TN_THREAD_ID", "78536e88-e440-43dd-a61d-584640f8792b")

IST = ZoneInfo("Asia/Kolkata")

CH = {
    "claude":  int(os.environ["CH_CLAUDE_INTEGRATION"]),
    "asia":    int(os.environ["CH_ASIA_SESSION"]),
    "london":  int(os.environ["CH_LONDON_SESSION"]),
    "us":      int(os.environ["CH_US_SESSION"]),
    "regime":  int(os.environ["CH_REGIME_OUTLOOK"]),
    "trades":  int(os.environ["CH_TRADES"]),
}

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

# Mutable token holder (so refresh can update it)
token_store = {"access": TN_TOKEN, "refresh": TN_REFRESH}

# --- TrueNorth SSE query ---
async def query_truenorth(prompt: str, timeout_read: float = 180.0) -> str:
    """Send prompt to TrueNorth SSE endpoint, return assembled text."""
    headers = {
        "Authorization": f"Bearer {token_store['access']}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "X-Accel-Buffering": "no",
    }
    body = {
        "query": prompt,
        "thread_id": TN_THREAD,
    }
    result_chunks = []
    try:
        timeout = httpx.Timeout(30.0, read=timeout_read)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", TN_ENDPOINT, headers=headers, json=body) as resp:
                print(f"[TN HTTP] status={resp.status_code}")
                if resp.status_code != 200:
                    body_text = ""
                    async for chunk in resp.aiter_text():
                        body_text += chunk
                        if len(body_text) > 500:
                            break
                    print(f"[TN HTTP] error body: {body_text[:500]}")
                    return ""
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
                                    result_chunks.append(content)
                            elif "content" in obj:
                                result_chunks.append(obj["content"])
                            elif "detail" in obj:
                                print(f"[TN SSE] API error: {json.dumps(obj['detail'])[:300]}")
                        except json.JSONDecodeError as e:
                            print(f"[TN SSE] JSON error: {e} | raw: {data[:100]}")
    except Exception as e:
        print(f"[TN ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()
    result = "".join(result_chunks).strip()
    print(f"[TN RESULT] {len(result)} chars")
    return result

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
async def refresh_tn_token():
    """Refresh TN access token via Privy. Skips if no refresh token."""
    if not token_store["refresh"]:
        print("[TokenRefresh] TN_REFRESH_TOKEN not set -- skipping")
        return
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://auth.privy.io/api/v1/sessions/refresh",
                json={"refresh_token": token_store["refresh"]},
                headers={
                    "privy-app-id": PRIVY_APP_ID,
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                token_store["access"] = data.get("token", token_store["access"])
                new_refresh = data.get("refresh_token")
                if new_refresh:
                    token_store["refresh"] = new_refresh
                print("[TokenRefresh] Token refreshed")
            else:
                print(f"[TokenRefresh] status={resp.status_code} body={resp.text[:200]}")
    except Exception as e:
        print(f"[TokenRefresh] {e}")

# --- Response parsers ---
def parse_trades_from_text(text: str) -> list[dict]:
    """Extract structured trades from TrueNorth markdown response."""
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
            low = line.lower().strip()
            cells = [c.strip() for c in line.split("|") if c.strip()]
            if len(cells) >= 2:
                label = cells[0].lower()
                val = cells[1]
                if "entry" in label and "stop" not in label:
                    trade["entry"] = val
                elif "stop" in label or "sl" == label:
                    trade["sl"] = val
                elif "take profit" in label or "tp" == label or "target" in label:
                    trade["tp"] = val
                elif "r:r" in label or "r/r" in label or ("risk" in label and "reward" in label):
                    trade["rr"] = val
            stripped = line.strip()
            if stripped and (stripped[0] in "•*-") and len(stripped) > 2:
                note = stripped.lstrip("•*- ").strip()
                if note and "entry" not in note.lower()[:6]:
                    trade["notes"].append(note)
        if not trade["entry"]:
            m = re.search(r'[Ee]ntry[:\s]*\$?([\d,.]+)', block)
            if m: trade["entry"] = f"${m.group(1)}"
        if not trade["sl"]:
            m = re.search(r'[Ss]top\s*[Ll]oss[:\s]*\$?([\d,.]+)', block)
            if m: trade["sl"] = f"${m.group(1)}"
        if not trade["tp"]:
            m = re.search(r'[Tt]ake\s*[Pp]rofit[:\s]*\$?([\d,.]+)', block)
            if m: trade["tp"] = f"${m.group(1)}"
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
            e.add_field(name="Entry Price", value=f"${t['entry']}", inline=True)
        if t["sl"]:
            e.add_field(name="Stop/Loss", value=f"${t['sl']}", inline=True)
        if t["tp"]:
            e.add_field(name="Take Profit", value=f"${t['tp']}", inline=True)
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
        CronTrigger(hour="*/12", minute=0, timezone=IST),
        id="token_refresh",
        replace_existing=True,
    )

# --- Bot events ---
@bot.event
async def on_ready():
    print(f"DCT TrueNorth Bot online as {bot.user}")
    setup_scheduler()
    scheduler.start()
    print(f"[SCHED] {len(SCHEDULE)} session briefs + regime + token refresh scheduled")
    ch = bot.get_channel(CH["claude"])
    if ch:
        e = discord.Embed(title="🤖 DCT TrueNorth Bot is online!", color=COLOR_INFO)
        e.description = "Connected to TrueNorth AI + Claude."
        e.add_field(
            name="💬 #claude-integration",
            value="Chat here. Finance -> TrueNorth. General -> Claude.",
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
            value="!brief asia/london/us · !regime · !trades · !refreshtoken",
            inline=False,
        )
        e.set_footer(text=FOOTER)
        await ch.send(embed=e)

FINANCE_KEYWORDS = {
    "$", "btc", "eth", "sol", "bitcoin", "ethereum", "solana",
    "market", "price", "trade", "long", "short", "chart",
    "bullish", "bearish", "support", "resistance", "volume",
    "liquidation", "funding", "oi", "open interest", "perp",
    "futures", "options", "puts", "calls", "vix", "spx",
    "nasdaq", "s&p", "fed", "cpi", "fomc", "yields",
    "dxy", "gold", "oil", "macro", "regime",
}

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    await bot.process_commands(message)
    if message.channel.id != CH["claude"]:
        return
    if message.content.startswith("!"):
        return
    content_lower = message.content.lower()
    is_finance = any(kw in content_lower for kw in FINANCE_KEYWORDS)
    async with message.channel.typing():
        if is_finance:
            result = await query_truenorth(message.content)
            if result:
                for i in range(0, len(result), 1900):
                    await message.reply(result[i:i+1900], mention_author=False)
            else:
                fallback = ask_claude(message.content)
                await message.reply(fallback, mention_author=False)
        else:
            result = ask_claude(message.content)
            await message.reply(result, mention_author=False)

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

@bot.command(name="refreshtoken")
async def manual_refresh(ctx: commands.Context):
    """Manually refresh TrueNorth token."""
    await ctx.send("🔄 Attempting token refresh...")
    await refresh_tn_token()
    test = await query_truenorth("ping")
    if test:
        await ctx.send("✅ Token is valid -- TrueNorth responded.")
    else:
        await ctx.send(
            "❌ Token refresh failed or TN did not respond.\n"
            "Get a fresh token from privy:token in localStorage at "
            "app.true-north.xyz and update TN_TOKEN in Railway."
        )

# --- Entry point ---
if __name__ == "__main__":
    print("[BOOT] Starting DCT TrueNorth Bot...")
    bot.run(DISCORD_TOKEN)
