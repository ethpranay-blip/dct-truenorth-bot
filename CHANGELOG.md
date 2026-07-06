# Changelog

History of notable changes to `dct-truenorth-bot`. Grouped by week, with key commit hashes for reference.

## v3 + track-record integrity — 2026-07-06

- **2026-07-06 · `f3b630c`** — **Wick-accurate outcome resolution + `!setup` dedupe.** The tracker now resolves open setups against 15-minute candle highs/lows (one batched `historical_bars` call for all open instruments) instead of spot checks — a stop that wicks and recovers no longer counts as still-open, so the public win rate can't overstate. Both-levels-in-one-candle books as a conservative LOSS; fills book at the level; `resolved_at` is the crossing candle's time. Repeat `!setup` for the same asset within 6 h reuses the open trade (reuse notice + jump link) instead of double-logging. 146 tests.
- **2026-07-06 · `9534db3`** — **v3: removed the LLM entirely.** After the Anthropic credit balance emptied on Jul 3 (second billing incident; briefs silently dead for 3 days), all synthesis went rule-based: `build_rule_brief` (regime line, BTC/ETH trend + levels, S/R channels with strength, funding/OI percentiles + liq magnets, movers, filtered headlines), `build_rule_regime_outlook` (+ cross-asset strip), `build_rule_setup` (weighted 4h signal score; ATR levels at 1.5R/3R; honest no-setup), `build_template_tweet`. `anthropic` removed from requirements; `ANTHROPIC_API_KEY` no longer read. Briefs fetch clean 1d TA for BTC+ETH; the regime check reuses brief data instead of refetching. Zero API spend; nothing left that can bill or expire. 135 tests.

## Feature wave — 2026-06-27 → 2026-06-29

- **2026-06-29 · `aeb03c5`** — **Typefully auto-draft moved to the v2 API** (`POST /v2/social-sets/{id}/drafts`, `Authorization: Bearer`) after v1 started returning 403. Social set configurable via `TYPEFULLY_SOCIAL_SET_ID` (default @Corgil_).
- **2026-06-28 · `40e4d55`** — **Regime-shift detection.** Rule-based RISK-ON/OFF/NEUTRAL derived on every brief run (MA position, funding sign, RSI, VIX when present; ≥2 signals per side, UNKNOWN on sparse data so outages can't false-alert); flips post an orange alert to `#regime-outlook`; baseline persisted to `last_regime.json`.
- **2026-06-28 · `aa17c6b`** — **Setup outcome tracking.** Every LONG/SHORT `!setup` logged to `setups.json` (atomic writes), resolved by a 15-min job, announced as a reply to the original message (green WIN / red LOSS / grey EXPIRED with direction-adjusted %), surfaced via the new `!winrate` command and a `!health` line.
- **2026-06-28 · `1f2d375`** — **Typefully auto-draft** (opt-in via `AUTO_DRAFT_ENABLED` + `TYPEFULLY_API_KEY`): each posted brief ghostwrote a tweet and saved it as a draft, never published. Fully failure-isolated from the brief.
- **2026-06-27 · `e694467`** — **`!scan [1-25]`** — zero-cost ANSI-colored top-N by 7-day relative strength vs BTC (works even with no LLM key — which mattered a day later).
- **2026-06-27 · `99dd67d`** — **`!setup <ticker>`** — on-demand single-asset trade setup (then Claude-generated; rule-based since v3). Ticker→CoinGecko-id map, channel gating via `SETUP_ALLOWED_CHANNELS`, 60 s/user cooldown.

## v2 pivot — 2026-06-12

- **2026-06-12 · `a9eb804`** — **Pivot to TrueNorth v2 agent-tools engine.** TrueNorth v2 retired the conversational SSE endpoint (`/sse/v2/streams` rejects the v1 request shape), killing scheduled briefs. Rebuilt on the keyless agent-tools REST API (the `@truenorth-ai/cli` backend): parallel tool calls (TA, derivatives, scanner, events, market info) build a JSON snapshot; `claude-opus-4-8` synthesizes the narrative. Briefs now fire 15 min before each market's local open with per-market-timezone crons (Tokyo 08:45 JST, London 07:45 UK, NY 09:15 ET — DST-proof); regime stays MWF 06:00 IST. Channel layout simplified: `#claude-integration` chat (+ Sonnet middleman) and `#trades` posting removed — briefs link to the setups dashboard (`DASHBOARD_URL`) instead. Deleted the entire v1 credential stack: Privy refresh, token/thread caches, `/credentials` webhook, Mac-local harvester (`harvester_local/`), `README_SETUP.md`. New test suite: 54 passing.

## Week 3 — 2026-04-18 through 2026-04-20

Auth hardening + durability. Most of this week was spent turning a "working on a happy path" bot into one that survives Railway restarts, Privy token rotation, and stale thread ids without manual intervention.

- **2026-04-20 · `5ca2b3f`** — Thread-id cache now writes atomically (`.tmp` → `fsync` → `os.replace`). Flipped boot precedence from env→cache to **cache→env**. `!health` shows "Thread source" alongside "Token source". Fixed root cause of "thread_id lost on Railway restart".
- **2026-04-20 · `82cd84f`** — Token cache uses atomic writes. Added JWT-freshness grace window for boot-time precedence. Boot always does one refresh call before handling commands. Solves "Privy rotates refresh tokens, we lost them on restart".
- **2026-04-20 · `fd233d3`** — `!setcreds` owner-only command: rotate all three auth values from Discord without touching Railway. Auto-deletes the caller's message for security. Playwright cookie harvester module (`harvester.py`) — optional backup that re-extracts tokens from browser cookies every 20 h.
- **2026-04-19 · `9baceb8`** — Stopped minting fake UUIDs for threads (previously fell back to `uuid.uuid4()` which TN rejected with empty-200 loops). Validate-on-boot via 30 s ping. Bounded retries at 3 attempts, back-off (10 s, 30 s). Fast-fail on `empty_200` instead of retrying forever.
- **2026-04-19 · `9fffa9a`** — Orphan `</Anchor>` tags stripped. Stale-date override (TN hallucinates 2025 dates — body strip + authoritative date in title). Header-safe truncation (no "5...." dangling bits). Leading / trailing `---` stripped from risk flag body.
- **2026-04-19 · `47324de`** — Privy refresh tries 5 body+header variants until one returns 200; caches the winner. `<sp>` tool-use markup + lowercase XML stripped. SSE chunks now stitched with overlap-dedup to kill repeated preambles. Brief embed summary field + table fencing.
- **2026-04-19 · `3970549`** — Single `sanitize_tn_text()` choke-point. Added Origin + Referer headers to Privy refresh (fixed `403 missing_origin`). Fixed false-positive failure alerts on HTTP 200 + empty stream. `extract_risk_flag` rewrite (was capturing only "s" of "Session Risk Flags").
- **2026-04-19 · `f6ec171`** — API thread-create probe across 7 candidate endpoints with UUID fallback. `!brief` confirmation message in invoking channel.
- **2026-04-19 · `e1643b2`** — Double `$` on prices fixed (parse stores clean numbers, display layer adds `$` once). Context bullets strip ticker headers + `<Token>` XML. `!refreshtoken` diagnostic embed. `tn_state` dict replaces a flaky `global` rebinding pattern for observability counters.
- **2026-04-18 · `b5ee7a5`** — Hardened TN integration: Privy token auto-refresh loop, dynamic thread creation, `!health` command, pytest test suite bootstrapped.

## Week 2 — 2026-04-14 through 2026-04-15

Feature build-out after the initial deployment got stable enough to post real briefs.

- **2026-04-15 · `f781e72`** — `#claude-integration` routing: default to TrueNorth passthrough, `!` prefix invokes Claude Sonnet as a middleman that calls TN multiple times in parallel with different angles.
- **2026-04-14 · `3bd7a7a`** — Emoji rendering fix: escape sequences replaced with actual Unicode glyphs in source (Discord was rendering the escaped form as literal text).
- **2026-04-14 · `4ba40c2` + `0c4689e`** — Full bot rewrite: proper session-channel routing, embed formatting (direction color / rank emoji / conviction description), cleaner SSE parsing.

## Week 1 — 2026-04-08 through 2026-04-09

Initial deployment. Discovered and debugged TrueNorth's SSE format, Privy auth, markdown quirks.

- **2026-04-09 · `92bc777` / `0b39bf1` / `8ba3c2d` / `edc695c` / `cf55bbd` / `2f36bed`** — Trade embed iterations: from raw markdown dump → pipe-table parser → structured embeds per setup. Briefly experimented with Pillow-generated trade-card images (reverted once Discord embeds were good enough).
- **2026-04-09 · `8890e4c` / `ba2e153`** — Incremental fixes and dependency adjustments.
- **2026-04-08 · `07a6de6` / `13ad816` / `b487811`** — Parser stabilization: safer line-based parser (was getting hit by unicode-dash regex errors), trailing-markdown-garbage fixes.
- **2026-04-08 · `6fb02d7`** — Separated command dispatching from the free-form chat handler (commands shouldn't double-fire), initial `<Token>` tag stripping, added `re` import (was using regex without importing).
- **2026-04-08 · `79e0a02`** — SSE parser switched to reading `data.data.content` for `event_type=llm_output` events (TrueNorth's actual envelope shape).
- **2026-04-08 · `0fb383d`** — Removed the `thinking: false` field from TN request body (API rejected it — expected a dict, not a bool).
- **2026-04-08 · `2b5431c` / `56c5063` / `2b618b0` / `77d0268`** — Iterative SSE debugging: raw line logging, proper SSE Accept header, timeout tuning, and fixing an indentation error that broke imports.
- **2026-04-08 · `7ae0660`** — First Privy auto-refresh attempt (matured substantially in Week 3).
- **2026-04-08 · `3c95045` / `d74652d` / `9f60f26` / `b2bf6bd` / `daed9d9`** — Scaffolding: initial `bot.py`, `requirements.txt`, first deploy files. The bot's first working-from-scratch commits.
