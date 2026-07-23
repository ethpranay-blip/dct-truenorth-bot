# DCT TrueNorth Bot

A Discord bot that pipes crypto trading intelligence from **TrueNorth AI** into a Discord server — scheduled pre-open session briefs, a macro regime outlook, on-demand trade setups with a public win/loss track record, and regime-shift alerts. **Fully deterministic: every number is composed rule-based from TrueNorth's live data. No LLM, no API keys, zero marginal cost.**

> **v3 (July 2026).** The v1 bot rode TrueNorth's authed conversational agent (killed by their v2 upgrade). v2 paired TrueNorth's keyless **agent-tools REST API** (the backend of [`@truenorth-ai/cli`](https://www.npmjs.com/package/@truenorth-ai/cli)) with Claude for prose (killed by API credit burn). v3 keeps the data layer and replaces the LLM with rule engines — there is nothing left that can expire, bill, or silently break.

---

## What it does

- **Pre-open session briefs**, 15 minutes before each market opens, posted to the matching channel:
  - **Asia** → 08:45 JST (Tokyo opens 09:00)
  - **London** → 07:45 UK (LSE opens 08:00)
  - **US** → 09:15 ET (NYSE opens 09:30)

  Cron jobs run in each market's local timezone, so UK/US daylight-saving shifts are handled automatically. Each brief: regime read, BTC/ETH price + trend (MA position, MACD, RSI), ranges + ATR + volume, nearest S/R channels with strength, funding/OI percentiles + liquidation magnets, top movers with relative strength, junk-filtered headlines.
- **Macro regime outlook** Mon/Wed/Fri at 06:00 IST — adds a cross-asset strip (SP500/NASDAQ/VIX/DXY/US10Y) and 7-day rotation.
- **Regime-shift alerts** — rule-derived RISK-ON/OFF/NEUTRAL is checked on every brief run; a flip posts an orange alert to `#regime-outlook` between scheduled posts. Baseline persists in `{CACHE_PATH}/last_regime.json`.
- **`!setup <ticker>`** — mechanical trade setup: weighted signal score from 4h TA (SMA20/50, MACD, RSI, Bollinger); |score| ≥ 2.5 sets direction, ≥ 4 is High conviction; entry ±0.25 ATR, stop 1.5 ATR, TP1/TP2 at 2.5R/4R (house minimum-1:2.5 standard). Mixed tape → an honest "No clear setup". Repeat calls within 6 h reuse the open setup instead of double-logging. **Cross-asset:** crypto tickers use TrueNorth's 4h TA; **stocks (`AAPL`, `NVDA`…) and commodities (`GOLD`, `OIL`…)** run the same scoring on the **daily**, with indicators computed locally from `historical_bars` OHLCV (TrueNorth has no non-crypto indicator tool) and a 7-day tracking window.
- **Outcome tracking + `!winrate`** — every LONG/SHORT setup is logged and resolved **against 15-minute candle wicks** (not spot checks): first candle through stop → LOSS, through TP1 → WIN, both-in-one-candle → conservative LOSS, 48 h no-trigger → EXPIRED. Resolutions reply to the original setup message; `!winrate` posts the public record (W/L/E, win rate, best/worst, realized R:R).
- **`!scan [1-25]`** — ANSI-colored top-N by 7-day relative strength vs BTC.
- **`!watch <ticker>` / `!unwatch` / `!watchlist`** — curate the tokens the 4h auto-signal scan checks (admins only for edits). Added symbols (crypto/stocks/commodities) are evaluated against the same gate — RVWAP structural bias + score ≥ 4.0 — and fire to `CH_SIGNALS` when they qualify. Persisted to `{CACHE_PATH}/watchlist.json`; capped at 25.
- **Auto-signals** (optional, `CH_SIGNALS`) — every 4h candle close: RS rotation leaders (longs) / laggards (shorts) aligned with the regime, gated by per-asset rolling-VWAP structural bias (365d + 90d same side) **and** a high-conviction signal score (≥ 4.0 of ±5.5), posted with 2.5R/4R targets and tracked in the same record tagged `auto` (`!winrate` shows a per-engine breakdown once any exist). Most scans post nothing — the triple gate is meant to fire a few times a week.
- **Typefully auto-drafts** (optional) — a template tweet (hot mover > funding extreme > regime read) lands as a **draft** in Typefully after each brief for human review. Never auto-published.
- **Dashboard link** — every brief links to the live dashboard (`DASHBOARD_URL`).
- **Public feeds** — `GET /api/track-record` (win/loss record) and `GET /api/live` (open signals + a cross-asset lead-lag map + the latest brief) power the companion dashboard.
- **Ops** — `!health` diagnostics, `GET /healthz` liveness.

## How a brief is built

```
TrueNorth agent-tools API (keyless REST)
┌──────────────────────────────────────┐
│ basic_market_info (BTC + ETH)        │
│ technical_analysis (BTC + ETH, 1d)   │   raw     ┌──────────────────────┐
│ derivatives_analysis (BTC)           │──dicts───▶│ rule engines         │──▶ Discord embed
│ performance_scanner (top 5, 1d)      │ (parallel)│ (build_rule_brief,   │    + Typefully draft
│ events (24h, junk-filtered)          │           │  detect_regime, …)   │
└──────────────────────────────────────┘           └──────────────────────┘
  regime outlook adds: market_index_price (all), 7d events, 7d scanner
```

Individual tool failures degrade per-section — a brief missing derivatives still posts everything else; only a total data blackout produces a failure embed.

## Setup

### 1. Discord

Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications), enable the **Message Content** intent, invite it with Send Messages + Embed Links, and copy the channel IDs (Developer Mode → right-click → Copy Channel ID).

### 2. Deploy (Railway)

Connect the repo; Railway auto-detects `requirements.txt` + `runtime.txt` + `Procfile`. The bot serves `GET /healthz` on `$PORT`. For the track record and regime baseline to survive redeploys, mount a **Volume** (e.g. at `/data`) and set `CACHE_PATH=/data`.

### Environment variables

| Var | Required | Purpose |
|---|---|---|
| `DISCORD_BOT_TOKEN` | ✅ | Discord gateway token |
| `CH_ASIA_SESSION` / `CH_LONDON_SESSION` / `CH_US_SESSION` | ✅ | Session brief channels |
| `CH_REGIME_OUTLOOK` | ✅ | Regime outlook + shift-alert channel |
| `CACHE_PATH` | recommended | Mounted volume path for `setups.json` / `last_regime.json` (default `.` = ephemeral) |
| `CH_OPS` | recommended | Channel for failure alerts (unset → Railway logs only) |
| `SIGNALS_WATCHLIST` | optional | Comma-separated stock/commodity symbols the auto-signal scan checks each run (default `NVDA,AAPL,TSLA,GOLD,OIL`) — gated by their own RVWAP bias + score ≥ 4.0 |
| `STOCKS_SCREENER` | optional | Stocks for the dashboard's stocks-vs-Nasdaq screener (default `NVDA,AAPL,TSLA,AMD,ASML,AVGO,AMZN,BABA,BX`), computed every 15 min from daily bars vs QQQ |
| `CH_SIGNALS` | optional | Auto-signal channel: every 4h (UTC candle close) the scanner's RS leaders/laggards are gated by regime + per-asset RVWAP bias (365d/90d) + high-conviction score (≥ 4.0); qualifying setups post here with 2.5R/4R targets and join the track record tagged `auto`. Unset → the job never runs |
| `DASHBOARD_URL` | optional | Public dashboard link appended to every brief |
| `SETUP_ALLOWED_CHANNELS` | optional | Comma-separated channel IDs where `!setup`/`!scan`/`!winrate` work (empty = everywhere) |
| `AUTO_DRAFT_ENABLED` | optional | `true` → template tweet drafts to Typefully after briefs |
| `TYPEFULLY_API_KEY` | optional | Typefully v2 API key (drafts only, never publishes) |
| `TYPEFULLY_SOCIAL_SET_ID` | optional | Typefully social set (default `208154` = @Corgil_) |
| `TN_AUTH_TOKEN` | optional | Raw `Authorization` header for TrueNorth app-gated tools |
| `TN_TOOLS_BASE` | optional | Override the agent-tools base URL |
| `TN_DEBUG` | optional | `1` logs per-tool result sizes |
| `PORT` | auto | Railway-managed; aiohttp binds `0.0.0.0:$PORT` |

**No AI or TrueNorth credentials are required** — the data tools are keyless and all composition is rule-based.

## Operating it

- **Normal day:** nothing. Briefs, regime posts, shift alerts, and setup resolutions post themselves.
- **Worried:** `!health` shows uptime, engine, TN tool state, last posts, regime, tracked setups, auto-draft count, and next scheduled runs.
- **Something failed:** scheduled failures post a ⚠️ embed to the target channel and an alert to `CH_OPS` (if set); Railway logs carry `[TN]`, `[SCHED]`, `[REGIME]`, `[TRACK]`, `[AUTODRAFT]`, and `[ALERT]` tagged lines.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest            # no network needed
python -m py_compile bot.py
```

Pushes to `main` auto-deploy to Railway in ~90 s. Don't push without the suite green.

## Companion dashboard

[`tn-dashboard`](https://github.com/ethpranay-blip/tn-dashboard) — public Next.js dashboard (Vercel) on the same keyless data: regime headline, stat cards, top setups, tradfi deep-dive, sector heatmap, 30-asset screener with sparklines. Live at [tn-dashboard-five.vercel.app](https://tn-dashboard-five.vercel.app).

## Credits

Built by **Pranay** ([@corgil_](https://x.com/corgil_)) for the Corgi Calls Discord. TrueNorth referral code: **CORGIL**.

MIT — see [LICENSE](LICENSE).
