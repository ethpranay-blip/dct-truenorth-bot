# Architecture

Deep technical walkthrough of `dct-truenorth-bot` for anyone modifying the code. For the high-level overview, see the [main README](README.md). For credential setup, see [README_SETUP.md](README_SETUP.md).

## Table of contents

- [File layout](#file-layout)
- [Request flow: one `!trades` command](#request-flow-one-trades-command)
- [Scheduled brief flow](#scheduled-brief-flow)
- [Authentication state machine](#authentication-state-machine)
- [Thread-ID state machine](#thread-id-state-machine)
- [Error handling taxonomy](#error-handling-taxonomy)
- [Testing strategy](#testing-strategy)
- [Known limitations and technical debt](#known-limitations-and-technical-debt)

---

## File layout

| File | Purpose |
|---|---|
| `bot.py` | Everything runtime: Discord commands, TrueNorth SSE client, Privy refresh variants, APScheduler jobs, embed builders, cache I/O, observability state. ~2 300 lines. |
| `harvester.py` | Optional Playwright-based session-cookie harvester. Isolated so Playwright stays an optional dependency. |
| `requirements.txt` | Pinned Python deps: `discord.py==2.3.2`, `anthropic>=0.40.0`, `httpx>=0.27.0`, `apscheduler>=3.10.0`, `pytest>=8.0.0`. Playwright is intentionally **not** in here. |
| `runtime.txt` | `python-3.11.8` — tells Railway which Python to build against. |
| `Procfile` | `worker: python bot.py` — Railway launches the bot as a background worker (no HTTP). |
| `tests/conftest.py` | Sets stub env vars before importing `bot` so tests don't need real credentials. |
| `tests/test_parsers.py` | All 90+ unit tests in one file. |
| `token_cache.json` | Runtime state — atomic cache of access + refresh tokens. Gitignored. |
| `thread_cache.json` | Runtime state — atomic cache of the shared thread id. Gitignored. |
| `.gitignore` | Excludes `.env`, `__pycache__/`, `*.pyc`, both cache files, `.pytest_cache/`, `.venv/`. |

---

## Request flow: one `!trades` command

Step-by-step trace of what happens when a user types `!trades` in `#claude-integration`.

### 1. Discord gateway → `bot.py`

- discord.py's gateway delivers the message to the `on_message` event handler.
- `on_message` calls `bot.process_commands(message)` first, which dispatches to the `@bot.command(name="trades")` handler (`manual_trades`).
- `on_message` then checks if the channel is `CH["claude"]` and if so, routes free-form chat to either `handle_direct_tn` or `handle_sonnet`. For `!trades` specifically, `KNOWN_COMMANDS` causes the fallback chat handler to skip, so only `manual_trades` runs.

### 2. `manual_trades` builds the prompt

```python
prompt = (
    "Give me 3 high-conviction trade setups for right now… "
    "Format each as: $TICKER | DIRECTION | Conviction Level, "
    "then Entry, Stop Loss, Take Profit, R:R as labeled rows. …"
)
await ctx.send("⏳ Querying TrueNorth for trade setups...")
result = await query_truenorth(prompt)
```

### 3. `query_truenorth` — the central orchestrator

1. Picks a thread id. If `thread_id` is None, calls `ensure_thread_id()` which returns the in-memory shared thread (or None if invalid / unset).
2. If `tn_state["thread_invalid"]` is True, fast-fails with `alert_thread_invalid()` — no retries against a known-broken thread.
3. Enters a **max-3-attempt loop** (`TN_MAX_ATTEMPTS`) with back-off `(10 s, 30 s)` before attempts 2 and 3.
4. Each attempt calls `_tn_call_once(prompt, tid, timeout_read)`.
5. Classifies the outcome via `_classify_failure(status, err, sse_error, exc, text, completed)` into one of: `ok`, `auth`, `http_error`, `sse_error`, `invalid_request`, `empty_200`, `incomplete_preamble`, `timeout`, `exception`.
6. Recovery action depends on the tag (see [Error handling taxonomy](#error-handling-taxonomy)).
7. On success, returns `dedupe_tn_text(sanitize_tn_text(text))` — sanitization at the choke point means every downstream consumer gets clean text.

### 4. `_tn_call_once` — the HTTP + SSE layer

```
POST https://api.adventai.io/api/discovery-agents/sse/v2/streams
Authorization: Bearer <access_token>
Content-Type: application/json
Accept: text/event-stream
```

Body:

```json
{"query": "<prompt>", "thread_id": "<uuid>"}
```

Reads the stream line by line:

- `event: <name>` lines update the current event type.
- `data: <json>` lines get JSON-parsed:
  - `event_type == "llm_output"` + `data.content` → append to chunks buffer.
  - `event_type == "error"` → store the payload as `err_snippet`, set `sse_error=True`.
  - `event_type in ("done", "complete", "end")` or sentinel `[DONE]` → set `completed=True`.
  - Other `detail` fields → only flagged as error if they contain error-marker words (`error`, `denied`, `expired`, `unauthorized`, `invalid`).

After the loop, chunks are joined via `stitch_sse_chunks()` which detects and drops overlapping tail/head repetitions (TN occasionally re-emits preamble). Returns `(text, status, err_snippet, sse_error, completed)`.

### 5. Back in `query_truenorth`, on `ok`:

```python
tn_state["last_success_at"] = datetime.now(IST)
cleaned = dedupe_tn_text(sanitize_tn_text(text))
return cleaned
```

`sanitize_tn_text()` strips `<Token>` (replaces with `$TICKER`), `<Anchor>` (drops), `<sp>` Claude-tool-use markup (drops wholesale), generic paired XML (unwraps), orphan openers / closers (drops), and collapses leftover whitespace.

`dedupe_tn_text()` runs two passes: `dedupe_repeated_substrings()` collapses adjacent ≥30-char duplicates, then `dedupe_contiguous_lines()` drops any line that immediately repeats the previous non-blank line.

### 6. `manual_trades` parses the setups

```python
trades = parse_trades_from_text(result)
```

`parse_trades_from_text` splits the response on `---` rules and new `$TICKER` markers, then for each block:

1. Captures the ticker / direction / conviction from the header line.
2. Walks each line. Pipe-table rows (`| Entry | $110.50 |`) are split and mapped to `entry` / `sl` / `tp` / `rr` fields.
3. Bulleted lines (prefix `- * •`) become context notes, with two filters: the ticker-header bullet and any bullet starting with "Entry" are skipped.
4. Fallback regex patterns catch inline forms (`Entry: $110.50`) that aren't in pipe tables.
5. Each price field is run through `_clean_price()` — strips any leading `$` so the display layer can re-add it exactly once.

### 7. `build_trade_embeds` produces Discord embeds

One embed per setup with colour (green/red by direction), emoji by rank (🥇 🥈 🥉), and inline fields: Entry / Stop-Loss / Take-Profit / R:R (via `_fmt_price` which guarantees exactly one `$` prefix). Context bullets join with `•` separators. Falls back to a "no structured trades extracted" code-block embed if parsing returned nothing.

### 8. Post to Discord

For each embed, `await ctx.send(embed=e)`. If `extract_risk_flag` finds a risk section, also post a yellow risk embed.

**Total round-trip on a warm path**: 10–120 s, dominated by TN's SSE stream duration. On an invalid thread: ~5 s to fast-fail via the empty-200 short-circuit.

---

## Scheduled brief flow

### Trigger

`APScheduler` runs 13 cron jobs on the `Asia/Kolkata` timezone:

- 12 session briefs — (asia, london, us) × (pre-open, post-open, pre-close, post-close)
- 1 daily regime outlook at 05:00 IST

Plus 2 utility jobs:

- Token refresh every 8 h (`CronTrigger(hour="*/8")`)
- Cookie harvester every 20 h (`IntervalTrigger(hours=20)`) — only when `TN_SESSION_COOKIES` is set

The exact times live in `SCHEDULE = [...]` in `bot.py`. Changing them requires a code edit + redeploy.

### `run_session_brief(session, phase)` channel routing

1. Looks up the session channel (`CH["asia"]` / `CH["london"]` / `CH["us"]`) and `CH["trades"]`.
2. Builds the session-specific prompt.
3. Calls `query_truenorth(prompt)` — no per-call thread_id, so the shared thread is used.
4. Builds the **analysis embed** via `build_brief_embed()` which:
   - Trims off the trade-setups section (from the first `$TICKER | LONG/SHORT` marker)
   - Trims off the risk-flag section
   - Runs `dedupe_tn_text()` on what's left
   - Promotes the first prose paragraph to a "Market Regime" inline field
   - Wraps markdown tables in code fences
   - Truncates at the latest sentence boundary below 4 000 chars, skipping any cut that would leave a dangling numbered-section header
   - Overrides any stale date (TrueNorth sometimes hallucinates "Apr 19, 2025") — the authoritative date lives in the embed title, `📊 {Label} Session — {Phase} · {today}`
5. Posts to the session channel.
6. Parses trades + posts to `#trades` (header + one embed per setup).
7. If a risk flag exists, posts the yellow risk embed to both the session channel and `#trades`.
8. Returns `True` on success.

For `!brief` (manual trigger), `manual_brief` wraps `run_session_brief` and adds a confirmation reply in the invoking channel: `✅ Brief posted to #us-session and #trades`.

---

## Authentication state machine

TrueNorth auth is piggy-backed on Privy. The state machine is:

```
                   ┌──────────────────┐
                   │    Boot start    │
                   └────────┬─────────┘
                            │
                            ▼
              ┌─────────────────────────────┐
              │  _load_initial_tokens()     │
              │                             │
              │  cache fresh (exp > -1h)? ──┼──► use cache (source="cache")
              │       │                     │
              │       ▼ no                  │
              │  use env vars ──────────────┼──► source="env"
              └─────────────────────────────┘
                            │
                            ▼
              ┌─────────────────────────────┐
              │  on_ready: startup refresh  │
              │  refresh_tn_token()         │
              │                             │
              │  try variants in order:     │
              │   1. snake_body             │
              │   2. snake_body+bearer_rt   │
              │   3. camel_body             │
              │   4. bearer_refresh_only    │
              │   5. snake_body+bearer_at   │
              │                             │
              │  first 200 wins, caches     │
              │  variant in _privy_refresh_ │
              │  variant, writes atomic     │
              │  to token_cache.json        │
              └────────┬────────────────────┘
                       │ success
                       ▼
              ┌─────────────────────────────┐
              │  Normal operation           │
              │                             │
              │  Every 8 h: refresh         │
              │  On 401/403: refresh+retry  │
              │  On invalid_request: alert  │
              └─────────────────────────────┘
```

### Key invariants

- **Privy refresh tokens are single-use.** Every successful refresh returns a new one. `_save_token_cache()` MUST be called atomically after the 200 parse. If it isn't, the next restart uses the old (invalidated) refresh and the bot dies with `missing_or_invalid_token`.
- **Cache always beats env.** Because env may be stale, cache is always newer (it was written by the last refresh or `!setcreds`).
- **1-hour grace on freshness.** A cached access token whose `exp` is within the last hour is still "fresh" — `refresh_tn_token()` will rotate it in seconds. Tokens older than that are treated as stale and env wins.

### Cache file format (atomic write)

```json
{
  "access": "eyJhbGci...",
  "refresh": "rt_...",
  "updated_at": "2026-04-20T15:30:00+05:30"
}
```

Write path:

1. Serialize to `<path>.tmp`
2. `f.flush()` → `os.fsync()`
3. `os.replace(tmp_path, TOKEN_CACHE_PATH)`

POSIX guarantees `os.replace` is atomic on the same filesystem, so a crash at any point leaves either the prior complete state or the new complete state on disk — never partial JSON.

### Headers sent to Privy

```
privy-app-id: cm6afcumv0688a6x3r78jkx7v
Content-Type: application/json
Accept: application/json
privy-client: react-auth:2.0.0
Origin: https://app.true-north.xyz
Referer: https://app.true-north.xyz/
```

Origin + Referer are load-bearing — without them Privy returns `403 missing_origin`.

---

## Thread-ID state machine

```
          ┌──────────────────┐
          │    Boot start    │
          └────────┬─────────┘
                   │
                   ▼
     ┌──────────────────────────┐
     │ _load_initial_thread()   │
     │                          │
     │ cache file valid UUID? ──┼──► use (source="cache")
     │     │                    │
     │     ▼ no                 │
     │ TN_THREAD_ENV valid UUID?┼──► use (source="env")
     │     │                    │
     │     ▼ no                 │
     │ None                     │
     └────────┬─────────────────┘
              │
              ▼
     ┌──────────────────────────┐
     │ on_ready:                │
     │                          │
     │ if None:                 │
     │   await _try_api_create_ │
     │   thread() — probe 7     │
     │   candidate endpoints    │
     │                          │
     │ if still None:           │
     │   invalidate_thread()    │
     │   post boot warning      │
     │                          │
     │ else:                    │
     │   validate_thread() ping │
     │   (30 s timeout)         │
     │   if ping fails:         │
     │     invalidate_thread()  │
     │     post boot warning    │
     └────────┬─────────────────┘
              │
              ▼
     ┌──────────────────────────┐
     │ Normal operation         │
     │                          │
     │ Runtime empty_200 → also │
     │ invalidates; every       │
     │ TN-dependent command     │
     │ fast-fails until         │
     │ !setcreds restores state │
     └──────────────────────────┘
```

### Why no UUID fallback

Prior versions fell back to `str(uuid.uuid4())` when env + cache + API-create all failed. TrueNorth responds with HTTP 200 + zero chunks to unknown UUIDs, which the retry path treated as "recoverable" and burned ~25 minutes of useless retries. Returning None and explicitly invalidating the thread is far more honest.

---

## Error handling taxonomy

`_classify_failure(status, err, sse_error, exc, text, completed)` is the central dispatcher. It returns one of these tags:

| Tag | Trigger | User-facing copy | Recovery |
|---|---|---|---|
| `ok` | HTTP 200 + non-empty text | — | return cleaned text |
| `auth` | HTTP 401 / 403 OR auth-marker in SSE error body | "Auth failed (HTTP <status>). Run !refreshtoken." | `refresh_tn_token()`, retry once |
| `http_error` | Any other HTTP 4xx / 5xx | "TrueNorth API error: HTTP <status>. <err snippet>" | back-off + retry |
| `sse_error` | Explicit `event_type=error` frame (non-InvalidRequest) | "TrueNorth SSE error: <err>" | back-off + retry |
| `invalid_request` | `error_type=InvalidRequestError` in SSE payload | "Request rejected by TrueNorth: <message>. Thread may be stale — try `!setcreds` with a fresh thread_id." | fast-fail, no retry |
| `empty_200` | HTTP 200 + zero chunks | THREAD_INVALID_HINT (full DevTools recipe) | fast-fail, `invalidate_thread()` |
| `incomplete_preamble` | HTTP 200 + short text matching "Let me scan…" prefixes + no `done` marker | — | retry with `TN_RETRY_READ_TIMEOUT` (360 s) |
| `timeout` | `httpx.ReadTimeout` / `ConnectTimeout` | "TrueNorth timed out after <N>s. Try again." | back-off + retry with longer timeout |
| `exception` | Any other raised exception | `<ExceptionType>: <message>` | back-off + retry |

### Alert rules

`alert_tn_failure(reason, last)` posts a diagnostic embed to `#claude-integration` **only** when the failure is "real":

```python
real_failure = (
    exc is not None
    or (status not in (0, 200))
    or sse_error
)
```

Empty-200 paths emit `alert_thread_invalid()` instead (once per process lifetime). All alerts are rate-limited to one per `ALERT_COOLDOWN_SEC` (3600 s) to prevent spam during sustained outages.

---

## Testing strategy

### What's covered (92+ unit tests)

All tests are in `tests/test_parsers.py`, run with `pytest`. No network access required.

**Parsers**:
- `parse_trades_from_text` across pipe-table and inline-price forms
- `extract_risk_flag` across plural `Risk Flags`, singular `Risk Flag`, with / without horizontal-rule edges
- `decode_jwt_exp` on valid, malformed, invalid-base64, non-JSON payloads

**Classifiers**:
- `_classify_failure` across all 9 failure tags
- `_format_failure_reason` produces the right actionable copy for each tag
- `_parse_sse_error_payload` pulls `error_type` / `error_code` / `error_message` from top-level and nested-under-`data` shapes

**Sanitizers**:
- `sanitize_tn_text` for `<Token>`, `<Anchor>`, `<sp>`, generic paired, orphan openers / closers
- `stitch_sse_chunks` overlap dedup
- `dedupe_contiguous_lines`, `dedupe_repeated_substrings`
- `wrap_markdown_tables` two-or-more-pipe-line detection, leaves single pipes alone

**Cache durability**:
- Atomic crash simulation (`os.replace` raises → canonical file still has prior state)
- Corrupt file deleted on load for both token cache and thread cache
- `_load_initial_tokens` / `_load_initial_thread` precedence (cache > env when fresh / valid UUID)
- `_cache_access_is_fresh` grace window

**Commands**:
- `!setcreds` validation (JWT shape, UUID, owner check, JSON + space-separated forms)
- Privy refresh variants: all 5 shapes listed, access-bearer variant omitted when no access token

**Harvester**:
- `_parse_cookies` handles Cookie-Editor JSON
- `harvest_session` gracefully skips when Playwright isn't installed

### What's NOT covered

- **Live TrueNorth SSE streaming** — impractical without valid creds. Covered indirectly via `_tn_call_once`'s parsing logic being pure-function over mock chunks.
- **Privy refresh actual HTTP** — the variant list is unit-tested, the HTTP call isn't. Checked by hand via `!refreshtoken`.
- **Playwright cookie harvest with a real browser** — the `harvest_session` flow is only tested in the no-Playwright skip path. The browser-launch branch is verified manually on Railway.
- **APScheduler trigger firing** — cron times are tested by calendar, not by pytest.

### Running the suite

```bash
pytest              # fast, ~0.7 s
pytest -v           # verbose per-test output
pytest tests/test_parsers.py::test_stitch_sse_chunks_dedupes_overlapping_tail_head
```

`tests/conftest.py` sets stub env vars so `import bot` succeeds without real credentials. Tests use `monkeypatch` extensively to override module state without mutating the real globals across tests.

---

## Known limitations and technical debt

1. **Per-conversation chat threads share one TrueNorth thread.** `get_tn_thread(conv_id)` used to mint per-Discord-thread UUIDs for chat, but UUIDs aren't valid TrueNorth threads. Until TN ships an API for per-thread creation, every `#claude-integration` chat pollutes one shared thread's context. Not fatal, but not ideal for context isolation.

2. **Privy refresh variants are probed blindly.** No public Privy docs for direct calls; `_privy_refresh_variants()` tries 5 known-likely shapes. If Privy changes the endpoint contract, the bot breaks silently (returns False from every variant). Mitigated by verbose per-variant logging.

3. **Stale-date regex is heuristic.** `_strip_stale_dates()` removes any `Mon DD, YYYY` or ISO date, which will also remove legitimate dates in future content (e.g. "Fed meeting on Dec 17, 2026"). Acceptable today because TN's current content is heavily poisoned by training-data dates.

4. **Cookie harvester is best-effort.** Playwright + headless Chromium is heavy, flaky on cold starts, and needs a manual `pip install playwright && playwright install chromium` on Railway. Optional for a reason.

5. **No per-command rate limiting** except the Sonnet middleman's `RATE_LIMIT_MAX = 3` per user per hour. A malicious user can still burn TN quota by spamming `!trades` directly.

6. **`!winrate` is a placeholder.** No DB, no trade-result tracking. Would need a `trades.db` (sqlite or Postgres) and a price-backfill loop.

7. **`ask_claude` fallback isn't actually wired** into the router today. The only Claude call path is via `run_sonnet_loop` for `!`-prefixed messages. Dead code for now.

8. **Cache files live in the working directory.** Fine on Railway (persistent enough within a container) but would break if the bot runs in a truly stateless environment (e.g. Lambda). A future iteration could use Redis or S3.

9. **Scheduled brief prompt is hard-coded** in `bot.py`. Any KOL forking this will want to tune the prompt for their own community tone; right now that's a source-code change.

10. **No observability beyond Railway stdout.** No Sentry, no metrics, no Prometheus. The observability layer is `!health` + `print()`.

If you're planning a significant rework, start with items 1, 6, and 9 — they're the highest-leverage improvements.
