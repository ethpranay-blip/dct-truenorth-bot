import os, json, asyncio, httpx, pytz, re
from datetime import datetime
from io import BytesIO
from dotenv import load_dotenv
import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import anthropic
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
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
    "asia": int(os.getenv("CH_ASIA_SESSION")),
    "london": int(os.getenv("CH_LONDON_SESSION")),
    "us": int(os.getenv("CH_US_SESSION")),
    "regime": int(os.getenv("CH_REGIME_OUTLOOK")),
    "trades": int(os.getenv("CH_TRADES")),
}
IST = pytz.timezone("Asia/Kolkata")
SESSIONS = {
    "asia": {"open": (5, 30), "close": (14, 30), "channel": "asia"},
    "london": {"open": (13, 30), "close": (22, 30), "channel": "london"},
    "us": {"open": (18, 30), "close": (3, 30), "channel": "us"},
}
COLORS = {
    "asia":   0xF4A623,
    "london": 0x4A90D9,
    "us":     0xE74C3C,
    "regime": 0x9B59B6,
    "trades": 0x2ECC71,
    "claude": 0x1ABC9C,
}
SESSION_FLAGS = {
    "asia":   ("\\U0001f30f", "ASIA"),
    "london": ("\\U0001f1ec\\U0001f1e7", "LONDON"),
    "us":     ("\\U0001f1fa\\U0001f1f8", "US"),
}
trade_log = []
chat_history = {}
_tn_access_token = os.getenv("TN_TOKEN", "")
# ── HELPERS ──────────────────────────────────
def clean_tn_response(text):
    text = re.sub(r'<Token[^>]*tokenSymbol="([^"]*)"[^>]*/>', r'$\\1', text)
    text = re.sub(r'<Anchor[^>]*/>', '', text)
    text = re.sub(r'<sp[^>]*>[^<]*</sp>', '', text)
    text = re.sub(r'<sp[^>]*/>', '', text)
    text = re.sub(r'<[A-Z][^>]*/>', '', text)
    text = re.sub(r'\\n{3,}', '\\n\\n', text)
    return text.strip()
def bold_numbers(text):
    text = re.sub(r'(\\$[\\d,\\.]+)', r'**\\1**', text)
    text = re.sub(r'([+-]?\\d+\\.?\\d*%)', r'**\\1**', text)
    return text
def truncate(text, limit=1024):
    return text[:limit - 3] + "..." if len(text) > limit else text
def now_ist():
    return datetime.now(IST).strftime("%b %d, %Y · %H:%M IST")
def parse_sections(text):
    """Split TrueNorth text into (title, body) tuples based on numbered headings."""
    text = text.replace("\r\n", "\n").strip()
    sections = []
    current_title = "Overview"
    current_body = []
    for line in text.split("\n"):
        stripped = line.strip()
        # Detect numbered section headers: "1. Something" or "## Something"
        is_header = False
        if stripped and len(stripped) < 80:
            if stripped[0].isdigit() and len(stripped) > 2 and stripped[1] in ".)" and stripped[2] == " ":
                is_header = True
                header_text = stripped[3:].strip().rstrip(":").strip("*")
            elif stripped.startswith("#"):
                header_text = stripped.lstrip("#").strip().rstrip(":").strip("*")
                if header_text and header_text[0].isupper():
                    is_header = True
        if is_header:
            body = "\n".join(current_body).strip()
            if body:
                sections.append((current_title, body))
            current_title = header_text
            current_body = []
        else:
            current_body.append(line)
    body = "\n".join(current_body).strip()
    if body:
        sections.append((current_title, body))
    return sections if sections else [("Overview", text)]
# ── EMBED BUILDERS ────────────────────────────
def build_session_embed(session_key, brief_type, tn_text):
    flag_emoji, label = SESSION_FLAGS[session_key]
    type_label = "PRE-SESSION BRIEF" if brief_type == "pre" else "POST-SESSION DEBRIEF"
    type_icon = "📋" if brief_type == "pre" else "📊"
    embed = discord.Embed(
        title=f"{type_icon}  {flag_emoji} {label}  ·  {type_label}",
        color=COLORS[session_key],
        timestamp=datetime.now(IST),
    )
    embed.set_footer(text="DCT TrueNorth Bot  ·  Powered by TrueNorth AI")
    sections = parse_sections(tn_text)
    for title, body in sections[:8]:
        embed.add_field(name=f"▸ {title}", value=truncate(bold_numbers(body), 1024), inline=False)
    return embed
def build_trades_embed(session_key, tn_text):
    if session_key:
        flag_emoji, label = SESSION_FLAGS[session_key]
        title = flag_emoji + " " + label + "  \u00b7  TRADE SETUPS"
        color = COLORS[session_key]
    else:
        title = "\U0001f3af  TRADE SCAN  \u00b7  LIVE SETUPS"
        color = COLORS["trades"]
    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(IST))
    embed.set_footer(text="DCT TrueNorth Bot  \u00b7  Min 1:2 R:R  \u00b7  Size responsibly")
    EMBED_LIMIT = 5500
    used = len(title) + 60
    sections = parse_sections(tn_text)
    for sec_title, body in sections[:5]:
        field_name = "\u25b8 " + sec_title
        max_val = min(600, EMBED_LIMIT - used - len(field_name) - 10)
        if max_val < 50:
            break
        field_val = truncate(bold_numbers(body), max_val)
        embed.add_field(name=field_name, value=field_val, inline=False)
        used += len(field_name) + len(field_val)
    btc_match = re.search(r"BTC.{0,20}bias.{0,80}", tn_text, re.IGNORECASE)
    if btc_match and used + 60 < EMBED_LIMIT:
        embed.add_field(name="\u20bf  BTC Directional Bias", value=bold_numbers(btc_match.group(0).strip()), inline=False)
    return embed
def build_regime_embed(tn_text):
    embed = discord.Embed(
        title="🌐  REGIME & MACRO UPDATE",
        color=COLORS["regime"],
        timestamp=datetime.now(IST),
    )
    embed.set_footer(text="DCT TrueNorth Bot  ·  Powered by TrueNorth AI")
    regime_match = re.search(
        r'(RISK[- ]ON|RISK[- ]OFF|NEUTRAL|FRAGILE|BEARISH|BULLISH)[^\\n]{0,80}',
        tn_text, re.IGNORECASE
    )
    if regime_match:
        regime_str = regime_match.group(0).strip()
        if any(x in regime_str.upper() for x in ["RISK-ON", "RISK ON", "BULLISH"]):
            badge = "🟢"
        elif any(x in regime_str.upper() for x in ["RISK-OFF", "RISK OFF", "BEARISH"]):
            badge = "🔴"
        else:
            badge = "🟡"
        embed.description = f"{badge}  **{regime_str}**"
    sections = parse_sections(tn_text)
    for title, body in sections[:8]:
        embed.add_field(name=f"▸ {title}", value=truncate(bold_numbers(body), 1024), inline=False)
    return embed
def build_claude_embed(user_msg, claude_reply):
    embed = discord.Embed(
        title="📡  TrueNorth + Claude",
        description=f"*Query: {truncate(user_msg, 200)}*",
        color=COLORS["claude"],
        timestamp=datetime.now(IST),
    )
    embed.set_footer(text="DCT TrueNorth Bot  ·  Finance queries use TrueNorth data")
    reply = bold_numbers(claude_reply)
    chunks = [reply[i:i+1024] for i in range(0, len(reply), 1024)]
    for idx, chunk in enumerate(chunks[:4]):
        embed.add_field(name="Analysis" if idx == 0 else "\\u200b", value=chunk, inline=False)
    return embed
# ── TOKEN REFRESH ─────────────────────────────

# -- TRADE CARD GENERATOR -----------------------------------------------
def parse_trade_data(block_text):
    """Extract coin, direction, entry, SL, TP from a trade block."""
    data = {
        "coin": "???", "direction": "LONG",
        "entry": None, "sl": None, "tp1": None, "tp2": None, "rr": None,
    }
    coin_m = re.search(r'\\$([A-Z]{2,10})', block_text)
    if coin_m:
        data["coin"] = coin_m.group(1)
    dir_m = re.search(r'\\b(LONG|SHORT)\\b', block_text, re.IGNORECASE)
    if dir_m:
        data["direction"] = dir_m.group(1).upper()
    entry_m = re.search(r'[Ee]ntry[:\\s]+\\$?([\\d,\\.]+)', block_text)
    if entry_m:
        data["entry"] = entry_m.group(1)
    sl_m = re.search(r'(?:SL|Stop[- ]?Loss)[:\\s]+\\$?([\\d,\\.]+)', block_text, re.IGNORECASE)
    if sl_m:
        data["sl"] = sl_m.group(1)
    tp1_m = re.search(r'(?:TP1?|Target\\s*1?)[:\\s]+\\$?([\\d,\\.]+)', block_text, re.IGNORECASE)
    if tp1_m:
        data["tp1"] = tp1_m.group(1)
    tp2_m = re.search(r'(?:TP2|Target\\s*2)[:\\s]+\\$?([\\d,\\.]+)', block_text, re.IGNORECASE)
    if tp2_m:
        data["tp2"] = tp2_m.group(1)
    rr_m = re.search(r'(?:R[:\\s/]R|Risk[:/]Reward)[:\\s]+([\\d\\.]+:[\\d\\.]+|[\\d\\.]+x)', block_text, re.IGNORECASE)
    if rr_m:
        data["rr"] = rr_m.group(1)
    return data


def generate_trade_card(trade_data, trade_num=1):
    """Generate a styled trade card PNG image using Pillow."""
    if not PIL_AVAILABLE:
        return None
    W, H = 600, 320
    is_long = trade_data["direction"] == "LONG"
    img = Image.new("RGB", (W, H), (18, 22, 32))
    draw = ImageDraw.Draw(img)
    accent = (46, 204, 113) if is_long else (231, 76, 60)
    draw.rectangle([0, 0, 6, H], fill=accent)
    draw.rectangle([0, 0, W, 60], fill=(26, 30, 44))
    coin = trade_data.get("coin", "???")
    direction = trade_data["direction"]
    try:
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        font_path_reg = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        fnt_big = ImageFont.truetype(font_path, 36)
        fnt_med = ImageFont.truetype(font_path, 18)
        fnt_small = ImageFont.truetype(font_path_reg, 14)
        fnt_tiny = ImageFont.truetype(font_path_reg, 12)
    except Exception:
        fnt_big = fnt_med = fnt_small = fnt_tiny = ImageFont.load_default()
    coin_label = "$" + coin
    draw.text((20, 10), coin_label, font=fnt_big, fill=(255, 255, 255))
    try:
        coin_w = draw.textlength(coin_label, font=fnt_big)
    except Exception:
        coin_w = len(coin_label) * 22
    badge_x = int(20 + coin_w + 14)
    badge_color = (39, 174, 96) if is_long else (192, 57, 43)
    draw.rounded_rectangle([badge_x, 16, badge_x + 80, 46], radius=8, fill=badge_color)
    draw.text((badge_x + 10, 20), direction, font=fnt_med, fill=(255, 255, 255))
    trade_label = "Trade #" + str(trade_num)
    draw.text((W - 110, 20), trade_label, font=fnt_small, fill=(140, 150, 170))
    draw.rectangle([16, 62, W - 16, 64], fill=(40, 50, 70))
    rows = []
    if trade_data.get("entry"):
        rows.append(("Entry", "$" + trade_data["entry"]))
    if trade_data.get("sl"):
        rows.append(("Stop Loss", "$" + trade_data["sl"]))
    if trade_data.get("tp1"):
        rows.append(("TP1", "$" + trade_data["tp1"]))
    if trade_data.get("tp2"):
        rows.append(("TP2", "$" + trade_data["tp2"]))
    if trade_data.get("rr"):
        rows.append(("R:R", trade_data["rr"]))
    col_w = W // 2
    row_h = 46
    start_y = 74
    for idx, (label, value) in enumerate(rows[:4]):
        col = idx % 2
        row = idx // 2
        x = 20 + col * col_w
        y = start_y + row * row_h
        draw.text((x, y), label, font=fnt_small, fill=(120, 130, 150))
        if "TP" in label:
            val_color = (46, 204, 113)
        elif "Stop" in label:
            val_color = (231, 76, 60)
        else:
            val_color = (220, 230, 255)
        draw.text((x, y + 18), value, font=fnt_med, fill=val_color)
    if len(rows) > 4:
        x = 20
        y = start_y + 2 * row_h + 8
        draw.text((x, y), rows[4][0], font=fnt_small, fill=(120, 130, 150))
        draw.text((x, y + 18), rows[4][1], font=fnt_med, fill=(220, 230, 255))
    draw.rectangle([0, H - 36, W, H], fill=(26, 30, 44))
    footer_txt = "DCT TrueNorth Bot  ·  Size responsibly  ·  Min 1:2 R:R"
    draw.text((20, H - 26), footer_txt, font=fnt_tiny, fill=(80, 90, 110))
    arrow = "^" if is_long else "v"
    draw.text((W - 30, H - 26), arrow, font=fnt_med, fill=accent)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
async def refresh_tn_token():
    global _tn_access_token
    if not TN_REFRESH_TOKEN:
        print("[TokenRefresh] TN_REFRESH_TOKEN not set -- skipping refresh")
        return False
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                PRIVY_REFRESH_URL,
                headers={"privy-app-id": PRIVY_APP_ID, "Content-Type": "application/json"},
                json={"refresh_token": TN_REFRESH_TOKEN},
            )
            if resp.status_code == 200:
                data = resp.json()
                new_token = data.get("token") or data.get("access_token") or data.get("identity_token", "")
                if new_token:
                    _tn_access_token = new_token
                    print("[TokenRefresh] Token refreshed at " + datetime.now(IST).isoformat())
                    return True
                print("[TokenRefresh] Refresh response missing token. Keys: " + str(list(data.keys())))
            else:
                print("[TokenRefresh] Refresh failed HTTP " + str(resp.status_code))
    except Exception as e:
        print("[TokenRefresh] Exception: " + str(e))
    return False
# ── TRUENORTH & CLAUDE ────────────────────────
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
    }
    result = ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0)) as client:
            async with client.stream("POST", TN_ENDPOINT, headers=headers, json=body) as resp:
                print(f"[TN HTTP] status={resp.status_code}")
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        data = line[5:].lstrip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                            if obj.get("event_type") == "llm_output":
                                chunk = obj.get("data", {}).get("content", "")
                                if chunk:
                                    result += chunk
                        except Exception:
                            pass
    except Exception as e:
        result = "[TrueNorth error: " + str(e) + "]"
    cleaned = clean_tn_response(result)
    return cleaned or "[No response from TrueNorth]"
def query_claude(messages, system=""):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    kwargs = {"model": "claude-haiku-4-5", "max_tokens": 1024, "messages": messages}
    if system:
        kwargs["system"] = system
    resp = client.messages.create(**kwargs)
    return resp.content[0].text
# ── PROMPTS ───────────────────────────────────
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
# ── BOT SETUP ─────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=IST)
async def send_session_brief(session_key, brief_type):
    session = SESSIONS[session_key]
    channel = bot.get_channel(CH[session["channel"]])
    if not channel:
        return
    flag_emoji, label = SESSION_FLAGS[session_key]
    type_label = "PRE-SESSION BRIEF" if brief_type == "pre" else "POST-SESSION DEBRIEF"
    await channel.send(f"{flag_emoji} **{label} · {type_label}** — *querying TrueNorth...*")
    prompt = pre_session_prompt(session_key) if brief_type == "pre" else post_session_prompt(session_key)
    tn = await query_truenorth(prompt)
    embed = build_session_embed(session_key, brief_type, tn)
    await channel.send(embed=embed)
    if brief_type == "pre":
        await asyncio.sleep(2)
        trades_ch = bot.get_channel(CH["trades"])
        if trades_ch:
            await trades_ch.send(f"{flag_emoji} **{label} SESSION SETUPS** — *fetching from TrueNorth...*")
            tr = await query_truenorth(trades_prompt())
            embed_tr = build_trades_embed(session_key, tr)
            await trades_ch.send(embed=embed_tr)
            if PIL_AVAILABLE:
                raw_blocks = [b for b in re.split(r"\$[A-Z]", tr) if b.strip()]
                for t_idx, block in enumerate(raw_blocks[:6], 1):
                    full_block = "$" + block
                    td = parse_trade_data(full_block)
                    if td.get("entry") or td.get("sl"):
                        buf = generate_trade_card(td, t_idx)
                        if buf:
                            fname = "trade_" + td["coin"] + "_" + str(t_idx) + ".png"
                            await trades_ch.send(file=discord.File(buf, filename=fname))
async def send_regime_update(trigger="scheduled"):
    channel = bot.get_channel(CH["regime"])
    if not channel:
        return
    await channel.send("🌐 **REGIME & MACRO UPDATE** — *querying TrueNorth...*")
    resp = await query_truenorth(regime_update_prompt())
    embed = build_regime_embed(resp)
    await channel.send(embed=embed)
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
    scheduler.add_job(refresh_tn_token, "interval", hours=12,
                      id="tn_token_refresh", replace_existing=True)
# ── EVENTS ────────────────────────────────────
@bot.event
async def on_ready():
    print("DCT TrueNorth Bot online as " + str(bot.user))
    await refresh_tn_token()
    schedule_sessions()
    scheduler.start()
    ch = bot.get_channel(CH["claude"])
    if ch:
        embed = discord.Embed(
            title="🤖  DCT TrueNorth Bot is online!",
            description="Connected to **TrueNorth AI** + **Claude**.",
            color=COLORS["claude"],
            timestamp=datetime.now(IST),
        )
        embed.add_field(name="💬  #claude-integration", value="Chat here. Finance → TrueNorth. General → Claude.", inline=False)
        embed.add_field(name="📋  Session Channels", value="Auto briefs **15 min before/after** each session.", inline=False)
        embed.add_field(name="🌐  #regime-outlook", value="Macro & regime updates before every session.", inline=False)
        embed.add_field(name="🎯  #trades", value="3 high-conviction setups per session.", inline=False)
        embed.add_field(name="⌨️  Manual Commands", value="`!brief asia/london/us` · `!regime` · `!trades` · `!refreshtoken`", inline=False)
        embed.set_footer(text="DCT TrueNorth Bot  ·  Powered by TrueNorth AI")
        await ch.send(embed=embed)
@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.content.startswith(bot.command_prefix):
        await bot.process_commands(message)
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
                    [{"role": "user", "content": "User asked: " + user_msg + "\\n\\nTrueNorth data:\\n" + tn_data}],
                    system=system,
                )
                embed = build_claude_embed(user_msg, reply)
                await message.reply(embed=embed)
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
            if cid == CH["regime"]:
                embed = build_regime_embed(resp)
            else:
                key = {CH["asia"]: "asia", CH["london"]: "london", CH["us"]: "us"}[cid]
                embed = build_session_embed(key, "pre", resp)
            await message.reply(embed=embed)
    elif cid == CH["trades"]:
        if message.content.lower().startswith("result:"):
            trade_log.append({"timestamp": datetime.now(IST).isoformat(), "note": message.content})
            wins = sum(1 for t in trade_log if "win" in t.get("note", "").lower())
            total = len(trade_log)
            wr = round(wins / total * 100, 1) if total > 0 else 0
            await message.reply(f"Logged. Win rate: **{wr}%** ({wins}/{total})")
        else:
            async with message.channel.typing():
                resp = await query_truenorth(message.content.strip())
                embed = build_trades_embed(None, resp)
                await message.reply(embed=embed)
# ── COMMANDS ──────────────────────────────────
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
    if not ch:
        print(f"[ERROR] manual_trades: channel CH['trades']={CH['trades']} not found")
        await ctx.send("❌ Trades channel not found.")
        return
    try:
        await ch.send("\U0001f3af **MANUAL TRADE SCAN** \u2014 *querying TrueNorth...*")
        resp = await query_truenorth(trades_prompt())
        print(f"[trades] Got TrueNorth response, length={len(resp)}")
        embed = build_trades_embed(None, resp)
        await ch.send(embed=embed)
        print("[trades] Embed sent successfully")
        if PIL_AVAILABLE:
            raw_blocks = [b for b in re.split(r"\$[A-Z]", resp) if b.strip()]
            print(f"[trades] Found {len(raw_blocks)} trade blocks for cards")
            for t_idx, block in enumerate(raw_blocks[:6], 1):
                full_block = "$" + block
                td = parse_trade_data(full_block)
                if td.get("entry") or td.get("sl"):
                    buf = generate_trade_card(td, t_idx)
                    if buf:
                        fname = "trade_" + td["coin"] + "_" + str(t_idx) + ".png"
                        await ch.send(file=discord.File(buf, filename=fname))
                        print(f"[trades] Card sent for {td['coin']}")
    except Exception as e:
        print(f"[ERROR] manual_trades: {e!r}")
        import traceback; traceback.print_exc()
        await ctx.send(f"\u274c Error in trades: {e}")
@bot.command(name="winrate")
async def winrate(ctx):
    total = len(trade_log)
    if total == 0:
        await ctx.send("No trades logged yet. Log with: `result: $BTC LONG WIN`")
        return
    wins = sum(1 for t in trade_log if "win" in t.get("note", "").lower())
    embed = discord.Embed(
        title="📈  Win Rate Tracker",
        color=COLORS["trades"],
        timestamp=datetime.now(IST),
    )
    embed.add_field(name="Win Rate", value=f"**{round(wins / total * 100, 1)}%**", inline=True)
    embed.add_field(name="Record",   value=f"**{wins}W / {total - wins}L**", inline=True)
    embed.add_field(name="Total",    value=f"**{total} trades**", inline=True)
    await ctx.send(embed=embed)
@bot.command(name="refreshtoken")
async def manual_refresh_token(ctx):
    await ctx.send("Refreshing TrueNorth token...")
    success = await refresh_tn_token()
    if success:
        await ctx.send("✅ TrueNorth token refreshed successfully.")
    else:
        await ctx.send("❌ Token refresh failed — check logs for details.")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
