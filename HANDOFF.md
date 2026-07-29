# Corgi Calls Market Desk — session handoff

**Read this first, then `dct-truenorth-bot/ARCHITECTURE.md` and `README.md`.** Everything below
is verified against the live system on 2026-07-29, not remembered. Where a number is stated,
re-check it before relying on it — the system is live and moves.

---

## 1. What this is

Three surfaces, all live, all under `/Users/pranaysharma/TN Integration/`:

| Surface | Repo / URL | Deploy |
|---|---|---|
| **Discord bot** (the engine) | `dct-truenorth-bot` · github.com/ethpranay-blip/dct-truenorth-bot | Railway, auto-deploys `main` |
| **Dashboard** (the view) | `tn-dashboard` · https://tn-dashboard-five.vercel.app | Vercel, auto-deploys `main` |
| **TrueNorth app twin** | truenorth.xyz (Pranay is Pro) | holds the strategy in agent memory |

Bot API base: `https://dct-truenorth-bot-production.up.railway.app`

```
TrueNorth keyless API → bot rule engines → /api/live, /api/track-record, /api/setup, /api/watchlist → dashboard
TN MCP (Pranay's OAuth — Claude sessions ONLY) → POST /api/flow → dashboard whale panel
```

## 2. Hard rules — do not break these

1. **Never push `dct-truenorth-bot` main without Pranay's explicit "ship it."** Show the diff first.
   Dashboard pushes he is fine with directly.
2. **Gate before committing bot code:** `pytest` green (**211 tests**) + `python -m py_compile bot.py`.
3. **No SMA anywhere.** `grep -c sma bot.py` must return 0. TrueNorth's TA tool only ships SMA, so
   EMAs are computed from raw bars and merged in (`_ema_states`, `gather_raw`).
4. **Never wire Pranay's TrueNorth OAuth into the bot.** Those MCP tools are session-only (verified
   absent from the keyless REST). Putting credentials in the bot is what killed v1.
5. **Verify by looking, not by building.** Bare text renders silently in JSX; a build passing is not
   proof. Screenshot or fetch the rendered page.

## 3. Strategy v2 (EMA-only)

**Score, max ±5.5** — 4H for crypto, daily for stocks/commodities:

| Component | Bullish | Bearish | Weight |
|---|---|---|---|
| Price vs EMA20 | above | below | 1.0 |
| Price vs EMA50 | above | below | 1.0 |
| MACD(12,26,9) state | bull | bear | 1.0 |
| MACD momentum | rising | falling | 0.5 |
| RSI(14) | >55 | <45 | 1.0 |
| RSI momentum | rising | falling | 0.5 |
| EMA20 vs EMA50 stack | 20 over 50 | 20 under 50 | 0.5 |

Gates: `<2.5` no trade · `2.5–4.0` watchlist · `≥4.0` high conviction.
Auto-signal fires only on **score ≥4.0 AND RVWAP bias agrees AND (rotation picks) regime agrees**.

**The stack replaced Bollinger** — a Bollinger midline is an SMA20 by definition, and price-vs-EMA20
was already scored, so the stack adds alignment instead of duplicating position.

**Two lenses:** *structure* = daily EMA50; *tape* = 4H EMA50 + 7d RVWAP. The headline states both so
it can never claim momentum the tape denies.

**Levels:** entry ±0.25 ATR · stop 1.5 ATR · **TP1 2.5R / TP2 4R** · expiry 48h crypto / 7d daily
assets, closing **at market** (a real position doesn't evaporate).

**$100 per signal:** every shown signal is treated as a real $100 position. One asset = one open
position, ever.

**Strategy versioning:** `STRATEGY_VERSION` is stamped on each setup. On bump, older rows archive to
`setups_archive.json` (nothing deleted), open ones close at last price, and the public record
restarts at zero — so win rate always describes exactly one strategy. **Currently v2.**

## 4. Open finding — the exit geometry (measured 2026-07-29, NOT yet acted on)

The live record is 0W/4L, −$8.30 per $100 across 4 closed trades. **The tracker is correct** — I
verified one stop-out against 15-minute candles and it matched to the minute. The issue is geometry:

Random-entry test on BTC 4H bars, Apr–Jul 2026 (468 samples), measuring *only* stop-vs-target
(no signal filter):

| Window | SHORT tp/stop/expire | wr | exp | LONG tp/stop/expire | wr | exp |
|---|---|---|---|---|---|---|
| **48h (current)** | 80/187/201 | 30.0% | +0.03R | 24/269/175 | 8.2% | −0.45R |
| 96h | 116/226/114 | 33.9% | +0.14R | 47/337/72 | 12.2% | −0.48R |
| **7d** | 148/268/22 | 35.6% | **+0.23R** | 62/338/38 | 15.5% | −0.42R |
| 10d | 144/269/7 | 34.9% | +0.22R | 69/322/29 | 17.6% | −0.36R |

**Read:** TP1 at 2.5R = 3.75 ATR while the stop is 1.5 ATR. In 48h (12 × 4H bars) that target is
reached ~17% of the time for shorts and ~5% for longs, and **43% of trades never decide at all** and
expire. The 2.5R minimum, 1.5 ATR stop and 48h window are mutually inconsistent.

**Caveat that matters:** Apr–Jul 2026 was a BTC downtrend, so the short/long asymmetry here is regime
bias, not edge. Do not read it as "shorts work."

**Recommended (needs Pranay's decision — it is a strategy change and would bump the version and reset
the record):** extend the crypto window from 48h toward ~7 days. Everything else stays.

**Not yet done:** the full backtest. A written spec exists in the 2026-07-29 chat; Pranay has a
separate codebase for running it. The two ablation rows that matter most: **SMA vs EMA** (does the v2
switch earn its keep?) and **RVWAP filter on/off**.

## 5. Bot internals worth knowing

- Single file `bot.py` by choice. Tests in `tests/test_bot.py`, no network (fixtures mirror live shapes).
- Local venv is Python 3.13 with `audioop-lts` (venv-only; discord.py 2.3.2 needs the removed
  `audioop`). Railway runs 3.11.8.
- **Endpoints:** `/healthz` · `/api/live` (open setups, tape, crowd, cross-asset, stocks/etfs/sectors,
  brief, flow) · `/api/track-record` · `/api/setup?ticker=X` (90s cache — **successes only**, errors
  bypass) · `GET|POST /api/watchlist` · `POST /api/flow`. Both write paths gated by
  `WATCHLIST_SECRET`.
- **Jobs (IST):** briefs 05:15 / 12:15 / 18:45 · MWF 06:00 macro · regime + outcome tracker every
  15 min · **auto-signals every 4h** (UTC :10) · dashboard cache 15 min.
- **Crowd read is keyless and bot-computed** (funding APR + 7d percentile, OI + percentile).
  **Whales are pushed** by a Claude scheduled task (`flow-intel-push`, every 4h, runs while the
  Claude app is open). Panel auto-hides after 13h of no push. **Polymarket was removed** — do not
  reintroduce it.
- **`CH_SIGNALS` needs the numeric Discord channel ID, not a webhook URL.** That mistake crash-looped
  prod twice; `_optional_channel_id` now degrades with a warning instead of dying.
- `CACHE_PATH=/data` (Railway volume, verified). Track record, watchlist, regime baseline and flow
  snapshot all survive redeploys.

## 6. Dashboard internals

Next.js **16.2.9** with **Cache Components** (`"use cache"` + `cacheLife`, 5-min revalidate).
`AGENTS.md` in that repo says: read `node_modules/next/dist/docs/` before writing code — it is not
the Next.js you remember.

Identity is "the desk": blue-black ground re-tinted app-wide via one Tailwind v4 `@theme` block,
tape-gold accent (chrome only), IBM Plex Mono for all figures, Fraunces display, live session clocks.

**Section order (Pranay's spec):** Search → Market map → Sector rotation → Top-ranked setups → Live
signals → Smart money & flow → Screener → Stocks & ETFs → Leaders & laggards → Catalysts → Options →
Track record → Cross-asset → Signal summary → Brief → Watchlist.

**Traps already hit:**
- React Compiler forbids manual `useMemo` and ref reads during render; `set-state-in-effect` needs a
  targeted disable for localStorage/clock init.
- Formatters must guard `null` **and** `undefined` — a renamed API field with the bot deploy lagging
  crashed prerender on `undefined.toPrecision`.
- A reorder script's leftover `@@placeholder@@` markers shipped to production because the build
  passed and nobody looked at the page.
- The TrueNorth preview pane reports "desktop only" / `loggedIn:false` until you
  `resize_window` to ≥1440px.

## 7. People

**Corgil** (@Corgil) is Pranay's trading partner and reviews the system. He was right that the old
hero read a daily SMA50 and claimed "momentum building" while the 4H tape was breaking down — that
drove the entire EMA v2 rework. Take his critiques seriously.

His open suggestions, **not built**: backtest of the 4H EMA50 + 7d RVWAP combo, and a Hyperdash-style
retail-vs-smart-money split.

## 8. State at handoff (2026-07-29 16:00 IST)

- Bot **640a2b1** · dashboard **a578351** · both in sync, 0 uncommitted · 211 tests green
- All endpoints 200; `/api/live` complete; flow snapshot 6.9h old; no overdue open positions
- Record: **7 tracked, 3 open, 4 closed, 0W/4L, −$8.30 per $100.** All four closed were genuine
  stop-outs, verified against candles
- Open: FARTCOIN SHORT (34h/48h), TSLA SHORT and AAPL LONG (90h/168h)
- Watchlist: AAPL, FARTCOIN, BTC, ETH, FET, LTC
- TN twin re-synced to EMA; Pranay accepted the memory card

**The single open decision:** whether to extend the crypto expiry window (§4). It is his call, not
the assistant's — it changes the strategy and resets the record.
