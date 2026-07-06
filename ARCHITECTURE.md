# Architecture (v3)

Technical walkthrough of `dct-truenorth-bot` for anyone modifying the code. For the operator overview see [README.md](README.md). For the stacks this replaced — v1 (Privy auth, SSE agent, Mac harvester) and v2 (Claude synthesis) — see git history: v1 ≤ `2492ede`, v2 ≤ `aeb03c5`.

## File layout

| File | Purpose |
|---|---|
| `bot.py` | Everything runtime: TN tool client, raw gathering, rule engines (brief/regime/setup/tweet), outcome tracking, embeds, scheduler, commands, `/healthz`. Single file by choice. |
| `tests/test_bot.py` | Full unit suite, no network; fixtures mirror live TN response shapes. |
| `tests/conftest.py` | Stubs env vars so `import bot` works in CI. |
| `Procfile` / `runtime.txt` / `requirements.txt` | Railway build: Python 3.11.8, `web: python bot.py`. No LLM SDK. |

## Request flow: one scheduled brief

1. **Cron fires** in the market's local timezone (`SCHEDULE` in bot.py) — e.g. `Asia/Tokyo 08:45`.
2. **`run_session_brief(session)`** calls `gather_raw(BRIEF_SOURCES)`: seven TN tools fetched in parallel via `asyncio.gather` (BTC+ETH market info, BTC+ETH 1d TA, derivatives, 1d scanner, 24h events); each `tn_call_safe` failure → `None`, never an exception.
3. **`tn_call`** POSTs `{"toolName", "arguments"}` to `https://api.adventai.io/api/agent-tools/call`. Keyless; optional raw `Authorization` header from `TN_AUTH_TOKEN`. One retry on 5xx/network errors (5 s backoff), fast-fail on 4xx. `_parse_tool_envelope` unwraps `{data: {result, isError}}` and flags app-gated stubs (`TNToolError(app_only=True)`).
4. **`check_regime_shift(session, ta=, derivs=)`** reuses the brief's own daily TA + derivatives: `detect_regime` (rule-based, ≥2 signals per side, UNKNOWN on sparse data so an outage can't fake a flip) vs the persisted baseline; a flip posts an orange alert to `#regime-outlook` and the baseline updates either way.
5. **`build_rule_brief(session, d)`** composes the embed text — pure function, no LLM: regime line, BTC/ETH headers (price, 24h/7d/30d), trend lines (MA position with levels, MACD state+momentum, RSI), range/ATR/volume line, nearest S/R channels with strength, funding/OI percentiles + nearest liquidation clusters, top movers with RS, junk-filtered event titles. Sections degrade independently; raises only if both BTC price and BTC TA are missing.
6. **`build_brief_embed`** wraps the text (sentence-aware truncation at 4000 chars) and appends the `DASHBOARD_URL` field. **`build_template_tweet`** (hot mover > funding extreme > regime read) goes to `maybe_autodraft` → Typefully v2 draft, if enabled.
7. **Failure handling:** zero sources → failure embed + `alert_ops`; builder exception → same. Partial sources → brief posts anyway.

The regime job (`regime_mwf`, Mon/Wed/Fri 06:00 IST) runs the same pipeline plus indices/7d-events/7d-scanner through `build_rule_regime_outlook` (adds the cross-asset strip and rotation).

## The other engines

- **`!setup` (`build_rule_setup`)** — weighted signal score from 4h TA: SMA20/50 position (±1 each), MACD state (±1) + momentum (±0.5), RSI zone (±1) + momentum (±0.5), Bollinger mid (±0.5). |score| ≥ 2.5 sets direction; ≥ 4 is High conviction; otherwise an honest no-setup with the score in the reasoning. Entry = price ± 0.25 ATR, stop = 1.5 ATR, TP1/TP2 = 1.5R/3R — coherent by construction. Same output dict the v2 Claude generator produced, so embeds and tracking never changed. Repeat `!setup` for the same asset within 6 h (`_find_recent_open`) reuses the open trade instead of double-logging.
- **Outcome tracking (`track_setups`, every 15 min)** — one batched `historical_bars` call (15 m candles from the oldest open entry, all instruments at once); `evaluate_setup_candles` scans chronologically from entry and the first **wick** through a level decides: stop → LOSS, TP1 → WIN, both-in-one-candle → conservative LOSS (intra-candle order unknowable). Fills book at the level; `resolved_at` is the crossing candle's close time. Spot check remains as per-instrument fallback; 48 h no-trigger → EXPIRED. Resolutions reply to the original setup message; `setups.json` is written atomically (`.tmp` + fsync + `os.replace`).
- **`!scan`** — one `performance_scanner` call rendered as an aligned ANSI table (padding applied before color codes).
- **`!winrate` (`compute_winrate`)** — pure stats over the log: W/L/E, win rate excluding expired, best/worst %, realized R:R on wins.

## Why these choices

- **Rule engines, not an LLM** — v2's Claude layer died twice on credentials/credits and cost money to idle. The TN tools return structured indicators; formatting them deterministically gives briefs where every number is source-true, output is consistent, and the marginal cost is zero. The raw-dict pipeline is LLM-ready if prose is ever wanted again (plug a synthesis fn between `gather_raw` and the embed).
- **Agent-tools REST, not the old SSE agent** — TrueNorth v2 broke `/sse/v2/streams`; the CLI is a thin wrapper over this REST API. Calling it directly avoids shipping Node on Railway.
- **Wick-based resolution** — a 15-min spot check misses intra-interval stop-outs that recover, silently inflating the public win rate. Candle highs/lows close that hole; ambiguous candles resolve against the record, not for it.
- **Per-market timezone crons** — "15 minutes before open" is only stable in the market's own timezone.
- **Persistent state is two small JSON files** — `setups.json` (track record) and `last_regime.json` (shift baseline) under `CACHE_PATH` (mounted volume in prod). Everything else is stateless.

## Error handling

| Failure | Behavior |
|---|---|
| One TN tool fails | Logged; that brief section is dropped; brief still posts |
| TN tool 5xx / network | One retry after 5 s, then treated as failed |
| App-gated tool, no token | Skipped silently (expected, not an error in `!health`) |
| All TN tools fail | Failure embed in target channel + `CH_OPS` alert |
| Brief/outlook builder raises | Failure embed + `CH_OPS` alert; error in `!health` ("Last build error") |
| Regime data sparse (<2 signals) | UNKNOWN → check skipped, baseline untouched, never a false flip |
| Candle data missing for a setup | Spot-check fallback for that instrument |
| Typefully/tweet failure | Logged `[AUTODRAFT]`, draft skipped, brief unaffected |
| Discord channel missing | Logged, job returns False |

## Testing

`pytest` — no network, sub-second. Fixtures mirror live TN response shapes (S/R channels, funding/OI/liq sections, scanner rows, junky event titles, string-OHLC candles). Coverage: envelope contract, gather_raw partial failure, every rule-engine branch (brief sections + degradation, regime rules + outage guard, setup score/levels/ATR math/coherence, tweet variants), wick resolution (incl. the close-recovers case and both-levels-one-candle), dedupe window, persistence round-trips, embeds, schedule/timezones, `tn_call` retries via `httpx.MockTransport`, and a no-LLM-dependency guard.

Not covered (verified by hand against the live API): TN response drift, APScheduler firing, Discord I/O.
