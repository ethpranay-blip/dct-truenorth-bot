# Mac-local harvester

Standalone script that reads fresh TrueNorth credentials out of an already-logged-in Chrome profile (via Chrome DevTools Protocol) and POSTs them to the bot's `/credentials` webhook on Railway. No Playwright, no Chromium download on Railway.

## Prerequisites

- macOS with Python 3.8+ and Google Chrome.
- The bot is deployed to Railway with these env vars set:
  - `HARVESTER_SECRET` — shared secret (generate a random 32+ char string).
  - Railway has exposed a public URL for the bot (e.g. `https://dct-truenorth-bot.up.railway.app`).
- You're already logged in to `app.true-north.xyz` in Chrome.

## Setup (one time)

```bash
cd harvester_local
./setup.sh
```

The script installs `pychrome` and prompts for the bot webhook URL + `HARVESTER_SECRET`. Config lands in `~/.dct-harvester-config` (chmod 600).

## Launch Chrome with remote debugging

Keep this window open while the harvester runs. Closing it stops the harvester.

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/Library/Application Support/Google/Chrome"
```

The `--user-data-dir` flag reuses your normal profile so you stay logged in.

## Run a dry run to verify

```bash
python3 harvester.py --verify
```

Reads tokens + thread_id from Chrome, prints them, and does NOT POST. Check `~/.dct-harvester.log` for details.

## Run for real

```bash
python3 harvester.py
```

## Schedule via cron

```bash
crontab -e
```

Add (push every 8 h):

```
0 */8 * * * /usr/bin/python3 /absolute/path/to/harvester_local/harvester.py >> $HOME/.dct-harvester.log 2>&1
```

Tail the log:

```bash
tail -f ~/.dct-harvester.log
```

## Troubleshooting

| Log message | Fix |
|---|---|
| `could not reach Chrome DevTools at http://localhost:9222` | Chrome isn't running, or it wasn't launched with `--remote-debugging-port=9222`. Relaunch with the command above. |
| `no tab containing app.true-north.xyz` | Open `app.true-north.xyz` in the Chrome window launched with remote debugging. |
| `localStorage missing privy:token / privy:refresh_token` | Log in again in the Chrome tab. Cookies expired. |
| `no thread_id available` | Send any chat message in TrueNorth while the harvester runs — that triggers the `/streams` request the harvester needs. Or write a UUID to `~/.dct-harvester-thread` as a manual fallback. |
| `webhook HTTP 401: unauthorized` | `HARVESTER_SECRET` in `~/.dct-harvester-config` doesn't match Railway. Re-run `setup.sh`. |
| `webhook HTTP 503: HARVESTER_SECRET not configured on the bot` | Add `HARVESTER_SECRET` to Railway env and redeploy. |

## How it works

1. The harvester hits `http://localhost:9222/json` to list open Chrome tabs.
2. Picks the tab whose URL contains `app.true-north.xyz`.
3. Uses `pychrome` to attach to that tab via CDP.
4. Runs `window.localStorage.getItem('privy:token')` / `privy:refresh_token` via `Runtime.evaluate`.
5. Enables the `Network` CDP domain and waits up to 30 s for a POST to `/sse/v2/streams`; parses `thread_id` from the request body.
6. Falls back to `~/.dct-harvester-thread` or a UUID in the tab URL if no streams request fires.
7. POSTs `{access_token, refresh_token, thread_id}` to `<bot_url>/credentials` with the `X-Harvester-Secret` header.
8. The bot validates shapes, writes both caches atomically, clears the `thread_invalid` flag, and returns `{ok, access_token_exp, thread_id}`.

Everything happens on your Mac's loopback interface plus one outbound HTTPS call to Railway. No credentials touch disk beyond the bot's atomic cache.
