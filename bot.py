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
TN_TOKEN = os.getenv("TN_TOKEN")
TN_ENDPOINT = "https://api.adventai.io/api/discovery-agents/sse/v2/streams"
TN_THREAD_ID = "78536e88-e440-43dd-a61d-584640f8792b"

CH = {
    "claude":  int(os.getenv("CH_CLAUDE_INTEGRATION")),
    "asia":    int(os.getenv("CH_ASIA_SESSION")),
    "london":  int(os.getenv("CH_LONDON_SESSION")),
    "us":      int(os.getenv("CH_US_SESSION")),
    "regime":  int(os.getenv("CH_REGIME_OUTLOOK")),
    "trades":  int(os.getenv("CH_TRADES")),
}

IST = pytz.timezone("Asia/Kolkata")

SESSIONS = {
    "asia":   {"open": (5,30),  "close": (14,30), "channel": "asia"},
    "london": {"open": (13,30), "close": (22,30), "channel": "london"},
    "us":     {"open": (18,30), "close": (3,30),  "channel": "us"},
}

trade_log = []
chat_history = {}


async def query_truenorth(prompt):
    headers = {
        "Authorization": f"Bearer {TN_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
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
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream("POST", TN_ENDPOINT, headers=headers, json=body) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                            delta = obj.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if isinstance(content, list):
                                for block in content:
                                    if block.get("type") == "text":
                                        result += block.get("text", "")
                            elif isinstance(content, str):
                                result += content
                        except Exception:
                            pass
    except Exception as e:
        result = f"[TrueNorth error: {e}]"
    return result.strip() or "[No response from TrueNorth]"


def query_claude(messages, system=""):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    kwargs = {"model": "claude-haiku-4-5", "max_tokens": 1024, "messages": messages}
    if system:
        kwargs["system"] = system
    resp = client.messages.create(**kwargs)
    return resp.content[0].text


def pre_session_prompt(session):
    return (f"Run a full pre-{session.upper()} session brief. Include: "
            f"1) Market regime check for $BTC, $GOLD, $OIL "
            f"2) Key levels to watch this session "
            f"3) Top macro events or news to watch "
            f"4) 3 high-conviction tokens to trade with direction and bias. Keep it sharp and actionable.")


def post_session_prompt(session):
    return (f"Run a full post-{session.upper()} session debrief. Include: "
            f"1) What moved and why — $BTC, $GOLD, $OIL recap "
            f"2) Key levels that held or broke "
            f"3) Any macro surprises or narrative shifts "
            f"4) What to carry into the next session. Be concise and data-driven.")


def regime_update_prompt():
    return ("Give me a regime and macro update. Include: "
            "1) Current market regime (risk-on / risk-off / neutral) "
            "2) Key geopolitical events, wars, or news impacting markets RIGHT NOW "
            "3) $BTC macro stance "
            "4) DXY, rates, bonds context "
            "5) One-line global outlook. Keep it under 400 words.")


def trades_prompt():
    return ("Give me 3 high-conviction trade setups RIGHT NOW. "
            "For each trade include: Token ($SYMBOL), Direction (LONG/SHORT), "
            "Entry zone, Stop Loss, Take Profit (minimum 1:2 RR), "
            "and a 2-sentence reason backed by TrueNorth data. "
            "Also include $BTC directional bias. Format cleanly.")


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=IST)


async def send_session_brief(session_key, brief_type):
    session = SESSIONS[session_key]
    channel = bot.get_channel(CH[session["channel"]])
    if not channel:
        return
    label = "📋 PRE-SESSION BRIEF" if brief_type == "pre" else "📊 POST-SESSION DEBRIEF"
    emoji = {"asia": "🌏", "london": "🇬🇧", "us": "🇺🇸"}[session_key]
    await channel.send(f"{emoji} **{label} — {session_key.upper()} SESSION**\n*Querying TrueNorth...*")
    prompt = pre_session_prompt(session_key) if brief_type == "pre" else post_session_prompt(session_key)
    tn = await query_truenorth(prompt)
    for chunk in [tn[i:i+1900] for i in range(0, len(tn), 1900)]:
        await channel.send(chunk)
    if brief_type == "pre":
        await asyncio.sleep(2)
        trades_ch = bot.get_channel(CH["trades"])
        if trades_ch:
            await trades_ch.send(f"{emoji} **{session_key.upper()} SESSION SETUPS**\n*Fetching from TrueNorth...*")
            tr = await query_truenorth(trades_prompt())
            for chunk in [tr[i:i+1900] for i in range(0, len(tr), 1900)]:
                await trades_ch.send(chunk)


async def send_regime_update(trigger="scheduled"):
    channel = bot.get_channel(CH["regime"])
    if not channel:
        return
    await channel.send(f"🌐 **REGIME & MACRO UPDATE**\n*Querying TrueNorth...*")
    resp = await query_truenorth(regime_update_prompt())
    for chunk in [resp[i:i+1900] for i in range(0, len(resp), 1900)]:
        await channel.send(chunk)


def schedule_sessions():
    offsets = {
        "asia":   {"pre": (5,15),  "post": (14,45)},
        "london": {"pre": (13,15), "post": (22,45)},
        "us":     {"pre": (18,15), "post": (3,45)},
    }
    for sess, times in offsets.items():
        ph, pm = times["pre"]
        scheduler.add_job(send_session_brief, "cron", hour=ph, minute=pm,
                          args=[sess, "pre"], id=f"pre_{sess}", replace_existing=True)
        posth, postm = times["post"]
        scheduler.add_job(send_session_brief, "cron", hour=posth, minute=postm,
                          args=[sess, "post"], id=f"post_{sess}", replace_existing=True)
    for i, (h, m) in enumerate([(5,15), (13,15), (18,15)]):
        scheduler.add_job(send_regime_update, "cron", hour=h, minute=m,
                          args=["scheduled"], id=f"regime_{i}", replace_existing=True)


@bot.event
async def on_ready():
    print(f"✅ DCT TrueNorth Bot online as {bot.user}")
    schedule_sessions()
    scheduler.start()
    ch = bot.get_channel(CH["claude"])
    if ch:
        await ch.send(
            "🤖 **DCT TrueNorth Bot is online!**\n"
            "Connected to TrueNorth + Claude.\n\n"
            "**Channels:**\n"
            "• `#claude-integration` — Chat with me. Finance = TrueNorth. General = Claude.\n"
            "• Session channels — Auto briefs 15min before/after each session\n"
            "• `#regime-outlook` — Macro updates before every session\n"
            "• `#trades` — 3 setups per session\n\n"
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
                "long", "short", "entry", "tp", "sl", "bias", "outlook"
            ]
            is_finance = any(k in user_msg.lower() for k in finance_kw)
            if is_finance:
                tn_data = await query_truenorth(user_msg)
                system = (
                    "You are a professional trading analyst. TrueNorth provided the market data below. "
                    "Synthesize into a clear actionable response. Use $SYMBOL format."
                )
                reply = query_claude(
                    [{"role": "user", "content": f"User asked: {user_msg}\n\nTrueNorth data:\n{tn_data}"}],
                    system=system
                )
                await message.reply(f"📡 *via TrueNorth + Claude*\n\n{reply}")
            else:
                chat_history[cid].append({"role": "user", "content": user_msg})
                if len(chat_history[cid]) > 20:
                    chat_history[cid] = chat_history[cid][-20:]
                reply = query_claude(
                    chat_history[cid],
                    system="You are a helpful assistant in a crypto trading Discord. Be concise."
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
            await message.reply(f"✅ Logged. Win rate: **{wr}%** ({wins}/{total})")
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
        await ch.send("🔍 **MANUAL TRADE SCAN**\n*Querying TrueNorth...*")
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
    await ctx.send(f"📊 Win Rate: **{round(wins / total * 100, 1)}%** | {wins}/{total}")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
