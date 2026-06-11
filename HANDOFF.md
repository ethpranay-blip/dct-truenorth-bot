# dct-truenorth-bot — Context Handoff

_Last updated: 2026-06-11. Use this to bootstrap a new Claude chat (or hand to a human collaborator) without re-explaining anything._

---

## TL;DR — paste this into a new chat first

> I'm working on `dct-truenorth-bot`, a Python Discord bot that pipes TrueNorth AI (`app.true-north.xyz`, hosted at `api.adventai.io`) trading intelligence into my Discord community **Corgi Calls**. It's deployed on Railway, auto-deploys from `main`. The repo lives at `/Users/pranaysharma/dct-truenorth-bot` and on GitHub at `ethpranay-blip/dct-truenorth-bot`.
>
> **The bot is currently LIVE and HEALTHY on Railway. Don't touch the deployment without asking.**
>
> Read `HANDOFF.md` in the repo root for the full state, architecture, decisions, pending work, and runbook. Read `README.md` and `ARCHITECTURE.md` for deeper architecture. Read `CHANGELOG.md` for the build history. Read `README_SETUP.md` for credential setup. Read `harvester_local/README.md` for the Mac-local harvester.

---

## 1. Project elevator pitch

**What:** A Discord bot bridging **TrueNorth AI** (a multi-agent crypto trading platform — covers TA, derivatives flow, on-chain, Polymarket, smart-money, options, liquidation risk, meme discovery, unlocks, macro events) to a Discord trading community.

**Who:** Built and operated by Pranay (@corgil_) for the **Corgi Calls** Discord. Code is MIT, designed to be fork-friendly for other KOLs.

**Business context:** Part of an affiliate/KOL relationship with TrueNorth. Referral code is `CORGIL`. Currently on a **150 TrueNorth credits/week** budget — every scheduled job is sized around that constraint.

**What it does daily:**

- Posts **pre-open briefs** for Asia / London / US trading sessions on a cron schedule. Each brief = market analysis (BTC price + key levels + top movers + regime) + 3 high-conviction trade setups + optional risk flag.
- Parses the 3 setups out of each brief and posts them as standalone embeds to a dedicated `#trades` channel.
- Posts a **macro regime outlook** Mon/Wed/Fri covering CB activity, geopolitics, crypto catalysts, BTC/ETH levels.
- Free-form Q&A in `#claude-integration` via TrueNorth (owner-only right now, see §10).
- Manual operator commands for ad-hoc briefs / setups / regime / health / credential rotation.

---

## 2. Current state snapshot

| Item | State |
|---|---|
| Latest commit on `main` | `abb6388` — _"Move regime_mwf from 05:00 → 06:00 IST (after weekly credit reset)"_ |
| Tests | **103 passing** (`pytest tests/`) |
| Working tree | Clean |
| Railway deployment | **LIVE**, auto-deploys from `main` |
| Mac-local harvester | Working end-to-end, last confirmed 2026-04-27 |
| Open issues | One known TN-side intermittent (ServiceUnavailableError on schedule) — see §11 |

**Pending work:** Nothing in flight. There is one diagnostic recommendation I made but didn't ship — see §11.

---

## 3. Repository layout

```
/Users/pranaysharma/dct-truenorth-bot/
├── bot.py                  # All runtime logic (~2300 lines, single file by choice)
├── harvester_local/
│   ├── harvester.py        # Mac-side pychrome script (chmod +x)
│   ├── setup.sh            # One-time installer + config wizard (chmod +x)
│   └── README.md           # Mac-harvester operator guide
├── tests/
│   ├── conftest.py         # Stubs env vars so `import bot` works in CI
│   └── test_parsers.py     # All 103 tests in one file
├── README.md               # Operator-facing, full setup walkthrough
├── README_SETUP.md         # Credential rotation playbook
├── ARCHITECTURE.md         # Deep technical doc — request flow, state machines
├── CHANGELOG.md            # Build history grouped by week with commit hashes
├── HANDOFF.md              # THIS FILE
├── LICENSE                 # MIT
├── Procfile                # web: python bot.py
├── requirements.txt        # discord.py 2.3.2, aiohttp, anthropic, httpx, apscheduler, pytest
├── runtime.txt             # python-3.11.8
├── token_cache.json        # Runtime state — gitignored, atomic writes
├── thread_cache.json       # Runtime state — gitignored, atomic writes
└── .gitignore              # excludes .env, caches, .pytest_cache, .venv
```

**No `nixpacks.toml`, no `start.sh`, no `harvester.py` in root** — those were removed when we pivoted away from Playwright on Railway. Don't reintroduce them.

---

## 4. Architecture (mental model)

```
Discord user ──▶ Discord gateway (discord.py 2.3.2)
                    │
                    ▼
                bot.py running on Railway as a web service
                    │
                    ├── APScheduler (IST/Asia-Kolkata timezone)
                    │     ├─ Asia pre-open    05:45 IST daily
                    │     ├─ London pre-open  13:15 IST daily
                    │     ├─ US pre-open      18:15 IST daily
                    │     ├─ Regime outlook   Mon/Wed/Fri 06:00 IST
                    │     └─ Token refresh    every 8h
                    │
                    ├── Command handlers (prefix !):
                    │     !brief !trades !regime !health !setcreds !refreshtoken !winrate
                    │
                    ├── on_message router:
                    │     #claude-integration → handle_direct_tn / handle_sonnet (owner-only)
                    │
                    ├── aiohttp web server on $PORT:
                    │     POST /credentials  ◀── Mac-local harvester (cron, pychrome)
                    │     GET  /healthz      ◀── liveness probe (public)
                    │
                    └── HTTP clients:
                          ├─ TrueNorth SSE   → api.adventai.io/discovery-agents/sse/v2/streams
                          ├─ Privy refresh   → auth.privy.io/api/v1/sessions (5 body+header variants)
                          └─ Anthropic       → claude-sonnet-4-6 + claude-haiku-4-5

Disk state (atomic writes via .tmp + fsync + os.replace):
  - token_cache.json    {access, refresh, updated_at}
  - thread_cache.json   {thread_id, updated_at}
```

Every TN call goes through `_tn_call_once(prompt, thread_id, timeout_read)` at `bot.py:547`. **A new `httpx.AsyncClient` is constructed per call** — there is no shared client / pool / TTL anywhere in the codebase. This means scheduler and Discord handlers never share HTTP state. Same for Privy refresh + thread-create probe.

---

## 5. Current schedule (post credit-budget trim)

| Job | Cron (IST) | TN credits/week |
|---|---|---|
| Asia pre-open brief | `5:45 * * *` | 7 |
| London pre-open brief | `13:15 * * *` | 7 |
| US pre-open brief | `18:15 * * *` | 7 |
| Regime outlook | `6:00 mon,wed,fri` | 3 |
| Privy token refresh | `*/8 * * *` (every 8h) | 0 — no TN call |
| **Total** | | **24/wk** |

**Budget:** 150 TN credits/week. 24 scheduled, ~126 left over for manual `!brief` / `!trades` / `!regime` and any owner chat. Manual brief shifts (Pre-Close, Post-Open, Post-Close) were **removed** to fit budget — only Pre-Open survives.

**Why both Asia and Regime were nudged off the hour:** TrueNorth credits reset at **05:30 IST every Monday**. Calls firing before 05:30 on Monday drew against the previous week's almost-exhausted bucket and 503'd. Moved Asia to 05:45 and Regime to 06:00 to land after reset.

---

## 6. All env vars (Railway)

| Var | Required? | Purpose |
|---|---|---|
| `DISCORD_BOT_TOKEN` | ✅ | Discord bot gateway token |
| `ANTHROPIC_API_KEY` | ✅ | Claude Sonnet middleman + Haiku fallback |
| `CH_CLAUDE_INTEGRATION` | ✅ | Channel ID for `#claude-integration` |
| `CH_ASIA_SESSION` | ✅ | Channel ID for `#asia-session` |
| `CH_LONDON_SESSION` | ✅ | Channel ID for `#london-session` |
| `CH_US_SESSION` | ✅ | Channel ID for `#us-session` |
| `CH_REGIME_OUTLOOK` | ✅ | Channel ID for `#regime-outlook` |
| `CH_TRADES` | ✅ | Channel ID for `#trades` |
| `TN_TOKEN` | ⚠️ fallback | Initial Privy access JWT — cache and webhook override this |
| `TN_REFRESH_TOKEN` | ⚠️ fallback | Initial Privy refresh token (single-use, rotates on every refresh) |
| `TN_THREAD_ID` | ⚠️ fallback | Initial TrueNorth thread UUID |
| `PRANAY_DISCORD_ID` | ✅ | Discord user ID allowed to run `!setcreds` AND free-form chat in `#claude-integration` |
| `HARVESTER_SECRET` | ✅ | Shared secret for `POST /credentials`. Mac harvester sends as `X-Harvester-Secret`. |
| `PORT` | auto | Railway-managed, aiohttp binds `0.0.0.0:$PORT` |
| `PRIVY_APP_ID` | optional | Defaults to `cm6afcumv0688a6x3r78jkx7v` (TrueNorth's Privy app) |
| `TN_APP_ORIGIN` | optional | Defaults to `https://app.true-north.xyz`. Privy refresh requires this as `Origin` header. |
| `TN_DEBUG` | optional | `1` enables per-SSE-chunk logging |

**Reminder: never commit env values to the repo. Cache files are gitignored.**

---

## 7. Credential model (the most complex part of the codebase)

TrueNorth doesn't have a public API key. We piggyback on Privy auth that the web app uses. Three credentials are required:

1. **Access token** — Privy JWT, lifetime ~24h, ~410 chars (TN's tokens are leaner than typical Privy).
2. **Refresh token** — Privy opaque string, lifetime 30 days, **single-use** — every refresh rotates it.
3. **Thread ID** — UUID identifying your TrueNorth chat thread.

### Boot precedence

```
token_cache.json (if access JWT exp > now - 1h grace)  ──▶ use cache
   ↓ stale or missing
TN_TOKEN / TN_REFRESH_TOKEN env vars                   ──▶ use env

thread_cache.json (if valid UUID)                      ──▶ use cache
   ↓ missing
TN_THREAD_ID env var (if valid UUID)                   ──▶ use env
   ↓ missing
_try_api_create_thread() probes 7 endpoint candidates  ──▶ use API
   ↓ all fail
None → invalidate, post boot warning, fail-fast all TN commands
```

### Refresh flow

`refresh_tn_token()` at `bot.py:~1126` probes 5 body+header variants in order, caches the first one that returns 200:

1. `snake_body` — `{"refresh_token": rt}` + standard headers
2. `snake_body+bearer_refresh` — same body + `Authorization: Bearer rt`
3. `camel_body` — `{"refreshToken": rt}` + standard headers
4. `bearer_refresh_only` — empty body + `Authorization: Bearer rt`
5. `snake_body+bearer_access` — body + `Authorization: Bearer <current access>`

Required headers (all variants): `privy-app-id`, `Content-Type`, `Accept`, `privy-client`, **`Origin: https://app.true-north.xyz`**, **`Referer: https://app.true-north.xyz/`**. Privy returns `403 missing_origin` without Origin+Referer.

On success, the new access AND the rotated refresh are atomically written to `token_cache.json`. **This is critical** — losing the rotated refresh = locked out until manual `!setcreds`.

### Three credential rotation paths

1. **Auto-refresh** (every 8h cron + on every `auth` failure tag). Works as long as the refresh token is valid.
2. **Mac-local harvester** (cron every 8h on your Mac). Reads from Chrome via CDP, POSTs to `/credentials`. Primary path now — bot stays authed with zero manual intervention.
3. **`!setcreds` Discord command** (owner-only manual). For when both auto paths fail. Auto-deletes your message after parsing.

---

## 8. Mac-local harvester (the operational dependency)

Lives in `harvester_local/`. **You run this on your laptop, not on Railway.**

**How it works:**

1. Reads `~/.dct-harvester-config` (INI: `bot_url`, `harvester_secret`).
2. Hits `http://localhost:9222/json` to list open Chrome tabs.
3. Finds the tab whose URL contains `app.true-north.xyz`.
4. **URL-first thread_id extraction** — if URL contains a UUID (e.g. `.../chat/<uuid>`), uses it immediately. **Skips CDP entirely on this path** — no pychrome _recv_loop crashes possible.
5. CDP fallback only if URL has no UUID: uses pychrome to read `privy:token` / `privy:refresh_token` from localStorage via `Runtime.evaluate`, then intercepts the next `/sse/v2/streams` POST for thread_id (30s timeout).
6. Cache fallback: `~/.dct-harvester-thread` (last-seen UUID).
7. POSTs `{access_token, refresh_token, thread_id}` to `<bot_url>/credentials` with `X-Harvester-Secret`.

**Prerequisites:**

- Chrome launched with `--remote-debugging-port=9222 --user-data-dir="$HOME/Library/Application Support/Google/Chrome"` (reuses your logged-in profile).
- `app.true-north.xyz` open in that Chrome window, ideally on a chat URL (`.../chat/<uuid>`) for the fast path.
- `pychrome` installed (`pip3 install pychrome`).
- Config exists at `~/.dct-harvester-config`.

**Cron line:**

```
0 */8 * * * /usr/bin/python3 /Users/pranaysharma/dct-truenorth-bot/harvester_local/harvester.py >> $HOME/.dct-harvester.log 2>&1
```

**Confirmed working live on 2026-04-27.** TN's access tokens are ~410 chars (originally we assumed ~1500, the short-JWT warning threshold was lowered to `<200` to stop crying wolf).

**Defensive measures already in place** (don't undo these):

- pychrome `_recv_loop` JSONDecodeError silenced via `threading.excepthook` filter (`harvester_local/harvester.py:_silence_pychrome_recv_noise`). Narrowly scoped — only swallows JSONDecodeError from pychrome frames. Other thread errors still surface.
- CDP fallback path wrapped in try/except at every step. If pychrome explodes the harvester returns None and the URL/cache fallbacks still try.

---

## 9. Commands

| Command | Who can run it | What it does |
|---|---|---|
| `!health` | Anyone | Diagnostic embed: uptime, thread_id + source, token exp + source, last TN success/error, webhook status, last creds push. **Does NOT call TN.** |
| `!brief asia` / `london` / `us` / `all` | Anyone | Manually runs `run_session_brief(session, "Manual Brief")`. Posts brief to session channel + trade embeds to `#trades`. |
| `!trades` | Anyone | Fresh standalone "give me 3 setups" call. |
| `!regime` | Anyone | Daily macro outlook, posted to `#regime-outlook`. |
| `!refreshtoken` | Anyone | Runs every Privy refresh variant, posts a per-variant diagnostic embed. Also pings TN to validate. |
| `!setcreds <json or 3 values>` | **Owner only** (`PRANAY_DISCORD_ID`) | Rotates access + refresh + thread_id at runtime. Auto-deletes your message for security. |
| `!winrate` | Anyone | Placeholder — no backtest yet. |
| Free-form chat in `#claude-integration` | **Owner only** since 2026-05-02 | Default = direct TN passthrough; `!`-prefix = Claude Sonnet middleman that calls TN multiple times in parallel. Non-owners get a polite "temporarily owner-only" reply with no TN call. |

---

## 10. Error handling taxonomy (worth knowing)

`_classify_failure()` in `bot.py:~706` returns one of these tags, each maps to a recovery action and user-facing copy:

| Tag | Trigger | Recovery | User copy via `_format_failure_reason` |
|---|---|---|---|
| `ok` | HTTP 200 + non-empty text | return cleaned text | — |
| `auth` | 401/403 or auth marker in SSE error | `refresh_tn_token()`, retry once | "Auth failed (HTTP …). Run !refreshtoken." |
| `http_error` | Other 4xx/5xx | backoff + retry | "TrueNorth API error: HTTP …" |
| `sse_error` | Explicit `event_type=error` (non-InvalidRequest) | backoff + retry | "TrueNorth SSE error: …" |
| `invalid_request` | `error_type=InvalidRequestError` in SSE payload | **fast-fail**, no retry | "Request rejected by TrueNorth: …. Thread may be stale — try `!setcreds`." |
| `empty_200` | HTTP 200 + zero chunks | **fast-fail**, `invalidate_thread()` | THREAD_INVALID_HINT (full DevTools recipe) |
| `incomplete_preamble` | 200 + short "Let me scan…" text + no done marker | retry with longer timeout (360s) | — |
| `timeout` | `ReadTimeout` / `ConnectTimeout` | backoff + retry with longer timeout | "TrueNorth timed out after …" |
| `exception` | Other raised exception | backoff + retry | `<Type>: <msg>` |

**Retry budget:** `TN_MAX_ATTEMPTS = 3` total attempts, backoff `(10s, 30s)`. `empty_200` / `invalid_request` short-circuit immediately.

**Alert rules:** `alert_tn_failure(reason, last)` posts a diagnostic embed to `#claude-integration` only when failure is "real" (`exc is not None` or `status not in (0, 200)` or `sse_error`). Empty-200 fires `alert_thread_invalid()` (one-shot per process lifetime). All alerts rate-limited to 1/hour via `ALERT_COOLDOWN_SEC`.

---

## 11. Known open issues

### 11a. Intermittent `ServiceUnavailableError` on scheduled briefs

**Symptom:** Asia pre-open scheduled brief sometimes returns HTTP 200 + SSE `error_type=ServiceUnavailableError` and zero chunks. The same `!brief asia` invoked manually moments later succeeds.

**Diagnosis** (last conversation, 2026-05-09):

- HTTP envelope is byte-identical across `!brief` and the scheduled job — both flow through `_tn_call_once`, same headers, same body shape `{"query": prompt, "thread_id": <uuid>}`.
- The only payload differences:
  - `!brief asia` → `phase = "Manual Brief"`, prompt starts `"Give me a manual brief brief for the Asia trading session…"`
  - Scheduled → `phase = "Pre-Open Brief"`, prompt starts `"Give me a pre-open brief brief for the Asia trading session…"`
  - (Both have a stale doubled-"brief" word from `phase.lower()` adding to f-string. Same in both, so not the differentiator.)
- httpx clients are per-call (no shared pool, no TTL — confirmed across the module). No bot-side stale state.
- Conclusion: **server-side variance**. Probably correlates with time-of-day load + long thread idle gaps (~7h between Asia and London briefs).

**Recommendation I made but didn't ship** (waiting for your green light):

1. **Log the full SSE error payload for `ServiceUnavailableError`** so we can see `error_message` for hints (rate limit? thread-not-ready? cold start?). Currently `err_snippet` truncated to 500 chars.
2. **Classify `ServiceUnavailableError` separately from generic `sse_error`** and add one automatic retry with 60–120s backoff (different from `incomplete_preamble`'s longer-timeout-same-instance retry). 503s usually clear within a minute.

Both are low-risk, surgical, and don't bloat the credit budget (would only fire on actual 503s).

### 11b. `urllib3` OpenSSL warning from Mac harvester

Cosmetic. Apple's bundled Python uses LibreSSL 2.8.3; urllib3 v2 warns about it. One-time per script run, prints to stderr. Harmless. Suppressing would require silencing all warnings at module load or switching to a Homebrew Python.

### 11c. `!winrate` is a placeholder

No DB, no backtest. Returns a static "coming soon" embed. Would need a sqlite/Postgres for closed trade results + price backfill.

---

## 12. Recent decisions and rationale (so we don't relitigate)

Reverse-chronological. Read this before suggesting changes to any of these.

| Date | Decision | Why |
|---|---|---|
| 2026-05-04 | Regime outlook moved 05:00 → 06:00 IST | Avoid 05:30 IST Monday credit reset window |
| 2026-05-04 | Asia pre-open moved 05:15 → 05:45 IST | Same reason as above |
| 2026-05-02 | Owner-only gate on `#claude-integration` free-form chat | 150/wk TN credit budget |
| 2026-05-02 | Schedule trimmed 12 → 3 briefs/day, regime → MWF | Same reason — fit 150/wk budget |
| 2026-04-27 | pychrome `_recv_loop` JSONDecodeError silenced via threading.excepthook | Daemon thread crash was cosmetic noise; main thread already had the data |
| 2026-04-27 | JWT-length warning threshold lowered 500 → 200 chars | TN's Privy JWTs are ~410 chars empirically; old threshold cried wolf every cron tick |
| 2026-04-27 | URL-first thread_id in harvester, CDP is fallback | pychrome _recv_loop bug; URL parse is faster + safer |
| 2026-04-22 | **PIVOTED off Playwright on Railway** to Mac-local harvester + `/credentials` webhook | Railway's Chromium install was unreliable; Chrome on your Mac is already logged in |
| 2026-04-21 | Webhook auth via `X-Harvester-Secret` header matching `HARVESTER_SECRET` env | Simple shared secret, no need for OAuth dance for owner-only endpoint |
| 2026-04-20 | Atomic writes (`.tmp` + fsync + `os.replace`) for both caches | Mid-write Railway kills were corrupting `thread_cache.json` |
| 2026-04-20 | Cache > env precedence on boot (was env > cache) | `!setcreds` writes to cache; env may be stale |
| 2026-04-19 | No-UUID-fallback for thread_id | Random UUIDs = empty-200 loops, burned ~25 min per failure |
| 2026-04-19 | Single `sanitize_tn_text` chokepoint inside `query_truenorth` | Was scattered across call sites; some forgotten |
| 2026-04-19 | Added `Origin`/`Referer` headers to Privy refresh | Privy returns `403 missing_origin` without them |
| 2026-04-19 | 5-variant Privy refresh probe | No public docs; sniffed real shapes |

---

## 13. Things NOT to do (past mistakes baked in)

- **Don't generate `uuid.uuid4()` as a fallback thread_id.** TrueNorth returns HTTP 200 + zero chunks for unknown UUIDs, which previously triggered ~25 minutes of useless retries. The codebase deliberately returns `None` and fails fast instead.
- **Don't reintroduce Playwright or `nixpacks.toml` to Railway.** That path was tried and abandoned. Mac-local CDP harvester won.
- **Don't add `--with-deps` to any Playwright command.** Build container has no sudo.
- **Don't change cache writes to non-atomic.** Mid-write Railway kills will corrupt the file.
- **Don't drop the `Origin`/`Referer` headers from Privy refresh.** Returns `403 missing_origin`.
- **Don't make `_tn_call_once` synchronous or use a shared client without thinking about Privy refresh + token rotation timing.** Currently per-call clients are fine; if you optimize, preserve the property that each call reads the latest `token_store["access"]`.
- **Don't change the credit-budget schedule without recomputing.** 21 briefs + 3 regimes = 24/wk leaves ~125 for manual usage. Adding any cron job eats into that.
- **Don't strip the `_silence_pychrome_recv_noise` filter** in the Mac harvester. The crash is harmless but logs were unreadable without it.
- **Don't merge `!setcreds` token-write before thread-write.** Thread cache is written first so a failure leaves the bot in a "needs setcreds again" state, not a partial-rotation state.
- **Don't expect `!health` to call TN.** It doesn't. It only reports cached `tn_state`. If you see a TN error in `!health`, it's the last failed call echoing back.

---

## 14. Operational runbook (for the live bot)

### Routine: nothing to do

The bot self-manages. Auto-refresh runs every 8h. The Mac-local harvester (if cron'd) pushes fresh creds every 8h. Atomic caches survive Railway restarts. Just let it run.

### Diagnostic: "is everything OK?"

In any Discord channel where the bot is present, run `!health`. The embed shows:

- Uptime
- Thread ID + source (`cache` / `env` / `api` / `webhook` / `unset`)
- Refresh token: set / NOT SET
- Access token exp (with relative time)
- Token source + cache age
- Last TN success, Last TN error (with relative time + error detail)
- Webhook endpoint state + last creds push age

### When something breaks

Symptom → action:

| Symptom | Action |
|---|---|
| `!health` shows `Thread source: unset` or alert in `#claude-integration` says thread invalid | Re-run Mac harvester: `/usr/bin/python3 /Users/pranaysharma/dct-truenorth-bot/harvester_local/harvester.py` |
| Mac harvester fails with localStorage error | Log back in to TrueNorth in the Chrome window launched with `--remote-debugging-port=9222`, then re-run |
| Mac harvester fails with `webhook HTTP 401: unauthorized` | `HARVESTER_SECRET` rotated on Railway; re-run `harvester_local/setup.sh` to update local config |
| `!refreshtoken` shows every variant returning 400/403 | Refresh token revoked. Run Mac harvester or `!setcreds` manually |
| Bot dark (no green dot) on Discord | Railway dashboard → Deployments → check logs |
| Scheduled brief failed with ServiceUnavailableError | See §11a — known TN-side variance, no immediate action |

### When you want to push code

1. Edit on your Mac under `/Users/pranaysharma/dct-truenorth-bot/`.
2. Run `pytest tests/` — expect 103 passing.
3. Run `python3 -m py_compile bot.py` for syntax check.
4. `git commit -m "…"` and `git push origin main`.
5. Railway auto-deploys in ~90 seconds.
6. Check Railway logs for `DCT TrueNorth Bot online as …`.

### When the Mac IP changes / harvester config drifts

Re-run `bash harvester_local/setup.sh` — prompts for bot URL + secret, writes `~/.dct-harvester-config` (chmod 600).

---

## 15. Pending work (roadmap)

Nothing in flight on the keyboard. Things on the backlog:

### Short-term

- **Ship the `ServiceUnavailableError` retry+logging** (§11a). Two-part change, ~30 lines + 2 tests.
- **Typefully auto-drafting pipeline** — push top trade setups + regime outlook as pre-filled draft posts to Typefully so they're ready to send to social. This is what the user is heading toward as the next big project.
- **Content approval workflow** — 👍/👎 reaction gate in Discord before content leaves the server.

### Medium-term

- **Polymarket copy-trading signals** — own channel, dedicated cron.
- **Migration to TrueNorth's official API key** — TN team has one in flight. Once shipped, retire the Privy refresh dance + Mac harvester entirely.
- **Per-user chat isolation in `#claude-integration`** — needs a real thread-create endpoint (currently all chat shares one TN thread).

### Long-term

- **`!winrate` backtest** — sqlite/Postgres + price backfill.
- **Alerts on TrueNorth flash events** — TN has an internal event bus for breaking on-chain / liquidation cascade signals; expose as live alerts to `#trades`.

---

## 16. Key code locations (so you can jump straight in)

| What | Where |
|---|---|
| Module config + env vars | `bot.py:1-110` |
| JWT helpers + cache loaders | `bot.py:~115-220` |
| `tn_state` dict (observability state) | `bot.py:~248-290` |
| `save_cached_thread` / `load_cached_thread` | `bot.py:~290-380` |
| Thread acquisition (`ensure_thread_id`, `_try_api_create_thread`, `validate_thread`, `invalidate_thread`) | `bot.py:~420-545` |
| `_tn_call_once` (the SSE call) | `bot.py:547` |
| `_classify_failure`, `_format_failure_reason` | `bot.py:~706-770` |
| `query_truenorth` (retry orchestrator) | `bot.py:~739-820` |
| Alerts (`alert_tn_failure`, `alert_thread_invalid`) | `bot.py:~820-920` |
| `_apply_pushed_creds` + webhook handlers (`/credentials`, `/healthz`) | `bot.py:~990-1080` |
| `_privy_refresh_variants`, `refresh_tn_token` | `bot.py:~1080-1200` |
| Sonnet middleman loop | `bot.py:~1340-1420` |
| Sanitizers + dedupers | `bot.py:~1420-1610` |
| Embed builders (`build_trade_embeds`, `build_brief_embed`, `build_risk_embed`, `build_regime_embed`) | `bot.py:~1750-1890` |
| `run_session_brief` (the cron job) | `bot.py:~1920-1985` |
| `run_regime_update` | `bot.py:~1985-2005` |
| `SCHEDULE` list + `setup_scheduler` | `bot.py:~2009-2055` |
| `on_ready` (boot sequence) | `bot.py:~2055-2100` |
| `on_message` (chat routing) | `bot.py:~2117-2160` |
| Manual commands | `bot.py:~2178-2540` (in order: brief, trades, regime, winrate, refreshtoken, setcreds, health) |
| `_is_owner` helper | `bot.py:~2380` |

---

## 17. Test suite

103 tests, all in `tests/test_parsers.py`. Categories covered:

- Parsers (`parse_trades_from_text`, `extract_risk_flag`, `decode_jwt_exp`)
- Classifiers (`_classify_failure` across all 9 tags, `_format_failure_reason`, `_parse_sse_error_payload`)
- Sanitizers (`sanitize_tn_text` for Token/Anchor/sp/orphan tags, `stitch_sse_chunks` overlap dedup, `dedupe_contiguous_lines`, `dedupe_repeated_substrings`, `wrap_markdown_tables`)
- Cache durability (atomic crash simulation, corrupt file deletion, precedence, freshness grace)
- Commands (`!setcreds` validation, Privy refresh variants)
- Webhook (`/credentials` auth, payload validation, happy path, `/healthz`)
- Mac harvester (URL-first extraction, resolve_thread_id precedence, defensive CDP, JSONDecodeError suppression)

**What's NOT covered:**

- Live TrueNorth SSE — would need real creds, impractical in CI.
- Privy refresh actual HTTP — variant list is unit-tested, the call isn't.
- APScheduler trigger firing — verified by calendar, not pytest.

Run: `pytest tests/` (uses `/tmp/tn-venv` if you set it up, otherwise a fresh venv).

---

## 18. Where to actually pick up in the next chat

Three reasonable next steps in priority order:

1. **Ship the ServiceUnavailableError retry+logging** (§11a). I drafted the recommendation, just need approval to implement. Probably 30 min of work + tests.

2. **Start the Typefully draft pipeline.** This is the biggest user-facing next project per recent conversations. Would need a Typefully API key, a draft template per content type (brief, trades, regime), and a posting hook in `run_session_brief` / `run_regime_update`.

3. **Approval workflow for content** — preceding step 2 in some designs (you'd want approval before _anything_ leaves Discord).

Anything else (per-user threads, Polymarket, official TN API migration) is gated on external things — TN's API key timeline, mostly.

---

## 19. Useful one-liners

```bash
# Run Mac harvester one-shot
/usr/bin/python3 /Users/pranaysharma/dct-truenorth-bot/harvester_local/harvester.py

# Dry-run (reads tokens but doesn't POST)
/usr/bin/python3 /Users/pranaysharma/dct-truenorth-bot/harvester_local/harvester.py --verify

# Tail harvester log
tail -f ~/.dct-harvester.log

# Run tests
cd /Users/pranaysharma/dct-truenorth-bot && pytest tests/

# Compile-check bot.py
python3 -m py_compile /Users/pranaysharma/dct-truenorth-bot/bot.py

# Generate harvester secret (any random 32+ chars)
openssl rand -hex 32

# Verify git state
cd /Users/pranaysharma/dct-truenorth-bot && git log --oneline -5 && git status

# Last 5 commits
git log --format='%h %ad %s' --date=short -5
```

---

## 20. Glossary

- **APScheduler** — the cron-job library. Runs in-process on the same asyncio loop as discord.py.
- **CDP** — Chrome DevTools Protocol. What the Mac harvester uses to read localStorage from your open Chrome.
- **Corgi Calls** — the Discord community this bot serves.
- **KOL** — Key Opinion Leader. Crypto-Twitter usage; people running paid signal services.
- **Privy** — auth-as-a-service provider TrueNorth uses for its web app login.
- **`_tn_call_once`** — the single function that hits TrueNorth's SSE endpoint. Every TN call routes through it.
- **TN** — TrueNorth.
- **TrueNorth** — the multi-agent trading platform we're integrating with. `app.true-north.xyz` / `api.adventai.io`.
- **`tn_state`** — module-level dict of observability counters. Survives only the process lifetime.
- **Sonnet middleman** — when chat is prefixed with `!`, Claude Sonnet calls `query_truenorth` multiple times in parallel and synthesizes a single answer.

---

_If you're a new Claude reading this: trust the contents of this file, then verify against the code + git log before making destructive changes. The bot is live on Railway. Don't push without tests passing. Ask before touching anything in §13._
