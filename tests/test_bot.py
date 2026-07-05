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
# Source tables + gather_raw
# =============================================================================

def test_brief_sources_cover_core_tools():
    tools = {tool for _k, tool, _a in bot.BRIEF_SOURCES}
    assert {"basic_market_info", "technical_analysis", "derivatives_analysis",
            "performance_scanner", "events"} <= tools


def test_brief_source_keys_are_unique():
    keys = [k for k, _t, _a in bot.BRIEF_SOURCES]
    assert len(keys) == len(set(keys))
    regime_keys = keys + [k for k, _t, _a in bot.REGIME_EXTRA_SOURCES]
    assert len(regime_keys) == len(set(regime_keys))


def test_regime_extra_sources_include_indices_and_macro_events():
    tools = {tool for _k, tool, _a in bot.REGIME_EXTRA_SOURCES}
    assert "market_index_price" in tools
    assert "events" in tools


def test_brief_ta_sources_are_daily():
    # Regime/levels need true daily MAs — the 4h,1d combined TA is ambiguous.
    for key, tool, args in bot.BRIEF_SOURCES:
        if tool == "technical_analysis":
            assert args.get("timeframe") == "1d", key


def test_gather_raw_returns_raw_dicts_and_tolerates_failure(monkeypatch):
    import asyncio

    async def fake_safe(tool, args, **kw):
        if tool == "derivatives_analysis":
            return None
        return {"tool": tool, "ok": True}

    monkeypatch.setattr(bot, "tn_call_safe", fake_safe)
    d = asyncio.run(bot.gather_raw(bot.BRIEF_SOURCES))
    assert d["derivs"] is None
    assert d["info_btc"] == {"tool": "basic_market_info", "ok": True}
    assert set(d) == {k for k, _t, _a in bot.BRIEF_SOURCES}


# =============================================================================
# Deterministic brief engine — fixtures mirror live TN response shapes
# =============================================================================

def _ta_full(price_rel="price_below", rsi=33.8, sma20=62941.02, sma50=69453.55):
    return {
        "technical_indicators": {
            "rsi14": {"value": rsi, "state": "neutral", "momentum": "rising"},
            "macd_12_26_9": {"dif": -1292.8, "dea": -1873.3, "hist": 580.4, "state": "bull", "momentum": "rising"},
            "sma20": {"value": sma20, "slope": "down", "state": price_rel},
            "sma50": {"value": sma50, "slope": "down", "state": price_rel},
            "boll_20_2": {"upper": 65946.1, "mid": 61994.3, "lower": 58042.5, "pb": 0.576,
                          "mid_relation": "above_mid"},
            "atr14": {"value": 2111.73, "atr_pct": 0.0337, "state": "normal"},
            "volume": {"value": 50446.0, "ma20": 168638.4, "vs_ma20": -0.7009, "state": "low"},
        },
        "support_resistance": {
            "support and resistance channel": {
                "channels": [
                    {"hi": 62401.7, "lo": 59080.0, "strength": 84},
                    {"hi": 67255.4, "lo": 64918.2, "strength": 73},
                    {"hi": 97932.1, "lo": 94555.0, "strength": 51},
                ],
                "signals": {"in_channel": False},
            },
            "recent_high_low": {"calendar": {"high_24h": 63114.9, "low_24h": 62410.1,
                                             "high_7d": 63450.0, "low_7d": 57758.6}},
        },
    }


def _info(price=62594.85, d1=0.62, d7=-2.0, d30=-22.1):
    return {"market_data": {"current_price": price, "price_change_percentage_24h": d1,
                            "price_change_percentage_7d": d7, "price_change_percentage_30d": d30}}


def _derivs_full(funding=0.003, pctile=33.9):
    return {"derivative_data": {"BTC": {
        "Aggregated open interest": {
            "current_open_interest": 6228606505.0,
            "rolling_changes": {"oi_change_1d_abs": -315477047.0},
            "percentile_analysis": {"current_oi_percentile_7d": 31.8},
        },
        "1h Aggregated OI weighted funding rate": {
            "current_funding_rate_in_percentage": funding,
            "current_funding_percentile_7d": pctile,
        },
        "Binance/Bybit/OKX aggreated liquidation map": {
            "price_now": 63252.0,
            "max_liquidation_points": {
                "max_short_liquidation_point": [{"price": 64130.0, "liq_usd": 148229387, "distance_pct": 1.39}],
                "max_long_liquidation_point": [{"price": 61200.0, "liq_usd": 118000000, "distance_pct": -2.2}],
            },
        },
    }}}


def _scanner_raw():
    return {"leaderboard": [
        {"rank": 1, "ticker": "CRVUSDT", "momentum1D": -4.6, "momentum7D": 34.1, "rsVsBenchmark": 30.4},
        {"rank": 2, "ticker": "TRUMPUSDT", "momentum1D": 19.5, "momentum7D": 30.3, "rsVsBenchmark": 26.6},
    ]}


def _events_raw():
    return {"results": [
        {"title": "Content Not Related to Crypto: something political"},
        {"title": "SEC approves spot altcoin ETF listings"},
        {"title": "Content is not crypto-related; cannot generate a title."},
        {"title": "Fed minutes flag slower balance-sheet runoff"},
    ]}


def _brief_data(**over):
    d = {
        "info_btc": _info(),
        "ta_btc": _ta_full(),
        "info_eth": _info(price=1603.7, d1=-0.7, d7=-3.2, d30=-25.0),
        "ta_eth": _ta_full(price_rel="price_below", rsi=41.0, sma20=1700.0, sma50=1900.0),
        "derivs": _derivs_full(),
        "scanner": _scanner_raw(),
        "events": _events_raw(),
    }
    d.update(over)
    return d


def test_nearest_levels_picks_adjacent_channels():
    sup, res = bot._nearest_levels(_ta_full(), 62594.85)
    assert "59,080" in sup and "62,402" in sup and "str 84" in sup
    assert "64,918" in res and "67,255" in res and "str 73" in res


def test_nearest_levels_handles_missing():
    assert bot._nearest_levels(None, 100.0) == (None, None)
    assert bot._nearest_levels(_ta_full(), None) == (None, None)


def test_clean_event_titles_filters_junk():
    titles = bot.clean_event_titles(_events_raw())
    assert titles == ["SEC approves spot altcoin ETF listings",
                      "Fed minutes flag slower balance-sheet runoff"]
    assert bot.clean_event_titles(None) == []


def test_trend_line_reads_ma_macd_rsi():
    line = bot._trend_line(_ta_full())
    assert "below 20d ($62,941)" in line and "50d ($69,454)" in line
    assert "MACD bull rising" in line
    assert "RSI 34 rising" in line


def test_positioning_lines_funding_oi_liq():
    lines = bot._positioning_lines(_derivs_full())
    joined = " | ".join(lines)
    assert "Funding +0.0030%" in joined and "34th pctile" in joined
    assert "OI $6.2B" in joined and "-$315M 24h" in joined
    assert "148M shorts @ $64,130 (+1.4%)" in joined
    assert "118M longs @ $61,200 (-2.2%)" in joined


def test_build_rule_brief_has_all_sections():
    text = bot.build_rule_brief("us", _brief_data())
    assert text.startswith("**Regime — ")
    assert "**BTC $62,594.85**" in text or "**BTC $62,595**" in text
    assert "**ETH $1,603.70**" in text or "**ETH $1,604**" in text
    assert "**Positioning**" in text
    assert "**Movers (24h, RS vs BTC)**" in text and "CRV" in text
    assert "**Watch**" in text and "SEC approves" in text
    assert len(text) <= 4096


def test_build_rule_brief_degrades_per_section():
    text = bot.build_rule_brief("asia", _brief_data(events=None, scanner=None, derivs=None,
                                                    info_eth=None, ta_eth=None))
    assert "**BTC" in text
    assert "**Positioning**" not in text
    assert "**Movers" not in text
    assert "**Watch**" not in text
    assert "ETH" not in text


def test_build_rule_brief_raises_without_btc_data():
    with pytest.raises(RuntimeError):
        bot.build_rule_brief("us", _brief_data(info_btc=None, ta_btc=None))


def test_build_rule_brief_uses_only_snapshot_numbers():
    # Every $ level in the output must come from the fixture — no invented data.
    text = bot.build_rule_brief("us", _brief_data())
    assert "$69,454" in text            # sma50 from fixture
    assert "$57,759" in text            # 7d low from fixture
    assert "$2,112" in text             # ATR from fixture


def test_build_rule_regime_outlook_sections():
    d = _brief_data()
    d["indices"] = {"prices": [
        {"index": "gspc", "latest": {"close": 7293.36, "change_percentage": 0.36}},
        {"index": "vix", "latest": {"close": 21.56, "change_percentage": -2.97}},
        {"index": "dxy", "latest": {"close": 100.28, "change_percentage": 0.37}},
    ]}
    d["scanner_7d"] = _scanner_raw()
    d["events_7d"] = _events_raw()
    text = bot.build_rule_regime_outlook(d)
    assert "**Cross-asset**" in text and "SP500 7,293" in text and "VIX 21.56" in text
    assert "**Rotation (7d RS vs BTC)**" in text
    assert "**Catalysts**" in text


def test_build_rule_regime_outlook_raises_when_empty():
    with pytest.raises(RuntimeError):
        bot.build_rule_regime_outlook({})


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


def test_setup_scheduler_registers_jobs():
    bot.setup_scheduler()
    job_ids = {j.id for j in bot.scheduler.get_jobs()}
    assert job_ids == {"asia_pre_open", "london_pre_open", "us_pre_open", "regime_mwf", "setup_tracker"}
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


def test_no_llm_dependency():
    # v3 is deterministic — the module must not import an LLM SDK.
    import sys
    assert "anthropic" not in sys.modules or not hasattr(bot, "claude_client")
    src = open(bot.__file__).read()
    assert "import anthropic" not in src
    assert "api.anthropic.com" not in src


# =============================================================================
# !setup — ticker resolution, JSON parsing, embed, channel gate
# =============================================================================

def test_resolve_token_maps_common_tickers():
    assert bot._resolve_token("BTC") == "bitcoin"
    assert bot._resolve_token("eth") == "ethereum"
    assert bot._resolve_token("  Sol ") == "solana"
    assert bot._resolve_token("CRV") == "curve-dao-token"


def test_resolve_token_passthrough_for_unknown():
    # Unknown tickers / full ids fall through lowercased so the API can try.
    assert bot._resolve_token("bitcoin") == "bitcoin"
    assert bot._resolve_token("curve-dao-token") == "curve-dao-token"
    assert bot._resolve_token("WEIRDCOIN") == "weirdcoin"


def test_ticker_map_values_look_like_coingecko_ids():
    for ticker, tid in bot.TICKER_TO_ID.items():
        assert ticker == ticker.upper(), f"key {ticker} not uppercase"
        assert tid == tid.lower() and " " not in tid, f"bad id {tid}"


def _bullish_ta():
    ta = _ta_full(price_rel="price_above", rsi=62.0)
    return ta


def _bearish_ta():
    ta = _ta_full(price_rel="price_below", rsi=35.0)
    ind = ta["technical_indicators"]
    ind["macd_12_26_9"].update(state="bear", momentum="falling")
    ind["rsi14"]["momentum"] = "falling"
    ind["boll_20_2"]["mid_relation"] = "below_mid"
    return ta


def test_rule_setup_long_on_bullish_alignment():
    s = bot.build_rule_setup("BTC", _info(), _bullish_ta(), _derivs_full())
    assert s["has_setup"] is True and s["direction"] == "LONG"
    assert s["conviction"] in ("High", "Medium")
    assert "1.5R/3R" in s["reasoning"] and "funding" in s["reasoning"]
    # Levels coherent + ATR math: stop = price − 1.5·ATR, TP1 = +1.5R, TP2 = +3R
    entry = bot._parse_zone_midpoint(s["entry_zone"])
    stop = bot._parse_price(s["stop_loss"])
    tp1 = bot._parse_price(s["take_profit_1"])
    tp2 = bot._parse_price(s["take_profit_2"])
    assert stop < entry < tp1 < tp2
    assert abs((entry - stop) - 1.5 * 2111.73) < 2       # risk = 1.5 ATR
    assert abs((tp1 - entry) - 1.5 * (entry - stop)) < 2  # 1.5R
    assert abs((tp2 - entry) - 3.0 * (entry - stop)) < 2  # 3R


def test_rule_setup_short_on_bearish_alignment():
    s = bot.build_rule_setup("ETH", _info(price=2500.0), _bearish_ta(), None)
    assert s["has_setup"] is True and s["direction"] == "SHORT"
    entry = bot._parse_zone_midpoint(s["entry_zone"])
    stop = bot._parse_price(s["stop_loss"])
    tp1 = bot._parse_price(s["take_profit_1"])
    assert tp1 < entry < stop  # SHORT coherence


def test_rule_setup_none_on_mixed_signals():
    # price above MAs (bullish) but MACD bear + RSI weak/falling → |score| < 2.5
    ta = _ta_full(price_rel="price_above", rsi=42.0)
    ind = ta["technical_indicators"]
    ind["macd_12_26_9"].update(state="bear", momentum="falling")
    ind["rsi14"]["momentum"] = "falling"
    ind["boll_20_2"]["mid_relation"] = "below_mid"
    s = bot.build_rule_setup("SOL", _info(price=65.0), ta, None)
    assert s["has_setup"] is False and s["direction"] == "NONE"
    assert "ranging/choppy" in s["reasoning"]
    assert s["conviction"] == "None"


def test_rule_setup_none_without_atr_or_price():
    s = bot.build_rule_setup("BTC", None, _bullish_ta(), None)
    assert s["has_setup"] is False and "unavailable" in s["reasoning"]
    ta = _bullish_ta()
    del ta["technical_indicators"]["atr14"]
    s2 = bot.build_rule_setup("BTC", _info(), ta, None)
    assert s2["has_setup"] is False


def test_rule_setup_output_shape_matches_tracker_contract():
    # log_setup + build_setup_embed consume this dict — keys must all be present.
    s = bot.build_rule_setup("BTC", _info(), _bullish_ta(), _derivs_full())
    assert set(s) == {"has_setup", "direction", "entry_zone", "stop_loss", "take_profit_1",
                      "take_profit_2", "rr_ratio", "conviction", "reasoning"}
    assert all(isinstance(s[k], str) for k in
               ("entry_zone", "stop_loss", "take_profit_1", "take_profit_2", "rr_ratio", "conviction"))


def test_rule_setup_is_trackable_end_to_end(tmp_path, monkeypatch):
    # The generated setup must survive log_setup's parse + coherence guards.
    monkeypatch.setattr(bot, "CACHE_PATH", str(tmp_path))
    monkeypatch.setattr(bot, "SETUPS_PATH", str(tmp_path / "setups.json"))
    monkeypatch.setattr(bot, "_SETUPS", [])
    s = bot.build_rule_setup("BTC", _info(), _bullish_ta(), _derivs_full())
    bot.log_setup(s, "BTC", "bitcoin", _FakeMsg())
    assert len(bot._SETUPS) == 1 and bot._SETUPS[0]["status"] == "OPEN"


LONG_SETUP = {
    "has_setup": True, "direction": "LONG", "entry_zone": "$62,400 – $62,900",
    "stop_loss": "$61,200", "take_profit_1": "$64,500", "take_profit_2": "$66,000",
    "rr_ratio": "2.8", "conviction": "High", "reasoning": "Reclaimed weekly VWAP with rising funding.",
}
SHORT_SETUP = {**LONG_SETUP, "direction": "SHORT"}
NO_SETUP = {
    "has_setup": False, "direction": "NONE", "entry_zone": "—", "stop_loss": "—",
    "take_profit_1": "—", "take_profit_2": "—", "rr_ratio": "—", "conviction": "None",
    "reasoning": "No clear setup — ranging/choppy conditions.",
}


def test_setup_embed_long_is_green_with_levels():
    e = bot.build_setup_embed("BTC", 62594.85, LONG_SETUP)
    assert e.color.value == bot.COLOR_LONG
    assert "BTC Trade Setup" in e.title
    assert "LONG" in e.description and "$62,594.85" in e.description
    names = {f.name for f in e.fields}
    assert {"Entry Zone", "Stop Loss", "Take Profit 1", "Take Profit 2", "R : R", "Conviction"} <= names


def test_setup_embed_short_is_red():
    e = bot.build_setup_embed("ETH", 2500.0, SHORT_SETUP)
    assert e.color.value == bot.COLOR_SHORT
    assert "SHORT" in e.description


def test_setup_embed_no_setup_is_amber_with_no_level_fields():
    e = bot.build_setup_embed("DOGE", 0.1663, NO_SETUP)
    assert e.color.value == bot.COLOR_RISK
    assert len(e.fields) == 0  # no level fields when there's no setup
    assert "ranging/choppy" in e.description
    assert "$0.1663" in e.description  # sub-$1 precision


def test_setup_embed_tolerates_missing_keys():
    # A malformed model response must not crash the embed builder.
    e = bot.build_setup_embed("SOL", 65.0, {"direction": "LONG", "has_setup": True})
    assert e.color.value == bot.COLOR_LONG
    assert any(f.value == "—" for f in e.fields)


def test_setup_embed_footer_flags_not_advice():
    e = bot.build_setup_embed("BTC", 62000.0, NO_SETUP)
    assert "not financial advice" in e.footer.text


def test_fmt_spot_precision():
    assert bot._fmt_spot(62594.85) == "$62,594.85"
    assert bot._fmt_spot(0.1663).startswith("$0.166")


def test_setup_channel_gate_empty_allows_all():
    saved = bot.SETUP_ALLOWED_CHANNELS
    try:
        bot.SETUP_ALLOWED_CHANNELS = set()
        assert bot._setup_channel_allowed(123) is True
        assert bot._setup_channel_allowed(999) is True
    finally:
        bot.SETUP_ALLOWED_CHANNELS = saved


def test_setup_channel_gate_restricts_when_set():
    saved = bot.SETUP_ALLOWED_CHANNELS
    try:
        bot.SETUP_ALLOWED_CHANNELS = {111, 222}
        assert bot._setup_channel_allowed(111) is True
        assert bot._setup_channel_allowed(333) is False
    finally:
        bot.SETUP_ALLOWED_CHANNELS = saved


def test_setup_command_registered_with_cooldown():
    cmd = bot.bot.get_command("setup")
    assert cmd is not None
    bucket = cmd._buckets
    assert bucket._cooldown.per == 60.0
    assert bucket._cooldown.rate == 1


# =============================================================================
# !scan — pure data formatting, no Claude
# =============================================================================

def test_clean_ticker_strips_quote_currency():
    assert bot._clean_ticker("CRVUSDT") == "CRV"
    assert bot._clean_ticker("btcusd") == "BTC"
    assert bot._clean_ticker("ETHPERP") == "ETH"
    assert bot._clean_ticker("AAVE") == "AAVE"
    assert bot._clean_ticker(None) == "?"
    # Must not eat a token that *is* the suffix-length edge.
    assert bot._clean_ticker("USDT") == "USDT"


def test_scan_cell_colors_by_sign():
    pos = bot._scan_cell(25.5, 9)
    neg = bot._scan_cell(-1.2, 9)
    assert "\x1b[32m" in pos and "+25.5%" in pos      # green
    assert "\x1b[31m" in neg and "-1.2%" in neg       # red
    assert pos.endswith("\x1b[0m")                    # reset


def test_scan_cell_none_is_dash_uncolored():
    out = bot._scan_cell(None, 9)
    assert "\x1b[" not in out and "—" in out


def test_scan_cell_padding_is_consistent_after_stripping_codes():
    import re
    for val in (25.5, -1.2, 0.0, 130.7, None):
        cell = bot._scan_cell(val, 9)
        stripped = re.sub(r"\x1b\[[0-9;]*m", "", cell)
        assert len(stripped) == 9, f"{val!r} -> {stripped!r}"


def _fake_scanner(n):
    return {
        "leaderboard": [
            {"rank": i + 1, "ticker": f"TKN{i}USDT", "momentum1D": (i - 2) * 1.5,
             "momentum7D": (10 - i) * 2.0, "rsVsBenchmark": (10 - i) * 3.0}
            for i in range(n)
        ]
    }


def test_scan_embed_title_footer_and_block():
    e = bot.build_scan_embed(_fake_scanner(10), 10)
    assert e.title == "🔍 Market Scanner — Top 10 by Relative Strength"
    assert "Data from TrueNorth | Refreshed" in e.footer.text
    assert "IST" in e.footer.text
    assert e.description.count("```ansi") == 1 and e.description.rstrip().endswith("```")
    assert e.color.value == bot.COLOR_SCAN


def test_scan_embed_respects_count():
    e5 = bot.build_scan_embed(_fake_scanner(20), 5)
    # 5 data rows + 1 header line inside the ansi block
    body = e5.description.split("```ansi\n", 1)[1].rsplit("\n```", 1)[0]
    assert len(body.splitlines()) == 6
    assert "Top 5" in e5.title


def test_scan_embed_handles_empty_leaderboard():
    e = bot.build_scan_embed({"leaderboard": []}, 10)
    assert "Top 0" in e.title  # header only, no crash


def test_scan_embed_tolerates_missing_momentum():
    scanner = {"leaderboard": [{"rank": 1, "ticker": "BTCUSDT",
                                "momentum1D": None, "momentum7D": None, "rsVsBenchmark": None}]}
    e = bot.build_scan_embed(scanner, 10)
    assert "—" in e.description and "BTC" in e.description


def test_scan_command_registered_with_cooldown_and_gate():
    cmd = bot.bot.get_command("scan")
    assert cmd is not None
    assert cmd._buckets._cooldown.per == 60.0
    # Shares the !setup channel gate.
    saved = bot.SETUP_ALLOWED_CHANNELS
    try:
        bot.SETUP_ALLOWED_CHANNELS = {42}
        assert bot._setup_channel_allowed(42) is True
        assert bot._setup_channel_allowed(7) is False
    finally:
        bot.SETUP_ALLOWED_CHANNELS = saved


# =============================================================================
# Typefully auto-draft
# =============================================================================

def test_template_tweet_hot_mover_variant():
    tweet = bot.build_template_tweet("us", _brief_data())
    assert tweet is not None and len(tweet) <= 280
    assert tweet == tweet.lower()               # lowercase, casual
    assert "crv" in tweet                       # hottest RS mover leads
    assert tweet.rstrip().endswith("?")         # ends on a question
    assert "#" not in tweet and "http" not in tweet


def test_template_tweet_funding_variant():
    d = _brief_data(scanner=None, derivs=_derivs_full(funding=0.02, pctile=85.0))
    tweet = bot.build_template_tweet("asia", d)
    assert tweet is not None and "funding" in tweet and "heating up" in tweet
    assert "asia open" in tweet and tweet.rstrip().endswith("?")


def test_template_tweet_regime_fallback_variant():
    d = _brief_data(scanner=None, derivs=_derivs_full(funding=0.003, pctile=35.0))
    tweet = bot.build_template_tweet("london", d)
    assert tweet is not None and "london open" in tweet
    assert "tape" in tweet and tweet.rstrip().endswith("?")


def test_template_tweet_none_without_price():
    assert bot.build_template_tweet("us", {"info_btc": None}) is None


def test_fmt_price_short():
    assert bot._fmt_price_short(96000) == "96k"
    assert bot._fmt_price_short(62400) == "62.4k"
    assert bot._fmt_price_short(1604) == "$1,604"
    assert bot._fmt_price_short(0.19) == "$0.19"


def test_autodraft_state_keys_present():
    assert "drafts_created" in bot.tn_state
    assert "last_draft_error" in bot.tn_state


def test_autodraft_skips_when_disabled_or_empty(monkeypatch):
    import asyncio
    called = {"create": False}

    async def fake_create(_t):
        called["create"] = True
        return "x"

    monkeypatch.setattr(bot, "create_typefully_draft", fake_create)
    monkeypatch.setattr(bot, "AUTO_DRAFT_ENABLED", False)
    monkeypatch.setattr(bot, "TYPEFULLY_API_KEY", "k")
    asyncio.run(bot.maybe_autodraft("a tweet", "US"))
    assert called["create"] is False            # disabled → no call
    monkeypatch.setattr(bot, "AUTO_DRAFT_ENABLED", True)
    asyncio.run(bot.maybe_autodraft(None, "US"))
    assert called["create"] is False            # no tweet built → no call


def test_autodraft_skips_when_key_missing(monkeypatch):
    import asyncio
    called = {"create": False}

    async def fake_create(_t):
        called["create"] = True
        return "x"

    monkeypatch.setattr(bot, "create_typefully_draft", fake_create)
    monkeypatch.setattr(bot, "AUTO_DRAFT_ENABLED", True)
    monkeypatch.setattr(bot, "TYPEFULLY_API_KEY", "")
    asyncio.run(bot.maybe_autodraft("a tweet", "US"))
    assert called["create"] is False


def test_autodraft_creates_and_increments_counter(monkeypatch):
    import asyncio

    async def fake_create(t):
        assert t == "gm tweet"
        return "https://typefully.com/t/abc"

    monkeypatch.setattr(bot, "AUTO_DRAFT_ENABLED", True)
    monkeypatch.setattr(bot, "TYPEFULLY_API_KEY", "k")
    monkeypatch.setattr(bot, "create_typefully_draft", fake_create)
    bot.tn_state["drafts_created"] = 0
    asyncio.run(bot.maybe_autodraft("gm tweet", "US"))
    assert bot.tn_state["drafts_created"] == 1


def _draft_transport(monkeypatch, handler):
    import httpx
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(bot.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))


def test_create_draft_v2_endpoint_auth_and_body(monkeypatch):
    import asyncio, httpx, json as _json
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("Authorization")
        seen["body"] = _json.loads(req.content)
        return httpx.Response(200, json={"id": 123, "share_url": "https://typefully.com/t/xyz"})

    _draft_transport(monkeypatch, handler)
    monkeypatch.setattr(bot, "TYPEFULLY_API_KEY", "mykey")
    monkeypatch.setattr(bot, "TYPEFULLY_SOCIAL_SET_ID", "208154")
    url = asyncio.run(bot.create_typefully_draft("hello world"))
    assert url == "https://typefully.com/t/xyz"
    assert seen["url"] == "https://api.typefully.com/v2/social-sets/208154/drafts"
    assert seen["auth"] == "Bearer mykey"           # v2: plain Authorization Bearer
    assert seen["body"]["platforms"]["x"] == {"enabled": True, "posts": [{"text": "hello world"}]}
    assert seen["body"]["draft_title"] == "Auto-draft from brief"
    assert "publish_at" not in seen["body"]         # stays a draft, never published


def test_create_draft_uses_social_set_env(monkeypatch):
    import asyncio, httpx
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        return httpx.Response(201, json={"id": 9})

    _draft_transport(monkeypatch, handler)
    monkeypatch.setattr(bot, "TYPEFULLY_API_KEY", "k")
    monkeypatch.setattr(bot, "TYPEFULLY_SOCIAL_SET_ID", "999777")
    asyncio.run(bot.create_typefully_draft("hi"))
    assert "/v2/social-sets/999777/drafts" in seen["url"]


def test_create_draft_strips_existing_bearer_prefix(monkeypatch):
    import asyncio, httpx
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, json={"id": 1, "share_url": "u"})

    _draft_transport(monkeypatch, handler)
    monkeypatch.setattr(bot, "TYPEFULLY_API_KEY", "Bearer abc123")  # user already included prefix
    asyncio.run(bot.create_typefully_draft("hi"))
    assert seen["auth"] == "Bearer abc123"          # not double-prefixed


def test_create_draft_403_returns_none_and_records(monkeypatch):
    import asyncio, httpx

    _draft_transport(monkeypatch, lambda req: httpx.Response(403, text="forbidden"))
    monkeypatch.setattr(bot, "TYPEFULLY_API_KEY", "k")
    bot.tn_state["last_draft_error"] = None
    url = asyncio.run(bot.create_typefully_draft("hi"))
    assert url is None
    assert "403" in bot.tn_state["last_draft_error"]   # the v1 failure mode, now handled cleanly


def test_create_draft_server_error_returns_none_and_records(monkeypatch):
    import asyncio, httpx
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    _draft_transport(monkeypatch, handler)
    monkeypatch.setattr(bot, "TYPEFULLY_API_KEY", "mykey")
    bot.tn_state["last_draft_error"] = None
    url = asyncio.run(bot.create_typefully_draft("hi"))
    assert url is None
    assert calls["n"] == 1  # 5xx is not an auth error → no retry
    assert "500" in bot.tn_state["last_draft_error"]


def test_create_draft_network_error_returns_none(monkeypatch):
    import asyncio, httpx

    def handler(req):
        raise httpx.ConnectError("no route")

    _draft_transport(monkeypatch, handler)
    monkeypatch.setattr(bot, "TYPEFULLY_API_KEY", "mykey")
    url = asyncio.run(bot.create_typefully_draft("hi"))
    assert url is None


# =============================================================================
# Setup outcome tracking
# =============================================================================

def test_extract_numbers_and_price_parsing():
    assert bot._extract_numbers("$62,400 – $62,900") == [62400.0, 62900.0]
    assert bot._extract_numbers("$0.1840") == [0.1840]
    assert bot._extract_numbers("—") == []
    assert bot._parse_price("$61,200") == 61200.0
    assert bot._parse_price("nope") is None


def test_parse_zone_midpoint():
    assert bot._parse_zone_midpoint("$62,400 – $62,900") == 62650.0
    assert bot._parse_zone_midpoint("$95,800") == 95800.0   # single value
    assert bot._parse_zone_midpoint("") is None


def test_levels_coherent():
    assert bot._levels_coherent("LONG", 100, 95, 110) is True
    assert bot._levels_coherent("LONG", 100, 110, 90) is False   # stop above entry
    assert bot._levels_coherent("SHORT", 100, 110, 90) is True
    assert bot._levels_coherent("SHORT", 100, 95, 110) is False
    assert bot._levels_coherent("NONE", 100, 95, 110) is False


def test_money_formatting():
    assert bot._money(95800) == "$95,800"
    assert bot._money(65.5) == "$65.50"
    assert bot._money(0.1663).startswith("$0.166")
    assert bot._money(None) == "—"


def test_trade_pct_direction_adjusted():
    long_win = {"entry_price": 95800, "direction": "LONG"}
    assert round(bot._trade_pct(long_win, 99400), 1) == 3.8   # (99400-95800)/95800
    long_loss = {"entry_price": 95800, "direction": "LONG"}
    assert bot._trade_pct(long_loss, 94200) < 0
    short_win = {"entry_price": 2500, "direction": "SHORT"}
    assert bot._trade_pct(short_win, 2400) > 0                # price fell → short profits
    short_loss = {"entry_price": 2500, "direction": "SHORT"}
    assert bot._trade_pct(short_loss, 2600) < 0
    assert bot._trade_pct(long_win, None) is None


def test_evaluate_setup_long():
    s = {"direction": "LONG", "entry_price": 100, "stop_loss": 95, "tp1": 110,
         "timestamp": bot.datetime.now(bot.IST).isoformat()}
    assert bot.evaluate_setup(s, 111) == ("WIN", "TP1")
    assert bot.evaluate_setup(s, 94) == ("LOSS", "SL")
    assert bot.evaluate_setup(s, 102) is None                # mid-range, still open


def test_evaluate_setup_short():
    s = {"direction": "SHORT", "entry_price": 100, "stop_loss": 110, "tp1": 90,
         "timestamp": bot.datetime.now(bot.IST).isoformat()}
    assert bot.evaluate_setup(s, 89) == ("WIN", "TP1")
    assert bot.evaluate_setup(s, 111) == ("LOSS", "SL")
    assert bot.evaluate_setup(s, 100) is None


def test_evaluate_setup_expiry_time_based():
    from datetime import timedelta
    old = (bot.datetime.now(bot.IST) - timedelta(hours=49)).isoformat()
    s = {"direction": "LONG", "entry_price": 100, "stop_loss": 95, "tp1": 110, "timestamp": old}
    assert bot.evaluate_setup(s, 102) == ("EXPIRED", "EXPIRED")   # no trigger, aged out
    # Price trigger still wins over expiry.
    assert bot.evaluate_setup(s, 111) == ("WIN", "TP1")
    # No price + expired ⇒ still expires (price fetch failed).
    assert bot.evaluate_setup(s, None) == ("EXPIRED", "EXPIRED")


def test_evaluate_setup_no_price_not_expired_is_none():
    s = {"direction": "LONG", "entry_price": 100, "stop_loss": 95, "tp1": 110,
         "timestamp": bot.datetime.now(bot.IST).isoformat()}
    assert bot.evaluate_setup(s, None) is None


def _resolved(status, **kw):
    base = {"ticker": "BTC", "direction": "LONG", "entry_price": 95800.0,
            "stop_loss": 94200.0, "tp1": 99400.0, "tp2": 101000.0,
            "timestamp": bot.datetime.now(bot.IST).isoformat(),
            "resolved_at": bot.datetime.now(bot.IST).isoformat(),
            "status": status, "resolution_price": None, "result_pct": None, "outcome_label": None}
    base.update(kw)
    return base


def test_resolution_embed_win_green():
    s = _resolved("WIN", resolution_price=99400.0, result_pct=3.8, outcome_label="TP1")
    e = bot.build_resolution_embed(s)
    assert e.color.value == bot.COLOR_LONG
    assert "TP1 HIT" in e.title
    assert "$95,800" in e.description and "$99,400" in e.description and "+3.8%" in e.description


def test_resolution_embed_loss_red():
    s = _resolved("LOSS", resolution_price=94200.0, result_pct=-1.7, outcome_label="SL")
    e = bot.build_resolution_embed(s)
    assert e.color.value == bot.COLOR_SHORT
    assert "STOPPED" in e.title and "SL $94,200" in e.description and "-1.7%" in e.description


def test_resolution_embed_expired_grey():
    s = _resolved("EXPIRED", resolution_price=96000.0, result_pct=0.2, outcome_label="EXPIRED")
    e = bot.build_resolution_embed(s)
    assert e.color.value == bot.COLOR_EXPIRED
    assert "EXPIRED" in e.title and "no trigger in 48h" in e.description


def test_compute_winrate_math():
    setups = [
        _resolved("WIN", entry_price=100, stop_loss=90, resolution_price=110, result_pct=10.0),
        _resolved("WIN", entry_price=100, stop_loss=95, resolution_price=108, result_pct=8.0),
        _resolved("LOSS", entry_price=100, stop_loss=96, resolution_price=96, result_pct=-4.0),
        _resolved("EXPIRED", resolution_price=101, result_pct=1.0),
        {"status": "OPEN", "entry_price": 100, "stop_loss": 95, "tp1": 110,
         "timestamp": bot.datetime.now(bot.IST).isoformat()},
    ]
    st = bot.compute_winrate(setups)
    assert st["total"] == 5 and st["open"] == 1
    assert st["wins"] == 2 and st["losses"] == 1 and st["expired"] == 1
    assert round(st["win_rate"], 1) == 66.7          # 2 / (2+1)
    assert st["best"] == 10.0 and st["worst"] == -4.0
    # realized R:R: win1 = |110-100|/|100-90| = 1.0 ; win2 = |108-100|/|100-95| = 1.6 → avg 1.3
    assert round(st["avg_rr"], 2) == 1.30


def test_compute_winrate_empty():
    st = bot.compute_winrate([])
    assert st["total"] == 0 and st["win_rate"] is None
    assert st["best"] is None and st["avg_rr"] is None


def test_winrate_embed_shape():
    e = bot.build_winrate_embed(bot.compute_winrate([
        _resolved("WIN", entry_price=100, stop_loss=90, resolution_price=110, result_pct=10.0),
    ]))
    names = {f.name for f in e.fields}
    assert {"Tracked", "Win rate", "Wins", "Losses", "Expired",
            "Best trade", "Worst trade", "Avg R:R realized"} <= names


class _FakeMsg:
    def __init__(self, mid=111, cid=222):
        self.id = mid

        class _Ch:
            id = cid
        self.channel = _Ch()


def test_log_setup_persists_long(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "CACHE_PATH", str(tmp_path))
    monkeypatch.setattr(bot, "SETUPS_PATH", str(tmp_path / "setups.json"))
    monkeypatch.setattr(bot, "_SETUPS", [])
    setup = {"has_setup": True, "direction": "LONG", "entry_zone": "$95,600 – $96,000",
             "stop_loss": "$94,200", "take_profit_1": "$99,400", "take_profit_2": "$101,000",
             "rr_ratio": "2.8", "conviction": "High"}
    bot.log_setup(setup, "BTC", "bitcoin", _FakeMsg(mid=555, cid=777))
    assert len(bot._SETUPS) == 1
    rec = bot._SETUPS[0]
    assert rec["entry_price"] == 95800.0 and rec["stop_loss"] == 94200.0 and rec["tp1"] == 99400.0
    assert rec["discord_message_id"] == 555 and rec["discord_channel_id"] == 777
    assert rec["status"] == "OPEN"
    # round-trips through disk
    import json
    saved = json.loads((tmp_path / "setups.json").read_text())
    assert saved["setups"][0]["coingecko_id"] == "bitcoin"


def test_log_setup_skips_no_clear_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "CACHE_PATH", str(tmp_path))
    monkeypatch.setattr(bot, "SETUPS_PATH", str(tmp_path / "setups.json"))
    monkeypatch.setattr(bot, "_SETUPS", [])
    bot.log_setup({"has_setup": False, "direction": "NONE"}, "BTC", "bitcoin", _FakeMsg())
    assert bot._SETUPS == []


def test_log_setup_skips_incoherent_levels(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "CACHE_PATH", str(tmp_path))
    monkeypatch.setattr(bot, "SETUPS_PATH", str(tmp_path / "setups.json"))
    monkeypatch.setattr(bot, "_SETUPS", [])
    # LONG but tp1 below entry → would resolve instantly; must be rejected.
    bad = {"has_setup": True, "direction": "LONG", "entry_zone": "$100",
           "stop_loss": "$95", "take_profit_1": "$90", "take_profit_2": "$80"}
    bot.log_setup(bad, "BTC", "bitcoin", _FakeMsg())
    assert bot._SETUPS == []


def test_init_setups_missing_file_starts_fresh(tmp_path, monkeypatch):
    path = tmp_path / "nope" / "setups.json"
    monkeypatch.setattr(bot, "CACHE_PATH", str(tmp_path / "nope"))
    monkeypatch.setattr(bot, "SETUPS_PATH", str(path))
    monkeypatch.setattr(bot, "_SETUPS", [{"stale": True}])
    bot.init_setups()
    assert bot._SETUPS == []          # reset
    assert path.exists()              # empty file created


def test_init_setups_loads_existing(tmp_path, monkeypatch):
    import json
    path = tmp_path / "setups.json"
    path.write_text(json.dumps({"version": 1, "setups": [
        {"ticker": "ETH", "status": "OPEN"}, {"ticker": "BTC", "status": "WIN"}]}))
    monkeypatch.setattr(bot, "CACHE_PATH", str(tmp_path))
    monkeypatch.setattr(bot, "SETUPS_PATH", str(path))
    monkeypatch.setattr(bot, "_SETUPS", [])
    bot.init_setups()
    assert len(bot._SETUPS) == 2
    assert {s["ticker"] for s in bot._SETUPS} == {"ETH", "BTC"}


def test_init_setups_corrupt_file_starts_fresh(tmp_path, monkeypatch):
    path = tmp_path / "setups.json"
    path.write_text("{ this is not valid json ")
    monkeypatch.setattr(bot, "CACHE_PATH", str(tmp_path))
    monkeypatch.setattr(bot, "SETUPS_PATH", str(path))
    monkeypatch.setattr(bot, "_SETUPS", [{"x": 1}])
    bot.init_setups()
    assert bot._SETUPS == []


def test_track_setups_resolves_and_persists(tmp_path, monkeypatch):
    import asyncio, json
    monkeypatch.setattr(bot, "CACHE_PATH", str(tmp_path))
    monkeypatch.setattr(bot, "SETUPS_PATH", str(tmp_path / "setups.json"))
    monkeypatch.setattr(bot, "_SETUPS", [
        {"ticker": "BTC", "coingecko_id": "bitcoin", "direction": "LONG",
         "entry_price": 95800.0, "stop_loss": 94200.0, "tp1": 99400.0, "tp2": 101000.0,
         "rr_ratio": "2.8", "conviction": "High",
         "timestamp": bot.datetime.now(bot.IST).isoformat(),
         "discord_message_id": 1, "discord_channel_id": 2, "status": "OPEN",
         "resolution_price": None, "resolved_at": None, "result_pct": None, "outcome_label": None},
    ])

    async def fake_price(tool, args, **kw):
        return {"market_data": {"current_price": 99500.0}}  # above tp1 → WIN

    posted = {"n": 0}

    async def fake_post(s):
        posted["n"] += 1

    monkeypatch.setattr(bot, "tn_call_safe", fake_price)
    monkeypatch.setattr(bot, "_post_resolution", fake_post)
    asyncio.run(bot.track_setups())

    rec = bot._SETUPS[0]
    assert rec["status"] == "WIN" and rec["outcome_label"] == "TP1"
    assert rec["resolution_price"] == 99500.0 and rec["result_pct"] > 0
    assert posted["n"] == 1
    saved = json.loads((tmp_path / "setups.json").read_text())
    assert saved["setups"][0]["status"] == "WIN"


def test_track_setups_no_open_is_noop(monkeypatch):
    import asyncio
    monkeypatch.setattr(bot, "_SETUPS", [{"status": "WIN"}])
    hit = {"price": False}

    async def fake_price(tool, args, **kw):
        hit["price"] = True
        return None

    monkeypatch.setattr(bot, "tn_call_safe", fake_price)
    asyncio.run(bot.track_setups())
    assert hit["price"] is False  # never price-checks resolved setups


def test_winrate_command_registered_no_cooldown():
    cmd = bot.bot.get_command("winrate")
    assert cmd is not None
    assert cmd._buckets._cooldown is None  # no cooldown on a local-file read


def test_setup_tracker_job_registered():
    bot.setup_scheduler()
    assert bot.scheduler.get_job("setup_tracker") is not None


# =============================================================================
# Regime-shift detection
# =============================================================================

def _ta(sma20_state, sma50_state, rsi, sma20_value=62000):
    return {"technical_indicators": {
        "sma20": {"value": sma20_value, "state": sma20_state},
        "sma50": {"value": 69000, "state": sma50_state},
        "rsi14": {"value": rsi, "state": "neutral"},
    }}


def _derivs(funding):
    return {"derivative_data": {"BTC": {
        "1h Aggregated OI weighted funding rate": {"current_funding_rate_in_percentage": funding}}}}


def test_btc_funding_extraction():
    assert bot._btc_funding(_derivs(0.003)) == 0.003
    assert bot._btc_funding(_derivs(-0.01)) == -0.01
    assert bot._btc_funding({}) is None
    assert bot._btc_funding(None) is None


def test_vix_value_extraction():
    idx = {"prices": [{"index": "vix", "latest": {"close": 21.5}},
                      {"index": "gspc", "latest": {"close": 7000}}]}
    assert bot._vix_value(idx) == 21.5
    assert bot._vix_value({"prices": []}) is None
    assert bot._vix_value(None) is None


def test_detect_regime_risk_off():
    # BTC below both MAs + funding negative + RSI 35 → 3 off signals
    regime, reasons = bot.detect_regime(_ta("price_below", "price_below", 35), _derivs(-0.02))
    assert regime == "RISK-OFF"
    assert any("below" in r and "MA" in r for r in reasons)
    assert any("funding negative" in r for r in reasons)
    assert any("RSI 35" in r for r in reasons)


def test_detect_regime_risk_on():
    regime, reasons = bot.detect_regime(_ta("price_above", "price_above", 60), _derivs(0.01))
    assert regime == "RISK-ON"
    assert any("above 20d & 50d" in r for r in reasons)
    assert any("funding positive" in r for r in reasons)
    assert any("RSI 60" in r for r in reasons)


def test_detect_regime_neutral_one_signal_each():
    # above both MAs (on) but funding negative (off) and RSI 50 (neither) → neither 2+
    regime, _ = bot.detect_regime(_ta("price_above", "price_above", 50), _derivs(-0.01))
    assert regime == "NEUTRAL"


def test_detect_regime_risk_off_needs_two():
    # Only one off signal (RSI 35); funding positive, MAs above → not RISK-OFF
    regime, _ = bot.detect_regime(_ta("price_above", "price_above", 35), _derivs(0.01))
    assert regime != "RISK-OFF"


def test_detect_regime_vix_counts():
    idx = {"prices": [{"index": "vix", "latest": {"close": 25}}]}
    # below 50d MA (off) + VIX 25 (off) = RISK-OFF even with neutral RSI/funding
    regime, reasons = bot.detect_regime(_ta("price_above", "price_below", 48), _derivs(0.0), idx)
    assert regime == "RISK-OFF"
    assert any("VIX 25" in r for r in reasons)


def test_detect_regime_unknown_on_insufficient_data():
    # No usable TA, no derivs → fewer than 2 signals → UNKNOWN (no false flip)
    assert bot.detect_regime({}, {}) == ("UNKNOWN", [])
    assert bot.detect_regime(None, None)[0] == "UNKNOWN"
    # Only one signal available (funding) → still UNKNOWN
    assert bot.detect_regime({}, _derivs(-0.01))[0] == "UNKNOWN"


def test_detect_regime_ma_level_in_reason():
    _, reasons = bot.detect_regime(_ta("price_below", "price_below", 35, sma20_value=94200), _derivs(-0.01))
    assert any("$94,200" in r for r in reasons)


def _regime_env(tmp_path, monkeypatch, baseline="UNKNOWN"):
    monkeypatch.setattr(bot, "CACHE_PATH", str(tmp_path))
    monkeypatch.setattr(bot, "REGIME_PATH", str(tmp_path / "last_regime.json"))
    monkeypatch.setattr(bot, "_LAST_REGIME", {"regime": baseline, "timestamp": None, "reasons": []})


def test_init_regime_missing_creates_unknown(tmp_path, monkeypatch):
    _regime_env(tmp_path, monkeypatch, baseline="RISK-ON")
    bot.init_regime()
    assert bot._LAST_REGIME["regime"] == "UNKNOWN"
    assert (tmp_path / "last_regime.json").exists()


def test_init_regime_loads_existing(tmp_path, monkeypatch):
    import json
    (tmp_path / "last_regime.json").write_text(json.dumps(
        {"regime": "RISK-OFF", "timestamp": "2026-06-28T09:00:00+05:30", "reasons": ["x"]}))
    _regime_env(tmp_path, monkeypatch)
    bot.init_regime()
    assert bot._LAST_REGIME["regime"] == "RISK-OFF"


def test_check_regime_shift_first_run_sets_baseline_no_alert(tmp_path, monkeypatch):
    import asyncio
    _regime_env(tmp_path, monkeypatch, baseline="UNKNOWN")

    async def fake_call(tool, args, **kw):
        if tool == "technical_analysis":
            return _ta("price_below", "price_below", 35)
        return _derivs(-0.02)

    posted = {"n": 0}

    async def fake_post(*a):
        posted["n"] += 1

    monkeypatch.setattr(bot, "tn_call_safe", fake_call)
    monkeypatch.setattr(bot, "_post_regime_shift", fake_post)
    asyncio.run(bot.check_regime_shift("us"))
    assert bot._LAST_REGIME["regime"] == "RISK-OFF"  # baseline set
    assert posted["n"] == 0                          # UNKNOWN → RISK-OFF is not alerted


def test_check_regime_shift_alerts_on_flip(tmp_path, monkeypatch):
    import asyncio, json
    _regime_env(tmp_path, monkeypatch, baseline="RISK-ON")

    async def fake_call(tool, args, **kw):
        if tool == "technical_analysis":
            return _ta("price_below", "price_below", 35)
        return _derivs(-0.02)

    captured = {}

    async def fake_post(session, prev, regime, reasons):
        captured.update(session=session, prev=prev, regime=regime, reasons=reasons)

    monkeypatch.setattr(bot, "tn_call_safe", fake_call)
    monkeypatch.setattr(bot, "_post_regime_shift", fake_post)
    asyncio.run(bot.check_regime_shift("london"))
    assert captured["prev"] == "RISK-ON" and captured["regime"] == "RISK-OFF"
    assert captured["session"] == "london"
    # baseline persisted
    saved = json.loads((tmp_path / "last_regime.json").read_text())
    assert saved["regime"] == "RISK-OFF"


def test_check_regime_shift_same_regime_no_alert(tmp_path, monkeypatch):
    import asyncio
    _regime_env(tmp_path, monkeypatch, baseline="RISK-OFF")

    async def fake_call(tool, args, **kw):
        if tool == "technical_analysis":
            return _ta("price_below", "price_below", 35)
        return _derivs(-0.02)

    posted = {"n": 0}

    async def fake_post(*a):
        posted["n"] += 1

    monkeypatch.setattr(bot, "tn_call_safe", fake_call)
    monkeypatch.setattr(bot, "_post_regime_shift", fake_post)
    asyncio.run(bot.check_regime_shift("asia"))
    assert posted["n"] == 0  # RISK-OFF → RISK-OFF: no spam


def test_check_regime_shift_data_outage_does_not_flip(tmp_path, monkeypatch):
    import asyncio
    _regime_env(tmp_path, monkeypatch, baseline="RISK-ON")

    async def dead(tool, args, **kw):
        return None  # both fetches fail

    posted = {"n": 0}

    async def fake_post(*a):
        posted["n"] += 1

    monkeypatch.setattr(bot, "tn_call_safe", dead)
    monkeypatch.setattr(bot, "_post_regime_shift", fake_post)
    asyncio.run(bot.check_regime_shift("us"))
    assert posted["n"] == 0                       # UNKNOWN from outage → no alert
    assert bot._LAST_REGIME["regime"] == "RISK-ON"  # baseline unchanged


def test_regime_shift_embed_shape(monkeypatch):
    import asyncio

    class _Ch:
        sent = []

        async def send(self, embed=None):
            _Ch.sent.append(embed)

    monkeypatch.setattr(bot.bot, "get_channel", lambda cid: _Ch())
    asyncio.run(bot._post_regime_shift("us", "RISK-ON", "RISK-OFF",
                                       ["BTC below 20d MA ($94,200)", "funding negative (-0.020%)", "RSI 35 (<40)"]))
    e = _Ch.sent[-1]
    assert e.color.value == bot.COLOR_REGIME_SHIFT
    assert e.title == "⚠️ REGIME SHIFT: Risk-On → Risk-Off"
    assert "BTC below 20d MA" in e.description
    assert "Auto-detected from US brief data" in e.footer.text


def test_regime_check_runs_before_compose_in_brief():
    # The regime check must be wired into run_session_brief ahead of the compose,
    # and must reuse the brief's own TA/derivs instead of refetching.
    import inspect
    src = inspect.getsource(bot.run_session_brief)
    assert "check_regime_shift" in src
    assert src.index("check_regime_shift") < src.index("build_rule_brief(")
    assert 'ta=d.get("ta_btc")' in src and 'derivs=d.get("derivs")' in src
