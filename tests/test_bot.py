"""Unit tests for the v2 bot: TN tool envelope, snapshot, prompts, embeds, schedule."""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import bot


# =============================================================================
# _parse_tool_envelope — the agent-tools /call response contract
# =============================================================================

def _envelope(result, is_error=False, tool="technical_analysis"):
    return {"data": {"toolName": tool, "result": result, "durationMs": 100, "isError": is_error}}


def test_envelope_success_returns_result():
    result = {"status": "success", "market_data": {"current_price": 62594.85}}
    assert bot._parse_tool_envelope(_envelope(result)) == result


def test_envelope_list_result_passes_through():
    result = [{"name": "x"}, {"name": "y"}]
    assert bot._parse_tool_envelope(_envelope(result)) == result


def test_envelope_failed_flag_raises():
    with pytest.raises(bot.TNToolError, match="upstream exploded"):
        bot._parse_tool_envelope({"failed": True, "message": "upstream exploded"})


def test_envelope_success_false_raises():
    with pytest.raises(bot.TNToolError):
        bot._parse_tool_envelope({"success": False, "message": "nope"})


def test_envelope_is_error_raises():
    with pytest.raises(bot.TNToolError, match="tool error"):
        bot._parse_tool_envelope(_envelope({"detail": "boom"}, is_error=True))


def test_envelope_non_dict_raises():
    with pytest.raises(bot.TNToolError):
        bot._parse_tool_envelope(["not", "a", "dict"])
    with pytest.raises(bot.TNToolError):
        bot._parse_tool_envelope(None)


def test_envelope_missing_data_raises():
    with pytest.raises(bot.TNToolError, match="missing 'data'"):
        bot._parse_tool_envelope({"something": "else"})


def test_envelope_app_only_raises_with_flag():
    """App-gated tools return success + status=app_only; callers must be able
    to distinguish that from a real failure (it's expected without auth)."""
    result = {"status": "app_only", "tool": "market_index_price",
              "message": "This capability is available in the TrueNorth app."}
    with pytest.raises(bot.TNToolError) as exc_info:
        bot._parse_tool_envelope(_envelope(result, tool="market_index_price"))
    assert exc_info.value.app_only is True


def test_envelope_normal_failure_has_app_only_false():
    with pytest.raises(bot.TNToolError) as exc_info:
        bot._parse_tool_envelope({"failed": True, "message": "x"})
    assert exc_info.value.app_only is False


# =============================================================================
# compact_json — prompt budget enforcement
# =============================================================================

def test_compact_json_under_limit_unchanged():
    obj = {"a": 1, "b": [1, 2, 3]}
    out = bot.compact_json(obj, 1000)
    assert json.loads(out) == obj


def test_compact_json_over_limit_truncates_with_marker():
    obj = {"k": "x" * 500}
    out = bot.compact_json(obj, 100)
    assert len(out) <= 100 + len("…[truncated]")
    assert out.endswith("…[truncated]")


def test_compact_json_none_is_unavailable():
    assert bot.compact_json(None, 100) == "(unavailable)"


def test_compact_json_non_serializable_falls_back_to_str():
    out = bot.compact_json({"dt": datetime(2026, 6, 12)}, 1000)
    assert "2026-06-12" in out


def test_compact_json_uses_compact_separators():
    assert bot.compact_json({"a": 1, "b": 2}, 100) == '{"a":1,"b":2}'


# =============================================================================
# Source tables + key→tool mapping
# =============================================================================

def test_brief_sources_cover_core_tools():
    tools = {bot._source_tool_name(k) for k, _a, _b in bot.BRIEF_SOURCES}
    assert {"basic_market_info", "technical_analysis", "derivatives_analysis",
            "performance_scanner", "events"} <= tools


def test_source_tool_name_strips_dedup_suffixes():
    assert bot._source_tool_name("technical_analysis_eth") == "technical_analysis"
    assert bot._source_tool_name("events_macro") == "events"
    assert bot._source_tool_name("performance_scanner_7d") == "performance_scanner"
    assert bot._source_tool_name("derivatives_analysis") == "derivatives_analysis"


def test_brief_source_keys_are_unique():
    keys = [k for k, _a, _b in bot.BRIEF_SOURCES]
    assert len(keys) == len(set(keys))
    regime_keys = keys + [k for k, _a, _b in bot.REGIME_EXTRA_SOURCES]
    assert len(regime_keys) == len(set(regime_keys))


def test_regime_extra_sources_include_indices_and_macro_events():
    tools = {bot._source_tool_name(k) for k, _a, _b in bot.REGIME_EXTRA_SOURCES}
    assert "market_index_price" in tools
    assert "events" in tools


def test_source_budgets_are_positive_and_bounded():
    for key, _args, budget in bot.BRIEF_SOURCES + bot.REGIME_EXTRA_SOURCES:
        assert 0 < budget <= 10000, f"{key} budget {budget} out of range"


# =============================================================================
# gather_snapshot
# =============================================================================

def test_gather_snapshot_tolerates_partial_failure(monkeypatch):
    """One source failing must not poison the rest of the snapshot."""
    import asyncio

    async def fake_safe(tool, args, **kw):
        if tool == "derivatives_analysis":
            return None
        return {"tool": tool, "ok": True}

    monkeypatch.setattr(bot, "tn_call_safe", fake_safe)
    snapshot = asyncio.run(bot.gather_snapshot(bot.BRIEF_SOURCES))
    assert snapshot["derivatives_analysis"] == "(unavailable)"
    assert "basic_market_info" in snapshot
    assert "(unavailable)" not in snapshot["basic_market_info"]
    assert set(snapshot) == {k for k, _a, _b in bot.BRIEF_SOURCES}


def test_gather_snapshot_applies_budgets(monkeypatch):
    import asyncio

    async def fake_safe(tool, args, **kw):
        return {"blob": "y" * 50000}

    monkeypatch.setattr(bot, "tn_call_safe", fake_safe)
    snapshot = asyncio.run(bot.gather_snapshot(bot.BRIEF_SOURCES))
    for (key, _args, budget) in bot.BRIEF_SOURCES:
        assert len(snapshot[key]) <= budget + len("…[truncated]")


# =============================================================================
# Prompt builders
# =============================================================================

SNAPSHOT = {
    "basic_market_info": '{"market_data":{"current_price":62594.85}}',
    "technical_analysis": '{"rsi14":{"value":56.24}}',
    "derivatives_analysis": "(unavailable)",
}


def test_brief_prompt_names_the_session():
    for session, marker in (("asia", "Tokyo"), ("london", "LSE"), ("us", "NYSE")):
        prompt = bot.build_brief_prompt(session, SNAPSHOT)
        assert marker in prompt


def test_brief_prompt_includes_snapshot_sections():
    prompt = bot.build_brief_prompt("asia", SNAPSHOT)
    assert "62594.85" in prompt
    assert "### technical_analysis" in prompt
    assert "(unavailable)" in prompt  # missing sections are visible, not hidden


def test_brief_prompt_has_utc_timestamp():
    prompt = bot.build_brief_prompt("us", SNAPSHOT)
    assert "UTC" in prompt
    assert str(datetime.now(ZoneInfo("UTC")).year) in prompt


def test_regime_prompt_includes_snapshot_and_structure():
    prompt = bot.build_regime_prompt(SNAPSHOT)
    assert "62594.85" in prompt
    assert "Regime" in prompt
    assert "Catalysts" in prompt


def test_synth_system_prohibits_invention_and_tables():
    assert "Never invent" in bot.SYNTH_SYSTEM
    assert "NO markdown tables" in bot.SYNTH_SYSTEM


# =============================================================================
# response_text — Claude content block extraction
# =============================================================================

class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


def test_response_text_joins_text_blocks_skips_thinking():
    blocks = [
        _Block("thinking", thinking="hmm"),
        _Block("text", text="**Regime** risk-off. "),
        _Block("text", text="BTC at 62.5k."),
    ]
    assert bot.response_text(blocks) == "**Regime** risk-off. BTC at 62.5k."


def test_response_text_empty_when_no_text_blocks():
    assert bot.response_text([_Block("thinking", thinking="x")]) == ""
    assert bot.response_text([]) == ""


# =============================================================================
# Truncation helpers (ported from v1 — behavior unchanged)
# =============================================================================

def test_truncate_at_sentence_prefers_sentence_boundary():
    text = "First sentence. Second sentence. Third sentence."
    out = bot._truncate_at_sentence(text, 20)
    assert out.endswith("…")
    body = out[:-1].rstrip()
    assert body and body[-1] in ".!?"


def test_truncate_short_text_unchanged():
    assert bot._truncate_at_sentence("Short.", 100) == "Short."


def test_truncate_does_not_leave_dangling_section_header():
    text = (
        "1. Macro: dovish tone holding.\n"
        "2. BTC: reclaimed weekly VWAP at 67k.\n"
        "3. ETH: lagging the majors.\n"
        "4. SOL: relative strength.\n"
        "5. Risks ahead — multiple catalysts queued.\n"
    )
    import re
    truncate_at = text.index("5.") + 3
    out = bot._truncate_at_sentence(text, truncate_at)
    last_line = out.rstrip("…").rstrip().rsplit("\n", 1)[-1].strip()
    assert not re.match(r'^\d+\.+\s*$', last_line), f"dangling header in {out!r}"


def test_strip_dangling_header_removes_orphan_number_lines():
    assert bot._strip_dangling_header("body.\n5.") == "body."
    assert bot._strip_dangling_header("body.\n5....") == "body."
    assert bot._strip_dangling_header("body.") == "body."
    assert bot._strip_dangling_header("body.\n5. body") == "body.\n5. body"


# =============================================================================
# Embed builders
# =============================================================================

def test_brief_embed_title_has_session_and_date():
    e = bot.build_brief_embed("**Regime** risk-off.", "asia")
    assert "Asia Session" in e.title
    assert datetime.now(bot.IST).strftime("%b %d, %Y") in e.title
    assert e.color.value == bot.COLOR_ASIA


def test_brief_embed_description_capped_at_4096():
    e = bot.build_brief_embed("Long sentence here. " * 500, "us")
    assert len(e.description) <= 4096


def test_brief_embed_dashboard_field_present_when_url_set(monkeypatch):
    monkeypatch.setattr(bot, "DASHBOARD_URL", "https://corgi.example.com")
    e = bot.build_brief_embed("text", "london")
    fields = {f.name: f.value for f in e.fields}
    assert "🎯 Top Setups" in fields
    assert "https://corgi.example.com" in fields["🎯 Top Setups"]


def test_brief_embed_no_dashboard_field_when_unset(monkeypatch):
    monkeypatch.setattr(bot, "DASHBOARD_URL", "")
    e = bot.build_brief_embed("text", "london")
    assert all(f.name != "🎯 Top Setups" for f in e.fields)


def test_regime_embed_shape():
    e = bot.build_regime_embed("**Regime** neutral.")
    assert "Regime Outlook" in e.title
    assert e.color.value == bot.COLOR_REGIME


def test_failure_embed_truncates_reason():
    e = bot._failure_embed("title", "x" * 5000)
    assert len(e.description) <= 1600
    assert e.description.startswith("⚠️")


# =============================================================================
# Schedule — "15 minutes before each market's local open"
# =============================================================================

def test_schedule_has_exactly_three_sessions():
    assert len(bot.SCHEDULE) == 3
    assert [s for s, *_ in bot.SCHEDULE] == ["asia", "london", "us"]


def test_schedule_times_are_15_min_before_market_open():
    expected = {
        "asia":   ("Asia/Tokyo", 8, 45),        # Tokyo 09:00 JST − 15m
        "london": ("Europe/London", 7, 45),     # LSE 08:00 local − 15m
        "us":     ("America/New_York", 9, 15),  # NYSE 09:30 ET − 15m
    }
    for session, tz, hour, minute in bot.SCHEDULE:
        assert (tz, hour, minute) == expected[session]


def test_schedule_timezones_are_valid():
    for _session, tz, _h, _m in bot.SCHEDULE:
        ZoneInfo(tz)  # raises if invalid


def test_schedule_sessions_have_channels():
    for session, *_ in bot.SCHEDULE:
        assert session in bot.CH


def test_setup_scheduler_registers_four_jobs():
    bot.setup_scheduler()
    job_ids = {j.id for j in bot.scheduler.get_jobs()}
    assert job_ids == {"asia_pre_open", "london_pre_open", "us_pre_open", "regime_mwf"}
    # Re-running must not introduce new job ids. (Exact-count idempotency only
    # holds on a *running* scheduler — replace_existing dedupes at start() —
    # and on_ready always starts the scheduler right after the first setup.)
    bot.setup_scheduler()
    assert {j.id for j in bot.scheduler.get_jobs()} == job_ids


def test_regime_job_is_mwf_0600_ist():
    bot.setup_scheduler()
    job = bot.scheduler.get_job("regime_mwf")
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["day_of_week"] == "mon,wed,fri"
    assert fields["hour"] == "6"
    assert fields["minute"] == "0"


# =============================================================================
# tn_call retry/error behavior (httpx mocked via transport)
# =============================================================================

def _mock_transport(responses):
    """httpx.MockTransport cycling through canned responses."""
    calls = {"n": 0}

    def handler(request):
        resp = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        return resp

    return httpx_transport(handler), calls


def httpx_transport(handler):
    import httpx
    return httpx.MockTransport(handler)


def test_tn_call_retries_5xx_then_succeeds(monkeypatch):
    import asyncio
    import httpx

    ok_body = json.dumps(_envelope({"status": "success", "v": 1}))
    transport, calls = _mock_transport([
        httpx.Response(503, text="overloaded"),
        httpx.Response(200, text=ok_body, headers={"Content-Type": "application/json"}),
    ])

    real_client = httpx.AsyncClient

    def patched_client(**kw):
        kw["transport"] = transport
        return real_client(**kw)

    monkeypatch.setattr(bot.httpx, "AsyncClient", patched_client)
    monkeypatch.setattr(bot, "TN_CALL_BACKOFF_S", 0.0)
    result = asyncio.run(bot.tn_call("technical_analysis", {"token_address": "bitcoin"}))
    assert result == {"status": "success", "v": 1}
    assert calls["n"] == 2


def test_tn_call_4xx_fails_fast_no_retry(monkeypatch):
    import asyncio
    import httpx

    transport, calls = _mock_transport([httpx.Response(400, text="bad args")])
    real_client = httpx.AsyncClient

    def patched_client(**kw):
        kw["transport"] = transport
        return real_client(**kw)

    monkeypatch.setattr(bot.httpx, "AsyncClient", patched_client)
    monkeypatch.setattr(bot, "TN_CALL_BACKOFF_S", 0.0)
    with pytest.raises(bot.TNToolError, match="HTTP 400"):
        asyncio.run(bot.tn_call("technical_analysis", {}))
    assert calls["n"] == 1


def test_tn_call_records_success_state(monkeypatch):
    import asyncio
    import httpx

    ok_body = json.dumps(_envelope({"status": "success"}))
    transport, _calls = _mock_transport([
        httpx.Response(200, text=ok_body, headers={"Content-Type": "application/json"}),
    ])
    real_client = httpx.AsyncClient

    def patched_client(**kw):
        kw["transport"] = transport
        return real_client(**kw)

    monkeypatch.setattr(bot.httpx, "AsyncClient", patched_client)
    before = datetime.now(bot.IST) - timedelta(seconds=1)
    asyncio.run(bot.tn_call("events", {"query": "crypto"}))
    assert bot.tn_state["last_tool_success_at"] >= before


def test_tn_call_sends_auth_header_when_token_set(monkeypatch):
    import asyncio
    import httpx

    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200, text=json.dumps(_envelope({"status": "success"})),
            headers={"Content-Type": "application/json"},
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched_client(**kw):
        kw["transport"] = transport
        return real_client(**kw)

    monkeypatch.setattr(bot.httpx, "AsyncClient", patched_client)
    monkeypatch.setattr(bot, "TN_AUTH_TOKEN", "raw-token-value")
    asyncio.run(bot.tn_call("events", {"query": "x"}))
    assert seen["auth"] == "raw-token-value"

    monkeypatch.setattr(bot, "TN_AUTH_TOKEN", "")
    asyncio.run(bot.tn_call("events", {"query": "x"}))
    assert seen["auth"] is None


def test_tn_call_safe_returns_none_and_records_error(monkeypatch):
    import asyncio

    async def boom(tool, args, **kw):
        raise bot.TNToolError("synthetic failure")

    monkeypatch.setattr(bot, "tn_call", boom)
    bot.tn_state["last_tool_error"] = None
    result = asyncio.run(bot.tn_call_safe("derivatives_analysis", {}))
    assert result is None
    assert "synthetic failure" in bot.tn_state["last_tool_error"]


def test_tn_call_safe_app_only_does_not_pollute_error_state(monkeypatch):
    """app_only is expected (no auth token) — it must not show up as an error in !health."""
    import asyncio

    async def gated(tool, args, **kw):
        raise bot.TNToolError("market_index_price is app-gated", app_only=True)

    monkeypatch.setattr(bot, "tn_call", gated)
    bot.tn_state["last_tool_error"] = None
    result = asyncio.run(bot.tn_call_safe("market_index_price", {"index": "all"}))
    assert result is None
    assert bot.tn_state["last_tool_error"] is None


# =============================================================================
# Health helpers + config
# =============================================================================

def test_format_delta_buckets():
    now = datetime.now(bot.IST)
    assert bot._format_delta(None) == "—"
    assert bot._format_delta(now - timedelta(seconds=30)).endswith("s ago")
    assert bot._format_delta(now - timedelta(minutes=5)).endswith("m ago")
    assert bot._format_delta(now - timedelta(hours=3)).endswith("m ago")  # "3h 0m ago"
    assert bot._format_delta(now - timedelta(days=2)).endswith("d ago")


def test_tn_tools_base_has_no_trailing_slash():
    assert not bot.TN_TOOLS_BASE.endswith("/")


def test_channels_required_set():
    assert set(bot.CH) == {"asia", "london", "us", "regime"}


def test_synth_model_default():
    assert bot.SYNTH_MODEL == "claude-opus-4-8"
