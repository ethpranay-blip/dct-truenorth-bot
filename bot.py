import os, json, asyncio, httpx, pytz
from datetime import datetime
from dotenv import load_dotenv
import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import anthropic

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
TN_REFRESH_TOKEN = os.getenv("TN_REFRESH_TOKEN")
TN_ENDPOINT = "https://api.adventai.io/api/discovery-agents/sse/v2/streams"
TN_THREAD_ID = "78536e88-e440-43dd-a61d-584640f8792b"
PRIVY_APP_ID = "cm6afcumv0688a6x3r78jkx7v"
PRIVY_REFRESH_URL = "https://auth.privy.io/api/v1/sessions"

CH = {
    "claude": int(os.getenv("CH_CLAUDE_INTEGRATION")),
    "asia":   int(os.getenv("CH_ASIA_SESSION")),
    "london": int(os.getenv("CH_LONDON_SESSION")),
    "us":     int(os.getenv("CH_US_SESSION")),
    "regime": int(os.getenv("CH_REGIME_OUTLOOK")),
    "trades": int(os.getenv("CH_TRADES")),
}

IST = pytz.timezone("Asia/Kolkata")

SESSIONS = {
    "asia":   {"open": (5, 30),  "close": (14, 30), "channel": "asia"},
    "london": {"open": (13, 30), "close": (22, 30), "channel": "london"},
    "us":     {"open": (18, 30), "close": (3, 30),  "channel": "us"},
}

trade_log = []
chat_history = {}

# In-memory token store -- refreshed on startup and every 12 hours
_tn_access_token = os.getenv("TN_TOKEN", "")


async def refresh_tn_token():
    """Call Privy /sessions to exchange refresh token for a new access token.
    Returns True on success, False on failure."""
    global _tn_access_token
    if not TN_REFRESH_TOKEN:
        print("[TokenRefresh] TN_REFRESH_TOKEN not set -- skipping refresh")
        return False
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                PRIVY_REFRESH_URL,
                headers={
                    "privy-app-id": PRIVY_APP_ID,
                    "Content-Type": "application/json",
                },
                json={"refresh_token": TN_REFRESH_TOKEN},
            )
        if resp.status_code == 200:
            data = resp.json()
            new_token = (
                data.get("token")
                or data.get("access_token")
                or data.get("identity_token", "")
            )
            if new_token:
                _tn_access_token = new_token
                print("[TokenRefresh] Token refreshed at " + datetime.now(IST).isoformat())
                return True
            else:
                print("[TokenRefresh] Refresh response missing token. Keys: " + str(list(data.keys())))
        else:
            print("[TokenRefresh] Refresh failed HTTP " + str(resp.status_code) + ": " + resp.text[:200])
    except Exception as e:
        print("[TokenRefresh] Exception: " + str(e))
    return False


async def query_truenorth(prompt):
    headers = {
        "Authorization": "Bearer " + _tn_access_token,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "X-Accel-Buffering": "no",
    }
    body = {
        "query": prompt,
        "stream": True,
        "allow_additional_tools": True,
        "thread_id": TN_THREAD_ID,
        "timezone": "Asia/Kolkata",
        "thinking": False,
    }
    result = ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0)) as client:
            async with client.stream("POST", TN_ENDPOINT, headers=headers, json=body) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                            # Try common response fields
                            text_chunk = (
                                obj.get("content") or
                                obj.get("text") or
                                obj.get("message") or
                                obj.get("delta", {}).get("content") or
                                obj.get("choices", [{}])[0].get("delta", {}).get("content") or
                                obj.get("choices", [{}])[0].get("text") or
                                ""
                            )
                            result += text_chunk
                            if text_chunk:
                                print(f"[TN SSE] got text chunk: {text_chunk[:80]}")
                            else:
                                print(f"[TN SSE] unrecognised shape: {list(obj.keys())}")
                        except Exception:
                            pass
    except Exception as e:
        result = "[TrueNorth error: " + str(e) + "]"
    return result.strip() or "[No response from TrueNorth]"


def query_claude(messages, system=""):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    kwargs = {"model": "claude-haiku-4-5", "max_tokens": 1024, "messages": messages}
    if system:
        kwargs["system"] = system
    resp = client.messages.create(**kwargs)
    return resp.content[0].text


def pre_session_prompt(session):
    return (
        "Run a full pre-" + session.upper() + " session brief. Include: "
        "1) Market regime check for $BTC, $GOLD, $OIL "
        "2) Key levels to watch this session "
        "3) Top macro events or news to watch "
        "4) 3 high-conviction tokens to trade with direction and bias. Keep it sharp and actionable."
    )


def post_session_prompt(session):
    return (
        "Run a full post-" + session.upper() + " session debrief. Include: "
        "1) What moved and why -- $BTC, $GOLD, $OIL recap "
        "2) Key levels that held or broke "
        "3) Any macro surprises or narrative shifts "
        "4) What to carry into the next session. Be concise and data-driven."
    )


def regime_update_prompt():
    return (
        "Give me a regime and macro update. Include: "
        "1) Current market regime (risk-on / risk-off / neutral) "
        "2) Key geopolitical events, wars, or news impacting markets RIGHT NOW "
        "3) $BTC macro stance "
        "4) DXY, rates, bonds context "
        "5) One-line global outlook. Keep it under 400 words."
    )


def trades_prompt():
    return (
        "Give me 3 high-conviction trade setups RIGHT NOW. "
        "For each trade include: Token ($SYMBOL), Direction (LONG/SHORT), "
        "Entry zone, Stop Loss, Take Profit (minimum 1:2 RR), "
        "and a 2-sentence reason backed by TrueNorth data. "
        "Also include $BTC directional bias. Format cleanly."
    )


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=IST)


async def send_session_brief(session_key, brief_type):
    session = SESSIONS[session_key]
    channel = bot.get_channel(CH[session["channel"]])
    if not channel:
        return
    label = "U0001f4cb PRE-SESSION BRIEF" if brief_type == "pre" else "U0001f4ca POST-SESSION DEBRIEF"
    emoji = {"asia": "U0001f30f", "london": "U0001f1ecU0001f1e7", "us": "U0001f1faU0001f1f8"}[session_key]
    await channel.send(emoji + " **" + label + " -- " + session_key.upper() + " SESSION**\n*Querying TrueNorth...*")
    prompt = pre_session_prompt(session_key) if brief_type == "pre" else post_session_prompt(session_key)
    tn = await query_truenorth(prompt)
    for chunk in [tn[i:i+1900] for i in range(0, len(tn), 1900)]:
        await channel.send(chunk)
    if brief_type == "pre":
        await asyncio.sleep(2)
        trades_ch = bot.get_channel(CH["trades"])
        if trades_ch:
            await trades_ch.send(emoji + " **" + session_key.upper() + " SESSION SETUPS**\n*Fetching from TrueNorth...*")
            tr = await query_truenorth(trades_prompt())
            for chunk in [tr[i:i+1900] for i in range(0, len(tr), 1900)]:
                await trades_ch.send(chunk)


async def send_regime_update(trigger="scheduled"):
    channel = bot.get_channel(CH["regime"])
    if not channel:
        return
    await channel.send("U0001f310 **REGIME & MACRO UPDATE**\n*Querying TrueNorth...*")
    resp = await query_truenorth(regime_update_prompt())
    for chunk in [resp[i:i+1900] for i in range(0, len(resp), 1900)]:
        await channel.send(chunk)


def schedule_sessions():
    offsets = {
        "asia":   {"pre": (5, 15),  "post": (14, 45)},
        "london": {"pre": (13, 15), "post": (22, 45)},
        "us":     {"pre": (18, 15), "post": (3, 45)},
    }
    for sess, times in offsets.items():
        ph, pm = times["pre"]
        scheduler.add_job(send_session_brief, "cron", hour=ph, minute=pm,
                          args=[sess, "pre"], id="pre_" + sess, replace_existing=True)
        posth, postm = times["post"]
        scheduler.add_job(send_session_brief, "cron", hour=posth, minute=postm,
                          args=[sess, "post"], id="post_" + sess, replace_existing=True)
    for i, (h, m) in enumerate([(5, 15), (13, 15), (18, 15)]):
        scheduler.add_job(send_regime_update, "cron", hour=h, minute=m,
                          args=["scheduled"], id="regime_" + str(i), replace_existing=True)
    # Refresh TrueNorth token every 12 hours
    scheduler.add_job(refresh_tn_token, "interval", hours=12,
                      id="tn_token_refresh", replace_existing=True)


@bot.event
async def on_ready():
    print("DCT TrueNorth Bot online as " + str(bot.user))
    # Refresh token on startup before any API calls
    await refresh_tn_token()
    schedule_sessions()
    scheduler.start()
    ch = bot.get_channel(CH["claude"])
    if ch:
        await ch.send(
            "U0001f916 **DCT TrueNorth Bot is online!**\n"
            "Connected to TrueNorth + Claude.\n\n"
            "**Channels:**\n"
            "• `#claude-integration` -- Chat with me. Finance = TrueNorth. General = Claude.\n"
            "• Session channels -- Auto briefs 15min before/after each session\n"
            "• `#regime-outlook` -- Macro updates before every session\n"
            "• `#trades` -- 3 setups per session\n\n"
            "Type `!brief asia/london/us` or `!regime` or `!trades` to trigger manually."
        )


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    cid = message.channel.id
    if cid == CH["claude"]:
        async with message.channel.typing():
            user_msg = message.content.strip()
            if cid not in chat_history:
                chat_history[cid] = []
            finance_kw = [
                "$", "btc", "bitcoin", "eth", "sol", "crypto", "trade", "market",
                "stock", "forex", "gold", "oil", "chart", "level", "support",
                "resistance", "breakout", "trend", "regime", "macro", "setup",
                "long", "short", "entry", "tp", "sl", "bias", "outlook",
            ]
            is_finance = any(k in user_msg.lower() for k in finance_kw)
            if is_finance:
                tn_data = await query_truenorth(user_msg)
                system = (
                    "You are a professional trading analyst. TrueNorth provided the market data below. "
                    "Synthesize into a clear actionable response. Use $SYMBOL format."
                )
                reply = query_claude(
                    [{"role": "user", "content": "User asked: " + user_msg + "\n\nTrueNorth data:\n" + tn_data}],
                    system=system,
                )
                await message.reply("U0001f4e1 *via TrueNorth + Claude*\n\n" + reply)
            else:
                chat_history[cid].append({"role": "user", "content": user_msg})
                if len(chat_history[cid]) > 20:
                    chat_history[cid] = chat_history[cid][-20:]
                reply = query_claude(
                    chat_history[cid],
                    system="You are a helpful assistant in a crypto trading Discord. Be concise.",
                )
                chat_history[cid].append({"role": "assistant", "content": reply})
                await message.reply(reply)
    elif cid in [CH["asia"], CH["london"], CH["us"], CH["regime"]]:
        async with message.channel.typing():
            resp = await query_truenorth(message.content.strip())
            chunks = [resp[i:i+1900] for i in range(0, len(resp), 1900)]
            await message.reply(chunks[0])
            for c in chunks[1:]:
                await message.channel.send(c)
    elif cid == CH["trades"]:
        if message.content.lower().startswith("result:"):
            trade_log.append({"timestamp": datetime.now(IST).isoformat(), "note": message.content})
            wins = sum(1 for t in trade_log if "win" in t.get("note", "").lower())
            total = len(trade_log)
            wr = round(wins / total * 100, 1) if total > 0 else 0
            await message.reply("Logged. Win rate: **" + str(wr) + "%** (" + str(wins) + "/" + str(total) + ")")
        else:
            async with message.channel.typing():
                resp = await query_truenorth(message.content.strip())
                chunks = [resp[i:i+1900] for i in range(0, len(resp), 1900)]
                await message.reply(chunks[0])
                for c in chunks[1:]:
                    await message.channel.send(c)
    await bot.process_commands(message)


@bot.command(name="brief")
async def manual_brief(ctx, session: str = "all"):
    sessions = ["asia", "london", "us"] if session == "all" else [session.lower()]
    for s in sessions:
        if s in SESSIONS:
            await send_session_brief(s, "pre")


@bot.command(name="regime")
async def manual_regime(ctx):
    await send_regime_update("manual")


@bot.command(name="trades")
async def manual_trades(ctx):
    ch = bot.get_channel(CH["trades"])
    if ch:
        await ch.send("U0001f50d **MANUAL TRADE SCAN**\n*Querying TrueNorth...*")
        resp = await query_truenorth(trades_prompt())
        for chunk in [resp[i:i+1900] for i in range(0, len(resp), 1900)]:
            await ch.send(chunk)


@bot.command(name="winrate")
async def winrate(ctx):
    total = len(trade_log)
    if total == 0:
        await ctx.send("No trades logged yet. Log with: `result: $BTC LONG WIN`")
        return
    wins = sum(1 for t in trade_log if "win" in t.get("note", "").lower())
    await ctx.send("U0001f4ca Win Rate: **" + str(round(wins / total * 100, 1)) + "%** | " + str(wins) + "/" + str(total))


@bot.command(name="refreshtoken")
async def manual_refresh_token(ctx):
    """Manually trigger a TrueNorth token refresh."""
    await ctx.send("Refreshing TrueNorth token...")
    success = await refresh_tn_token()
    if success:
        await ctx.send("TrueNorth token refreshed successfully.")
    else:
        await ctx.send("Token refresh failed -- check logs for details.")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
