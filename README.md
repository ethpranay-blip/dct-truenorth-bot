# DCT TrueNorth Bot

A Discord bot that pipes real-time crypto trading intelligence from **TrueNorth AI** into a Discord server — scheduled session briefs, high-conviction trade setups, daily regime outlooks, and on-demand Q&A.

---

## Table of contents

- [What is this?](#what-is-this)
- [Screenshots](#screenshots)
- [Features](#features)
- [Architecture at a glance](#architecture-at-a-glance)
- [Commands](#commands)
- [Setup & deployment](#setup--deployment)
- [Daily usage](#daily-usage)
- [Troubleshooting](#troubleshooting)
- [Architecture details (for developers)](#architecture-details-for-developers)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [Credits](#credits)
- [License](#license)

---

## What is this?

**TrueNorth AI** (hosted at [app.true-north.xyz](https://app.true-north.xyz)) is a multi-agent trading-intelligence platform. It runs dozens of internal tools in parallel to cover technical analysis, derivatives flow, on-chain data, Polymarket prediction-market signals, Hyperliquid smart-money tracking, options, liquidation risk, meme-coin discovery, token unlocks, performance scanning, and macro events across crypto, equities, and prediction markets. Instead of each analyst building their own dashboard of 50 tools, TrueNorth synthesizes everything into natural-language answers.

**This bot (`dct-truenorth-bot`)** is the bridge between TrueNorth and a Discord trading community. It auto-posts four kinds of content on a schedule:

- **Session briefs** for Asia / London / US trading sessions (pre-open, post-open, pre-close, post-close — 4 briefs × 3 sessions = 12 briefs per day)
- **Trade setups** — exactly 3 high-conviction trades per session brief, each with entry/stop/target/R:R
- **Daily regime outlook** — a 24 h macro roundup at 5:00 AM IST covering key events, central bank activity, BTC/ETH levels, and dominant market regime
- **On-demand Q&A** — ask anything in `#claude-integration` and get either a direct TrueNorth stream or a Claude-orchestrated multi-angle synthesis

**Who is this for?** Discord trading communities, KOLs (Key Opinion Leaders) running their own server, affiliates, or any team that wants a "TrueNorth → Discord" pipeline without manually copy-pasting every session. The code is open-source (MIT) — fork it for your own community.

**Business context.** This bot powers the [Corgi Calls](https://x.com/corgil_) Discord community and is part of an affiliate/KOL partnership with TrueNorth. Signups that go through referral code **CORGIL** support the maintainer. This is not a paid product — the code is free, the bot runs on the community's infrastructure.

---

## Screenshots

> _Placeholders — drop actual images into `docs/screenshots/` to render them below._

![Screenshot: #trades channel showing three auto-generated trade cards with entry/stop/target fields](docs/screenshots/trades-channel.png)

![Screenshot: session brief in #us-session with Market Regime summary and wrapped markdown table](docs/screenshots/us-session-brief.png)

![Screenshot: !health command output showing uptime, thread source, token source, JWT exp](docs/screenshots/health-command.png)

![Screenshot: !refreshtoken diagnostic embed with one row per Privy variant](docs/screenshots/refreshtoken-embed.png)

---

## Features

- **Scheduled session briefs** in IST for the Asia, London, and US sessions — 12 briefs per day covering both open and close
- **3 high-conviction trade setups** parsed out of every brief and posted to a dedicated `#trades` channel
- **Daily regime outlook** at 5:00 AM IST into `#regime-outlook` with macro events, central bank activity, and BTC/ETH levels
- **On-demand Q&A** in `#claude-integration`: default behaviour streams TrueNorth directly; prefix any message with `!` to invoke a Claude Sonnet "middleman" that calls TrueNorth multiple times in parallel from different angles
- **Automatic auth rotation** — Privy access token refreshed every 8 h, rotated refresh tokens persisted atomically to disk so Railway restarts don't break auth
- **Honest error diagnostics** — `!health` shows token source, thread source, last success / last error, JWT expiry; failure embeds include HTTP status, attempt count, SSE `error_type` / `error_code` / `error_message`
- **Sanitized output** — TrueNorth's internal markup (`<Token>`, `<Anchor>`, `<sp>`) is stripped before anything hits Discord; markdown tables get auto-wrapped in code fences so they render as aligned monospace
- **Bounded retries** — max 3 attempts per TrueNorth call with 10 s / 30 s back-off; empty-200 responses short-circuit immediately (they mean the thread is invalid, retrying won't help)
- **Owner-only `!setcreds`** — rotate all three auth values (access, refresh, thread_id) from Discord without touching Railway; the message containing the JWTs is auto-deleted for security
- **Mac-local harvester** — optional script in `harvester_local/` reads fresh credentials from an already-logged-in Chrome profile via CDP and POSTs them to the bot's `/credentials` webhook. Cron it every 8 h to keep the bot authed with zero manual effort. See [harvester_local/README.md](harvester_local/README.md)

---

## Architecture at a glance

```
┌──────────────────┐
│   Discord user   │
│  (trader / KOL)  │
└────────┬─────────┘
         │ !trades · !brief · chat
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                 DCT TrueNorth Bot  (bot.py)                     │
│                                                                 │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────────┐│
│  │  Scheduler   │   │  Command     │   │  on_message handler  ││
│  │  APScheduler │   │  handlers    │   │  (direct / sonnet)   ││
│  │  (IST cron)  │   │  (discord.py)│   │                      ││
│  └──────┬───────┘   └──────┬───────┘   └──────────┬───────────┘│
│         │                  │                      │            │
│         └────────┬─────────┴──────────────────────┘            │
│                  ▼                                             │
│         ┌──────────────────┐                                   │
│         │ query_truenorth  │  ── sanitize → dedupe → return    │
│         └────────┬─────────┘                                   │
│                  │                                             │
│  ┌───────────────┴────────────────┐                            │
│  │                                │                            │
│  ▼                                ▼                            │
│ ┌───────────────┐          ┌──────────────┐                    │
│ │ refresh_tn_   │          │ ensure_      │                    │
│ │ token (Privy) │          │ thread_id    │                    │
│ └──────┬────────┘          └──────┬───────┘                    │
└────────┼──────────────────────────┼────────────────────────────┘
         │ HTTPS                    │ file I/O (atomic)
         ▼                          ▼
┌─────────────────┐         ┌────────────────────┐
│  Privy Auth     │         │  Disk persistence  │
│ auth.privy.io   │         │  token_cache.json  │
└─────────────────┘         │  thread_cache.json │
                            └────────────────────┘
         │
         │  Bearer <access_token>  +  SSE stream
         ▼
┌────────────────────────────────────────────┐
│   TrueNorth API (api.adventai.io)          │
│   POST /api/discovery-agents/sse/v2/streams│
└────────────────────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────────┐
│   Anthropic API (for Sonnet middleman)     │
│   claude-sonnet-4-6 · claude-haiku-4-5     │
└────────────────────────────────────────────┘
```

The bot is a single Python process running on Railway. Scheduled jobs use APScheduler. External dependencies: Discord, TrueNorth (via Privy auth), Anthropic. Disk state: two JSON cache files that survive process restarts within a container.

---

## Commands

| Command | Who can run it | What it does |
|---|---|---|
| `!health` | Anyone | Uptime, thread ID + source, token exp + source, last TN success / error |
| `!brief asia` / `london` / `us` / `all` | Anyone | Manually runs a session brief; posts analysis to the session channel + trade embeds to `#trades`, then confirms in the invoking channel |
| `!trades` | Anyone | Fresh "give me 3 setups right now" scan, posted inline |
| `!regime` | Anyone | Daily regime outlook, posted to `#regime-outlook` |
| `!refreshtoken` | Anyone | Runs every Privy refresh variant in sequence, posts a diagnostic embed with one field per variant (status, body, response keys) |
| `!setcreds <json \| 3 values>` | **Owner only** (`PRANAY_DISCORD_ID`) | Rotates access token + refresh token + thread_id at runtime; deletes the caller's message for security; writes both caches atomically; pings TN to verify |

Channel defaults (routed by environment variable):

| Channel env var | Purpose |
|---|---|
| `CH_CLAUDE_INTEGRATION` | Universal chat (default → TN passthrough, `!` prefix → Claude Sonnet middleman) |
| `CH_ASIA_SESSION`, `CH_LONDON_SESSION`, `CH_US_SESSION` | Session-specific briefs |
| `CH_REGIME_OUTLOOK` | Daily macro outlook |
| `CH_TRADES` | All trade setups from every session brief |

---

## Setup & deployment

### 1. Clone the repo

```bash
git clone https://github.com/ethpranay-blip/dct-truenorth-bot.git
cd dct-truenorth-bot
```

### 2. Install dependencies (local dev only)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Create a Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**.
2. Under **Bot** → **Reset Token** — copy the token. This becomes `DISCORD_BOT_TOKEN`.
3. Under **Privileged Gateway Intents**, enable **Message Content Intent**.
4. Under **OAuth2 → URL Generator**, select scopes `bot` + `applications.commands`. In the bot-permissions list below, tick **Send Messages**, **Embed Links**, **Manage Messages** (so the bot can delete the `!setcreds` message containing JWTs), **Read Message History**, and **Create Public Threads**.
5. Open the generated URL, pick your Discord server, approve.
6. In Discord, right-click each target channel (`#claude-integration`, `#asia-session`, `#london-session`, `#us-session`, `#regime-outlook`, `#trades`) → **Copy Channel ID**. Requires Developer Mode (Settings → Advanced → Developer Mode).

### 4. Get an Anthropic API key

Head to [console.anthropic.com](https://console.anthropic.com), create an account, top up a few dollars, and issue an API key. That becomes `ANTHROPIC_API_KEY`.

### 5. Get your TrueNorth credentials

See the full walkthrough in [**README_SETUP.md**](README_SETUP.md). Short version: log into [app.true-north.xyz](https://app.true-north.xyz), open Chrome DevTools, grab three values from Application → Local Storage + Network tab:

- `privy:token` → `TN_TOKEN`
- `privy:refresh_token` → `TN_REFRESH_TOKEN`
- `thread_id` from the `/streams` request payload → `TN_THREAD_ID`

### 6. Find your own Discord user ID

Right-click your own Discord username with Developer Mode on → **Copy User ID**. That becomes `PRANAY_DISCORD_ID` (the env var name is legacy — rename in env if you fork, the bot just reads whatever int is in there).

### 7. Deploy to Railway

1. Create a project on [Railway](https://railway.app/) and connect it to your fork of this repo.
2. Under **Variables**, paste all the values you collected above. See the env-var table in [README_SETUP.md](README_SETUP.md#4-env-var-quick-reference) for the full list.
3. Railway auto-detects `requirements.txt` + `runtime.txt` (Python 3.11.8) + `Procfile` (`worker: python bot.py`). The first deploy takes ~90 s.

Any Python host works (Fly.io, Render, a VPS, Docker). Railway is the project's default because it's cheap, auto-deploys from GitHub pushes, and survives as a worker service without HTTP exposure.

### 8. Expected boot log

```
[BOOT] tokens loaded from cache (updated_at=...)
[BOOT] thread_id loaded from cache: 78536e88-...
[BOOT] TN_REFRESH_TOKEN present; attempting startup refresh...
[TokenRefresh] POST https://auth.privy.io/api/v1/sessions ...
[TokenRefresh] ok via variant=snake_body; access exp=...
[TokenRefresh] persisted rotated tokens to cache
[BOOT] Validating TN thread id=... via ping…
[BOOT] TN thread is valid: 78536e88-...
DCT TrueNorth Bot online as <bot name>
[SCHED] 12 session briefs + regime + token refresh scheduled
```

A green boot lands in ~10 seconds. If you see `⚠️ TN_THREAD_ID is invalid` in `#claude-integration`, see the [troubleshooting section](#troubleshooting).

---

## Daily usage

- **Normal day:** do nothing. Scheduled briefs post themselves. The bot auto-refreshes the Privy token every 8 h and survives Railway restarts via the atomic token + thread caches.
- **Once a month (roughly):** Privy's refresh token hits its 30-day lifetime or the session is revoked. When the bot posts a "thread invalid" or "Privy refresh FAILED" alert in `#claude-integration`, run `!setcreds` with a fresh trio of values (see [README_SETUP.md](README_SETUP.md)).
- **When worried:** run `!health` to see exactly where the bot stands — uptime, token source, thread source, last success / last error times.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `⚠️ TN_THREAD_ID is invalid — TN-dependent commands are disabled` on boot | Thread id rejected by TrueNorth (unknown thread or revoked session) | `!setcreds` with a fresh `thread_id` from the Network tab — see [README_SETUP.md](README_SETUP.md) |
| `🚨 TrueNorth auth failure` embed | Privy access token expired and refresh failed | `!refreshtoken` to see which variant Privy is rejecting. If every variant returns 400/403, the refresh token is revoked — `!setcreds` |
| Bot not responding to any command | Railway deployment is down or crash-looping | Railway dashboard → **Deployments** → latest → **Logs**. Look for Python tracebacks |
| `!trades` posts "TrueNorth returned empty response" | Stream returned HTTP 200 with zero chunks = unknown thread | `!setcreds` with a fresh thread_id |
| `🚨 TrueNorth rejected request` with `InvalidRequestError` | Thread went stale mid-session (happens with thread TTLs) | `!setcreds` |
| `!setcreds` replies but your message stayed visible | Bot lacks **Manage Messages** on the channel | Re-check the bot's role permissions on `#claude-integration` |
| `Session cookies expired` alert | Playwright harvester restored cookies but TN redirected to login | Re-export `TN_SESSION_COOKIES` from Cookie-Editor, or just `!setcreds` |
| Bot boots but `!health` shows `Token source: env (cache missing or stale on boot)` | First boot after deploy, or cache got deleted | Harmless — next refresh (within 8 h) populates the cache |

### Reading Railway logs

1. Railway dashboard → your project → **Deployments** → the green "active" deployment → **Logs** tab.
2. Scroll to the bottom for latest. Search for `[BOOT]`, `[TokenRefresh]`, `[Thread]`, `[TN ERROR]` prefixes — every critical path prints a tagged line.
3. For per-chunk SSE debugging, set env var `TN_DEBUG=1` and redeploy. Turn it off once done — it's noisy.

---

## Architecture details (for developers)

### Why SSE, not REST?

TrueNorth's backend streams agent output token-by-token over **Server-Sent Events** (SSE) at `POST /api/discovery-agents/sse/v2/streams`. A single request can take 2–3 minutes for multi-tool queries, so SSE lets the bot start processing chunks before the full response completes. We aggregate chunks, run an overlap-dedup pass (TN sometimes re-emits preamble), then sanitize.

### Why Privy?

TrueNorth's front-end uses [Privy](https://privy.io) for auth. Access tokens are JWTs signed with ES256 with a ~24 h lifetime. Refresh tokens are opaque strings, live 30 days, and are **single-use** (every successful refresh rotates them — this is the whole reason `token_cache.json` has to be durable). Privy's `/api/v1/sessions` endpoint isn't publicly documented for direct calls, so the bot probes a handful of body+header variants.

### Why Railway?

Cheap (starts at $5/mo), auto-deploys from GitHub pushes, runs worker services without HTTP exposure, gives you a real filesystem for the cache files. Anything with a persistent filesystem works — Fly.io, Render, a VPS, Docker-on-anything.

### Cache file formats

`token_cache.json`:

```json
{
  "access": "eyJhbGci...",
  "refresh": "rt_...",
  "updated_at": "2026-04-20T15:30:00+05:30"
}
```

`thread_cache.json`:

```json
{
  "thread_id": "78536e88-e440-43dd-a61d-584640f8792b",
  "updated_at": "2026-04-20T15:30:00+05:30"
}
```

Both are written atomically via write-to-`<path>.tmp` → `fsync` → `os.replace`. A mid-write crash can't corrupt the canonical file. Malformed files are deleted on next load so the bot falls back cleanly to env vars.

### Scheduled briefs

APScheduler runs 13 cron jobs — 12 session briefs + 1 daily regime — on the `Asia/Kolkata` (IST) timezone. The schedule is hard-coded in `bot.py:SCHEDULE` and tuned to the actual session open/close times. Changing it requires a code edit + redeploy.

Plus a background job every 8 h that calls `refresh_tn_token()` as a safety net. If you set `TN_SESSION_COOKIES`, there's also a 20 h harvester that re-fetches tokens from localStorage via headless Chromium.

### Retry policy

`TN_MAX_ATTEMPTS = 3` total attempts with `TN_RETRY_BACKOFF_S = (10.0, 30.0)` before retries 1 and 2. Different failure tags trigger different recovery paths — see [ARCHITECTURE.md](ARCHITECTURE.md#error-handling-taxonomy).

### Error classification

`_classify_failure()` returns one of these tags, each of which selects a different recovery path and user-facing message:

| Tag | Trigger | Recovery |
|---|---|---|
| `ok` | HTTP 200 + non-empty text | return cleaned text |
| `auth` | HTTP 401/403 or auth-marker in SSE body | `refresh_tn_token()` then retry |
| `http_error` | Any other HTTP 4xx/5xx | back-off + retry |
| `sse_error` | Explicit `event_type=error` frame (non-InvalidRequest) | back-off + retry |
| `invalid_request` | `error_type=InvalidRequestError` in SSE payload | fast-fail, suggest `!setcreds` |
| `empty_200` | HTTP 200 + zero chunks | fast-fail, invalidate thread |
| `incomplete_preamble` | Only a short "Let me scan…" preamble came through | retry with longer timeout |
| `timeout` | `ReadTimeout` / `ConnectTimeout` | back-off + retry with longer timeout |
| `exception` | Other unhandled exception | back-off + retry |

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full state-machine diagram.

---

## Roadmap

Short-term (next few weeks):

- [ ] **Typefully auto-drafting** — send each day's top trade setups and regime outlook as pre-filled draft posts to [Typefully](https://typefully.com) for the community's social channels
- [ ] **Content approval workflow** — before anything leaves Discord, route through a 👍 / 👎 reaction system so the KOL keeps editorial control

Medium-term:

- [ ] **Polymarket copy-trading signals** — TrueNorth already reports Polymarket positions from top traders; wire them into their own channel
- [ ] **Migration to official TrueNorth API key** — TrueNorth team has an API key in flight; once shipped, retire the Privy refresh dance and the Playwright harvester entirely
- [ ] **Per-user chat isolation** — today, all `#claude-integration` chat shares one TrueNorth thread. Once the API key lands we can mint a real per-user thread

Longer-term:

- [ ] **Backtesting loop** — parse every `!trades` setup into a DB, match against price history, publish win-rate over time (`!winrate` is a placeholder today)
- [ ] **Alerts on TrueNorth's own "flash" events** — TN surfaces breaking on-chain / liquidation cascade signals via an internal event bus; expose them as live alerts in `#trades`

---

## Contributing

This is maintained by one person as a community tool, but PRs are welcome.

### Running tests

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
```

92+ unit tests cover parsing, error classification, cache durability, and sanitization. They run in under a second and don't hit the network — mocked env vars let `import bot` succeed without real credentials (see `tests/conftest.py`).

### Coding style

- **No scope creep in fixes.** If the bug is "double dollar signs", don't refactor adjacent code. Three similar lines is better than a premature abstraction.
- **Defensive code at boundaries only.** Trust internal callers.
- **Atomic file writes** for anything that survives a restart — use the `_save_token_cache` / `save_cached_thread` pattern.
- **Diagnostic logging** on every failure path. Every classification tag, every retry attempt, every cache write should print a tagged line. Railway logs are the debugger.

### Opening issues

GitHub Issues on [this repo](https://github.com/ethpranay-blip/dct-truenorth-bot/issues). For credential / auth problems, always include a screenshot of the `!health` output (it's safe — no secrets).

---

## Credits

Built by **Pranay** ([@corgil_](https://x.com/corgil_)) for the Corgi Calls Discord. Iterated with Claude Code over 3 weeks.

Uses:
- [TrueNorth AI](https://app.true-north.xyz) — trading-intelligence backend
- [Anthropic Claude](https://www.anthropic.com/) — Claude Sonnet 4.6 for the middleman loop, Claude Haiku 4.5 for the fallback chat path
- [discord.py](https://discordpy.readthedocs.io/) — Discord gateway + HTTP
- [APScheduler](https://apscheduler.readthedocs.io/) — cron scheduling in IST
- [httpx](https://www.python-httpx.org/) — async HTTP + SSE
- [Playwright](https://playwright.dev/python/) (optional) — cookie harvester

**TrueNorth referral code: `CORGIL`** — sign up with that code if this bot was useful to you.

---

## License

MIT. See [LICENSE](LICENSE).
