# Architecture (v2)

Technical walkthrough of `dct-truenorth-bot` for anyone modifying the code. For the operator overview see [README.md](README.md). For the v1 architecture this replaced (Privy auth, SSE agent, Mac harvester), see git history ≤ `2492ede` and HANDOFF.md.

## File layout

| File | Purpose |
|---|---|
| `bot.py` | Everything runtime: TN tool client, snapshot gathering, Claude synthesis, embeds, scheduler, commands, `/healthz`. Single file by choice. |
| `tests/test_bot.py` | Full unit suite (54 tests, no network). |
| `tests/conftest.py` | Stubs env vars so `import bot` works in CI. |
| `Procfile` / `runtime.txt` / `requirements.txt` | Railway build: Python 3.11.8, `web: python bot.py`. |

## Request flow: one scheduled brief

1. **Cron fires** in the market's local timezone (`SCHEDULE` in bot.py) — e.g. `Asia/Tokyo 08:45`.
2. **`run_session_brief(session)`** calls `gather_snapshot(BRIEF_SOURCES)`: six TN tools fetched in parallel via `asyncio.gather`, each `tn_call_safe` (failures → `None` → `"(unavailable)"` in the snapshot, never an exception).
3. **`tn_call`** POSTs `{"toolName", "arguments"}` to `https://api.adventai.io/api/agent-tools/call`. Keyless; optional raw `Authorization` header from `TN_AUTH_TOKEN`. One retry on 5xx/network errors (5 s backoff), fast-fail on 4xx. `_parse_tool_envelope` unwraps `{data: {result, isError}}` and raises `TNToolError(app_only=True)` for app-gated stubs so they're never fed to Claude as data.
4. **Per-source char budgets** (`compact_json`) cap each tool's JSON in the prompt — a full brief prompt is ~6–7k tokens.
5. **`synthesize`** makes one `claude-opus-4-8` call (adaptive thinking, `max_tokens=16000`) with `SYNTH_SYSTEM` enforcing: snapshot numbers only, no invented data, no tables, < 3300 chars, trader-native tone.
6. **`build_brief_embed`** wraps the text (sentence-aware truncation at 4000 chars, dangling-header stripping) and appends the `DASHBOARD_URL` field.
7. **Failure handling:** zero sources available → failure embed in the session channel + `alert_ops`; synthesis exception → same. Partial sources → brief posts anyway.

The regime job (`regime_mwf`, Mon/Wed/Fri 06:00 IST) runs the same pipeline with three extra sources (indices, 7-day macro events, 7-day scanner) and a macro-shaped prompt.

## Why these choices

- **Agent-tools REST, not the old SSE agent** — TrueNorth v2 broke `/sse/v2/streams`; the TN team's interim integration path is the CLI, and the CLI is a thin wrapper over this REST API. Calling it directly from Python avoids shipping Node on Railway.
- **Claude as synthesis layer** — the tools return raw indicator/derivatives JSON, not narrative. One Opus call per brief (~24/wk scheduled) turns the snapshot into the trader-facing text. Costs cents per week.
- **Keyless by default** — the crypto tools (and empirically the index tool) need no auth. `TN_AUTH_TOKEN` is plumbed through for whenever TN issues real tokens, but nothing depends on it.
- **Per-market timezone crons** — "15 minutes before open" is only stable in the market's own timezone; an IST-fixed cron would drift an hour across UK/US DST transitions.
- **No persistent state** — v1's atomic token/thread caches existed for single-use Privy refresh tokens. v2 has no credentials, so the bot is fully stateless; a restart loses only in-process observability counters (`tn_state`).

## Error handling

| Failure | Behavior |
|---|---|
| One TN tool fails | Logged; snapshot marks it `(unavailable)`; brief still posts |
| TN tool 5xx / network | One retry after 5 s, then treated as failed |
| App-gated tool, no token | Skipped silently (expected, not an error in `!health`) |
| All TN tools fail | Failure embed in target channel + `CH_OPS` alert |
| Claude call fails / empty | Failure embed + `CH_OPS` alert; error in `!health` |
| Discord channel missing | Logged, job returns False |

## Testing

`pytest` — 54 tests, < 1 s, no network. Coverage: envelope contract (success/failed/isError/app_only), `compact_json` budgets, snapshot partial-failure tolerance, prompt builders, `response_text` block filtering, truncation helpers, embed shapes, schedule times + timezones + job registration, `tn_call` retry semantics via `httpx.MockTransport`, health helpers.

Not covered (verified by hand): live TN API responses, live Claude output quality, APScheduler firing, Discord I/O.
