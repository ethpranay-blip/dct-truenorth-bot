# DCT TrueNorth Bot

A Discord bot that pipes crypto trading intelligence from **TrueNorth AI** into a Discord server — scheduled pre-open session briefs and a macro regime outlook, synthesized by Claude from TrueNorth's live market-data tools.

> **v2 (June 2026).** TrueNorth upgraded to v2 and retired the conversational agent endpoint the original bot used. The bot now runs on TrueNorth's keyless **agent-tools REST API** (the backend of [`@truenorth-ai/cli`](https://www.npmjs.com/package/@truenorth-ai/cli)) with Claude as the synthesis layer. The entire v1 credential stack — Privy token refresh, thread caches, the Mac-local harvester — is gone; no TrueNorth auth is required.

---

## What it does

- **Pre-open session briefs**, 15 minutes before each market opens, posted to the matching channel:
  - **Asia** → 08:45 JST (Tokyo opens 09:00)
  - **London** → 07:45 UK (LSE opens 08:00)
  - **US** → 09:15 ET (NYSE opens 09:30)

  Cron jobs run in each market's local timezone, so UK/US daylight-saving shifts are handled automatically.
- **Macro regime outlook** Mon/Wed/Fri at 06:00 IST — regime read, BTC/ETH levels, cross-asset (SP500/VIX/DXY), rotation, dated catalysts.
- **Dashboard link** — every brief links to the live setups dashboard (`DASHBOARD_URL`) instead of posting trade setups into Discord.
- **Manual commands** — `!brief asia/london/us/all`, `!regime` (both 5-min cooldown), `!health`.

## How a brief is built

```
TrueNorth agent-tools API (keyless REST)          Claude (claude-opus-4-8)
┌──────────────────────────────────┐
│ basic_market_info (BTC)          │
│ technical_analysis (BTC 4h/1d)   │   JSON      ┌────────────────────┐    Discord
│ technical_analysis (ETH 4h)      │──snapshot──▶│ synthesis prompt   │───▶ embed
│ derivatives_analysis (BTC)       │  (parallel) │ (numbers from the  │
│ performance_scanner (top 10)     │             │  snapshot only)    │
│ events (24h crypto news)         │             └────────────────────┘
└──────────────────────────────────┘
  regime adds: market_index_price, 7d macro events, 7d scanner
```

Individual tool failures degrade gracefully — a brief built from 4 of 6 sources still posts; only a total data blackout produces a failure embed.

## Setup

### 1. Discord

Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications), enable the **Message Content** intent, invite it with Send Messages + Embed Links, and copy the channel IDs (Developer Mode → right-click → Copy Channel ID).

### 2. Anthropic

Issue an API key at [console.anthropic.com](https://console.anthropic.com).

### 3. Deploy (Railway)

Connect the repo; Railway auto-detects `requirements.txt` + `runtime.txt` + `Procfile`. The bot serves `GET /healthz` on `$PORT`.

### Environment variables

| Var | Required | Purpose |
|---|---|---|
| `DISCORD_BOT_TOKEN` | ✅ | Discord gateway token |
| `ANTHROPIC_API_KEY` | ✅ | Claude synthesis |
| `CH_ASIA_SESSION` / `CH_LONDON_SESSION` / `CH_US_SESSION` | ✅ | Session brief channels |
| `CH_REGIME_OUTLOOK` | ✅ | Regime outlook channel |
| `DASHBOARD_URL` | optional | Public dashboard link appended to every brief |
| `CH_OPS` | optional | Channel for failure alerts (unset → Railway logs only) |
| `TN_AUTH_TOKEN` | optional | Raw `Authorization` header for TrueNorth app-gated tools |
| `TN_TOOLS_BASE` | optional | Override the agent-tools base URL |
| `SYNTH_MODEL` | optional | Override the Claude model (default `claude-opus-4-8`) |
| `TN_DEBUG` | optional | `1` logs per-tool result sizes |
| `PORT` | auto | Railway-managed; aiohttp binds `0.0.0.0:$PORT` |

No TrueNorth credentials are required — the crypto data tools are keyless.

## Operating it

- **Normal day:** nothing. Briefs and regime post themselves.
- **Worried:** `!health` shows uptime, last TN tool success/error, last synthesis error, last posts, and the next scheduled runs.
- **Something failed:** scheduled failures post a ⚠️ embed to the session channel and an alert to `CH_OPS` (if set); Railway logs carry `[TN]`, `[SCHED]`, and `[ALERT]` tagged lines.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest            # 54 tests, no network
python -m py_compile bot.py
```

Pushes to `main` auto-deploy to Railway in ~90 s. Don't push without the suite green.

## Roadmap

- **Setups dashboard** (in flight) — public Vercel dashboard with top setups, market screener, and sector heatmap, fed by the same TN tool engine.
- **Content approval workflow** — reaction-gate before anything leaves Discord.
- **Typefully auto-drafting** — push briefs/setups as pre-filled social drafts.

## Credits

Built by **Pranay** ([@corgil_](https://x.com/corgil_)) for the Corgi Calls Discord. TrueNorth referral code: **CORGIL**.

MIT — see [LICENSE](LICENSE).
