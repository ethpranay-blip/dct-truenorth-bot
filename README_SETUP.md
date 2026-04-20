# DCT TrueNorth Bot — Credential Setup

Stopgap playbook while TrueNorth's official API key is in flight. The bot needs three pieces of auth state from `app.true-north.xyz`:

1. **Access token** — Privy JWT, lifetime ~24 h.
2. **Refresh token** — Privy opaque string, lifetime 30 d, single-use (rotated on each refresh).
3. **Thread ID** — a UUID that identifies your TrueNorth chat thread.

You can rotate all three via Discord without touching Railway (primary path) or keep Railway's env vars as the long-lived fallback.

---

## 1. Grab the three values (one DevTools session)

1. Open `https://app.true-north.xyz` in Chrome and log in.
2. Open DevTools → **Application** → **Local Storage** → `https://app.true-north.xyz`.
   - Copy `privy:token` → **access_token**.
   - Copy `privy:refresh_token` → **refresh_token**.
   - Strip surrounding double-quotes if present.
3. Switch to the **Network** tab and filter `streams`. In the TrueNorth UI, send any message (e.g. `"ping"`).
4. Click the newest `POST …/api/discovery-agents/sse/v2/streams` request.
   - **Payload** → copy `thread_id` (UUID). This is your **thread_id**.

That's everything the bot needs.

---

## 2. `!setcreds` — rotate via Discord (primary)

Only the Discord user whose ID matches the Railway env var `PRANAY_DISCORD_ID` can run this command.

### JSON form (recommended — no whitespace issues)

```
!setcreds {"access_token":"eyJ…","refresh_token":"rt_abc…","thread_id":"78536e88-e440-43dd-a61d-584640f8792b"}
```

### Space-separated form

```
!setcreds eyJ… rt_abc… 78536e88-e440-43dd-a61d-584640f8792b
```

### What happens

- The bot validates the shapes (JWT for access, UUID for thread_id, length ≥ 20 for refresh).
- **Your message is deleted immediately** to keep JWTs out of chat history. Grant the bot **Manage Messages** on `#claude-integration` so this can work.
- The bot tests the thread with a 30 s ping. You'll see either:
  - ✅ `Credentials updated. Thread valid. Access token exp: <time>.`
  - ⚠️ `Credentials stored but thread ping returned 0 chunks in 30s. Double-check the thread_id …`

Bad input gets an inline error (e.g. `access_token rejected: expected 3 dot-separated segments, got 1`).

---

## 3. `TN_SESSION_COOKIES` — automatic harvester (backup)

If you export your browser cookies, the bot can re-harvest tokens on boot and every 20 h without you lifting a finger.

### Exporting cookies

1. Install [Cookie-Editor](https://cookie-editor.cgagnier.ca/) (or any extension that produces the same JSON array format).
2. Visit `https://app.true-north.xyz` while logged in.
3. Click the extension → **Export** → **Export as JSON**.
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

5. Paste the **entire array** into Railway env var `TN_SESSION_COOKIES` (one-line JSON is fine — env vars don't care about pretty-printing).

### What the bot does with it

- On boot and every 20 h, launches headless Chromium, restores the cookies, visits `app.true-north.xyz`.
- If the session is still authenticated, reads `privy:token` + `privy:refresh_token` from `localStorage` and updates the bot state.
- If the session has expired (redirect to login), posts a warning to `#claude-integration` asking you to `!setcreds` or re-export cookies.

Playwright is optional. On a fresh Railway deploy run once:

```bash
pip install playwright
python -m playwright install chromium
```

If Playwright isn't installed, the harvester is a no-op and you rely on `!setcreds` / env vars only. The main bot functionality is unaffected.

---

## 4. Env var quick reference

| Env var | Required | Purpose |
|---|---|---|
| `DISCORD_BOT_TOKEN` | ✅ | Discord bot token |
| `ANTHROPIC_API_KEY` | ✅ | Claude fallback + Sonnet middleman |
| `CH_CLAUDE_INTEGRATION`, `CH_ASIA_SESSION`, `CH_LONDON_SESSION`, `CH_US_SESSION`, `CH_REGIME_OUTLOOK`, `CH_TRADES` | ✅ | Channel IDs |
| `TN_TOKEN` | ⚠️ fallback | Initial access token (overridden by cache / `!setcreds`) |
| `TN_REFRESH_TOKEN` | ⚠️ fallback | Initial refresh token |
| `TN_THREAD_ID` | ⚠️ fallback | Initial thread UUID |
| `PRANAY_DISCORD_ID` | ✅ for `!setcreds` | Discord user ID allowed to run `!setcreds` |
| `TN_SESSION_COOKIES` | optional | JSON array of browser cookies (Cookie-Editor format) |
| `TN_APP_ORIGIN` | optional | Override `Origin` header on Privy refresh (defaults to `https://app.true-north.xyz`) |
| `PRIVY_APP_ID` | optional | Defaults to the current TrueNorth Privy app id |
| `TN_DEBUG` | optional | `1` to log every raw SSE chunk for debugging |

---

## 5. Commands reference

| Command | Who | What it does |
|---|---|---|
| `!health` | Anyone | Uptime, thread id, access-token exp, last TN success / error |
| `!refreshtoken` | Anyone | Runs every Privy refresh variant, posts a per-variant diagnostic embed |
| `!setcreds <json or 3 values>` | Owner only | Rotates access + refresh + thread_id at runtime; deletes your message |
| `!brief asia/london/us/all` | Anyone | Manually triggers a session brief |
| `!trades` | Anyone | Fresh 3-setup scan |
| `!regime` | Anyone | Daily regime outlook |

---

## 6. Troubleshooting

- **`⚠️ TN_THREAD_ID is invalid — TN-dependent commands are disabled`** on boot → the ping against the configured thread returned zero chunks. Run `!setcreds` with a fresh thread_id from the Network tab payload.
- **`Privy refresh FAILED`** in `!refreshtoken` → the embed shows one field per variant with its HTTP status and body. If every variant returns 400 or 403, the refresh token is revoked — grab a new pair via `!setcreds`.
- **Bot can't delete your `!setcreds` message** → grant it the **Manage Messages** permission on `#claude-integration`. Until then, delete the message yourself right after sending.
- **Cookies expired alert in `#claude-integration`** → re-export the cookie JSON from the Cookie-Editor extension and replace `TN_SESSION_COOKIES` in Railway.
