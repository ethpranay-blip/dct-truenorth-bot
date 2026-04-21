# Credential Setup

This is the detailed walkthrough for provisioning and rotating TrueNorth credentials. If you're trying to deploy the bot for the first time, start with the [main README](README.md#setup--deployment) — it covers Discord, Anthropic, and Railway setup. Come back here for the TrueNorth / Privy parts.

## Table of contents

- [Why this is needed](#why-this-is-needed)
- [First-time setup](#first-time-setup)
- [When the session expires (the recurring rotation)](#when-the-session-expires-the-recurring-rotation)
- [Grabbing the three values from DevTools](#grabbing-the-three-values-from-devtools)
- [`!setcreds` — the primary rotation path](#setcreds--the-primary-rotation-path)
- [`TN_SESSION_COOKIES` — the automatic backup](#tn_session_cookies--the-automatic-backup)
- [Env var quick reference](#env-var-quick-reference)
- [Commands reference](#commands-reference)
- [What can go wrong during setup](#what-can-go-wrong-during-setup)

---

## Why this is needed

TrueNorth's API doesn't have a public API-key system yet — the only way to authenticate is to piggy-back on the same session credentials their web app uses. That means three values:

1. **Access token** — a Privy JWT (JSON Web Token). Lifetime ~24 h.
2. **Refresh token** — an opaque Privy string. Lifetime 30 days, **single-use** — every successful refresh returns a new one that replaces the old.
3. **Thread ID** — a UUID that identifies your TrueNorth chat thread. Persists as long as TrueNorth keeps the thread alive.

The bot handles the access token's 24 h rotation automatically every 8 h. You only need to intervene when:

- The refresh token hits its 30-day lifetime
- The session is revoked server-side (rare — usually after a browser logout)
- The thread_id becomes stale (rare — usually after TrueNorth clears old threads)

This whole dance goes away once TrueNorth ships their official API key — at that point you'll need just one long-lived token.

---

## First-time setup

You're here because you just deployed the bot and need to populate the Railway env vars for the first time. Do all four of these, in order:

1. **Grab the three values from DevTools** ([walkthrough below](#grabbing-the-three-values-from-devtools)).
2. Paste them into Railway as `TN_TOKEN`, `TN_REFRESH_TOKEN`, `TN_THREAD_ID`.
3. Redeploy so the bot boots with real values.
4. Verify via `!health` in `#claude-integration` — you want `Thread source: env` or `Thread source: cache`, and `Access token exp: <future time>`.

Optional but recommended: also export your browser cookies and set `TN_SESSION_COOKIES` ([section below](#tn_session_cookies--the-automatic-backup)). With cookies in place, the bot can self-heal without you running `!setcreds` manually.

---

## When the session expires (the recurring rotation)

You'll know it's time when:

- `#claude-integration` gets a `⚠️ TN_THREAD_ID is invalid` or `🚨 Privy refresh FAILED` embed, **or**
- `!health` shows `Access token exp: ... (EXPIRED Xm ago)` and `!refreshtoken` returns FAILED for every variant

The fix (under 60 seconds):

1. Open [`app.true-north.xyz`](https://app.true-north.xyz) in Chrome and confirm you're logged in. If not, log in.
2. Grab the three values from DevTools ([walkthrough below](#grabbing-the-three-values-from-devtools)).
3. In Discord, run `!setcreds` (owner only):
   ```
   !setcreds {"access_token":"eyJ…","refresh_token":"rt_…","thread_id":"78536e88-…"}
   ```
4. The bot auto-deletes your message, writes both caches atomically, pings TN to verify, and replies ✅ or ⚠️.

You don't need to redeploy, touch Railway, or restart anything. The caches survive the next restart too.

---

## Grabbing the three values from DevTools

### Step 1 — Turn on Chrome DevTools

Open [`app.true-north.xyz`](https://app.true-north.xyz), log in, then press **F12** (Windows / Linux) or **Cmd+Option+I** (Mac).

### Step 2 — Access token + refresh token (from Local Storage)

1. In DevTools, go to the **Application** tab.
2. In the left sidebar, expand **Storage** → **Local Storage** → click the `https://app.true-north.xyz` origin.
3. You'll see a table of key/value pairs. Find these two rows:
   - `privy:token` → this is your **access_token**.
   - `privy:refresh_token` → this is your **refresh_token**.
4. Click each row, copy the value. **Strip any surrounding double-quotes** before using it — Privy stores values as JSON strings so the raw cell content often looks like `"eyJhbGc..."`. You want just `eyJhbGc...`.

![Screenshot: DevTools Application tab showing Local Storage with privy:token and privy:refresh_token highlighted](docs/screenshots/localstorage-privy-tokens.png)

### Step 3 — Thread ID (from a Network request payload)

1. In DevTools, switch to the **Network** tab.
2. In the filter bar, type `streams` — this narrows the request list to TrueNorth's SSE endpoint.
3. Go back to the TrueNorth UI and send any message (e.g. type `ping` + Enter).
4. A new row appears: `POST https://api.adventai.io/api/discovery-agents/sse/v2/streams`.
5. Click it → **Payload** tab → look for `thread_id` in the JSON body. Copy the UUID.

![Screenshot: DevTools Network tab filtered on 'streams' with the POST request selected and thread_id visible in the Payload tab](docs/screenshots/network-streams-payload.png)

That's all three values. Keep the DevTools window open until you've pasted them somewhere safe.

---

## `!setcreds` — the primary rotation path

**Only the Discord user whose ID matches the `PRANAY_DISCORD_ID` env var can run this command.** Any other caller gets `⛔ restricted to the bot owner` and their message is deleted.

### JSON form (recommended — no whitespace issues)

```
!setcreds {"access_token":"eyJ…","refresh_token":"rt_abc…","thread_id":"78536e88-e440-43dd-a61d-584640f8792b"}
```

### Space-separated form

```
!setcreds eyJ… rt_abc… 78536e88-e440-43dd-a61d-584640f8792b
```

### What happens, step by step

1. **Owner check** — if the caller isn't `PRANAY_DISCORD_ID`, refuse and delete the message.
2. **Parse** the JSON or space-separated form.
3. **Validate** each value:
   - `access_token` must be shape-valid JWT (3 dot-separated segments + decodable base64-JSON payload).
   - `refresh_token` must be at least 20 characters.
   - `thread_id` must match a UUID regex exactly.
4. **Delete your message** immediately so the JWT doesn't sit in chat history.
5. **Write `thread_cache.json` atomically** (`.tmp` → `fsync` → `os.replace`). If this fails (disk full, read-only filesystem), the command bails and tells you — no partial state.
6. **Write `token_cache.json` atomically** (same pattern).
7. **Update in-memory state**: `token_store`, `_tn_thread_current`, clear the `thread_invalid` flag.
8. **Ping TrueNorth** with a 30-second timeout to confirm the thread is recognised.
9. **Reply** with one of:
   - ✅ `Credentials updated. Thread valid. Access token exp: <time>.`
   - ⚠️ `Credentials stored but thread ping returned 0 chunks in 30s. Double-check the thread_id …`

Bad input gets an inline error spelling out what failed — e.g. `access_token rejected: expected 3 dot-separated segments, got 1`.

### Prerequisite: bot must have Manage Messages

Without the **Manage Messages** permission in `#claude-integration`, the bot can't delete your `!setcreds` message and your JWTs stay visible. In the Discord server:

1. Server settings → **Roles** → the bot's role → enable **Manage Messages**.
2. Or right-click the channel → **Edit Channel** → **Permissions** → add the bot's role with Manage Messages allowed.

![Screenshot: Discord channel permissions panel showing the bot's role with Manage Messages enabled](docs/screenshots/discord-manage-messages.png)

---

## `TN_SESSION_COOKIES` — the automatic backup

If you export your browser cookies once and paste them into Railway, the bot can re-harvest tokens on boot and every 20 h without you touching anything. This is optional but saves you a lot of `!setcreds` runs.

### Export cookies

1. Install [Cookie-Editor](https://cookie-editor.cgagnier.ca/) in Chrome.
2. Visit [`app.true-north.xyz`](https://app.true-north.xyz) while logged in.
3. Click the Cookie-Editor extension icon → **Export** → **Export as JSON**.
4. You get a JSON array like:

```json
[
  {
    "domain": ".true-north.xyz",
    "name": "sid",
    "value": "…",
    "path": "/",
    "secure": true,
    "sameSite": "no_restriction",
    "expirationDate": 1766000000.0
  }
  // …more cookies
]
```

5. Paste the **entire array** into Railway env var `TN_SESSION_COOKIES`. One-line JSON is fine.

![Screenshot: Cookie-Editor extension popup with the Export button highlighted](docs/screenshots/cookie-editor-export.png)

### Railway env setup

![Screenshot: Railway Variables panel with TN_SESSION_COOKIES, TN_TOKEN, TN_REFRESH_TOKEN, TN_THREAD_ID rows](docs/screenshots/railway-variables.png)

### What the bot does with it

- On boot and every 20 h, launches headless Chromium, restores the cookies, visits `app.true-north.xyz`.
- If the session is still authenticated, reads `privy:token` + `privy:refresh_token` from `localStorage` and updates the bot state.
- If the session has expired (redirect to login), posts a warning to `#claude-integration` asking you to `!setcreds` or re-export cookies.

### Railway install flow (nixpacks.toml + start.sh)

Two-layer setup because Railway's build → runtime filesystem copy has been observed to drop the default Chromium cache:

**`nixpacks.toml`** (build phase)

1. `[phases.setup]` → installs Chromium's native system libs via `apt` (libnss3, libatk1.0-0, libpango-1.0-0, …).
2. `[variables]` → exports `PLAYWRIGHT_BROWSERS_PATH=/app/.playwright-browsers` so the Chromium binary lands under `/app` (which Railway preserves between build and runtime).
3. `[phases.install]` → `pip install -r requirements.txt` + `PLAYWRIGHT_BROWSERS_PATH=... python -m playwright install chromium`. No `--with-deps` here — that needs sudo which the build container refuses. The apt step above already handles native libs.
4. `[start]` → `bash start.sh` (not `python bot.py`).

**`start.sh`** (runtime safety net)

```bash
#!/bin/bash
set -e
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/app/.playwright-browsers}"
# Idempotent — Playwright skips the download if Chromium is already there
# (~1 s warm-path). If the build phase somehow dropped the binary, this
# re-fetches it at boot (~30 s cold-path).
python -m playwright install chromium 2>&1 | tail -20
exec python bot.py
```

This two-layer approach means **even if the build phase fails to land Chromium, the bot still recovers on boot**.

### Expected boot log (Railway)

```
[BOOT] PLAYWRIGHT_BROWSERS_PATH=/app/.playwright-browsers
[BOOT] Installing Chromium via start.sh (idempotent)…
Downloading Chromium 120.0.6099.28 …
[BOOT] Chromium install step completed.
[BOOT] Chromium cache dir contents:
drwxr-xr-x 2 root root 4096 …  chromium_headless_shell-1208
[BOOT] Launching bot.py
[BOOT] Harvester preflight: playwright=True browsers_path=/app/.playwright-browsers chromium_present=True (/app/.playwright-browsers/chromium_headless_shell-1208/chrome-linux/headless_shell)
DCT TrueNorth Bot online as …
[BOOT] TN_SESSION_COOKIES present; running cookie harvester…
[Harvester] extracted tokens (access_len=2048, refresh_len=64)
```

If `chromium_present=False` or the pre-flight line reports "chromium binary missing", the harvester is automatically skipped and `!health` surfaces the exact problem — no more opaque `BrowserType.launch: Executable doesn't exist` tracebacks.

First deploy takes ~2–3 minutes longer than normal (Chromium is ~170 MB). Warm redeploys are fast again.

### Memory impact — size your Railway instance accordingly

- Idle bot (Python + discord.py + deps): **~120 MB RAM**
- While the harvester runs (Chromium spawned, ~25–30 s): **+180–220 MB**, so ~320 MB peak
- After harvest completes and Chromium exits: back to ~120 MB

**Plan for at least 512 MB on Railway.** The free tier (256 MB) is too tight once Chromium launches and will OOM. Bump to Starter ($5/mo) or higher.

### Running it manually (dev / debugging)

On your local machine:

```bash
pip install playwright
python -m playwright install chromium
```

If Playwright isn't installed the harvester is a clean no-op — `run_cookie_harvest()` returns `{"ok": False, "reason": "playwright not installed …"}`, `!health` shows `Harvester available: no`, and nothing else in the bot is affected. You still have `!setcreds` + env vars as the fallback paths.

---

## Env var quick reference

| Env var | Required | Purpose |
|---|---|---|
| `DISCORD_BOT_TOKEN` | ✅ | Discord bot token (Discord Developer Portal) |
| `ANTHROPIC_API_KEY` | ✅ | Claude Sonnet middleman + Haiku fallback |
| `CH_CLAUDE_INTEGRATION`, `CH_ASIA_SESSION`, `CH_LONDON_SESSION`, `CH_US_SESSION`, `CH_REGIME_OUTLOOK`, `CH_TRADES` | ✅ | Discord channel IDs |
| `TN_TOKEN` | ⚠️ fallback | Initial access token (cache / `!setcreds` overrides) |
| `TN_REFRESH_TOKEN` | ⚠️ fallback | Initial refresh token |
| `TN_THREAD_ID` | ⚠️ fallback | Initial thread UUID |
| `PRANAY_DISCORD_ID` | ✅ for `!setcreds` | Discord user ID allowed to run `!setcreds` |
| `TN_SESSION_COOKIES` | optional | JSON array of browser cookies (Cookie-Editor format) |
| `TN_APP_ORIGIN` | optional | Override `Origin` header on Privy refresh (defaults to `https://app.true-north.xyz`) |
| `PRIVY_APP_ID` | optional | Defaults to TrueNorth's current Privy app id |
| `TN_DEBUG` | optional | `1` enables per-SSE-chunk logging (noisy; turn off after use) |

---

## Commands reference

| Command | Who | What it does |
|---|---|---|
| `!health` | Anyone | Uptime, thread id + source, access-token exp + source, last TN success / error |
| `!refreshtoken` | Anyone | Runs every Privy refresh variant, posts a per-variant diagnostic embed |
| `!setcreds <json or 3 values>` | Owner only | Rotates access + refresh + thread_id at runtime; deletes your message |
| `!brief asia/london/us/all` | Anyone | Manually triggers a session brief |
| `!trades` | Anyone | Fresh 3-setup scan |
| `!regime` | Anyone | Daily regime outlook |

---

## What can go wrong during setup

| Symptom | Cause | Fix |
|---|---|---|
| `!setcreds` replies `access_token rejected: expected 3 dot-separated segments, got 1` | You pasted a value with surrounding double-quotes, or wrong value entirely | Strip the quotes around `privy:token`; make sure you copied from `privy:token`, not `privy:id_token` |
| `!setcreds` replies `thread_id ... is not a valid UUID` | You pasted the wrong field from the Network payload, or truncated the UUID | Re-check the `/streams` request Payload tab — the value should be `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` exactly |
| `!setcreds` replies `Credentials stored but thread ping returned 0 chunks in 30s` | Thread_id is syntactically valid but TrueNorth doesn't recognise it | Send a message in the TrueNorth UI yourself — that refreshes the thread — then re-copy the thread_id from the latest request payload |
| `!setcreds` replies `could not write thread_cache.json` | Filesystem is read-only or disk full | Check Railway's disk usage; on rare occasions Railway volume mounts go stale — a redeploy usually fixes it |
| Your `!setcreds` message didn't get deleted | Bot lacks Manage Messages | Grant the permission ([section above](#prerequisite-bot-must-have-manage-messages)) and delete the message yourself for now |
| `!refreshtoken` embed shows every variant returning 400 / 403 | Refresh token is revoked (hit 30-day limit, or browser was logged out) | Grab fresh values via DevTools → `!setcreds` |
| `!refreshtoken` embed shows HTTP 403 `missing_origin` | `TN_APP_ORIGIN` env var is wrong | Default is `https://app.true-north.xyz`; don't override unless TN moves domains |
| Cookie harvester boot log shows `playwright not installed` | Playwright wasn't installed on this Railway instance | `pip install playwright && python -m playwright install chromium` in the Railway shell, redeploy |
| Cookie harvester alerts `session invalid: redirected to /login` | Cookies expired | Re-export cookies from Cookie-Editor, update Railway env, redeploy |
| `!health` shows `Token source: env (cache missing or stale on boot)` | Fresh deploy or cache got wiped | Harmless — the next auto-refresh (within 8 h) populates the cache |

Still stuck? Check [Railway logs](README.md#reading-railway-logs). Every critical path prints a tagged line with enough context to diagnose.
