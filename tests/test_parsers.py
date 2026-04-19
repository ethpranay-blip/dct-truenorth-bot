"""Unit tests for TrueNorth markdown response parsers."""
import bot


SAMPLE_BRIEF = """
**Asia Session Brief**

BTC trading at $67,450 after reclaiming the 4H range. Volume is thin but constructive,
with momentum rolling back to neutral on the 1H.

---

$BTC | LONG | High Conviction
| Entry | $67,200 |
| Stop Loss | $66,300 |
| Take Profit | $69,800 |
| R:R | 2.9:1 |

- Reclaimed weekly VWAP
- Funding flipped positive but not crowded
- Watching $68k for confirmation

---

$ETH | SHORT | Medium Conviction
| Entry | $3,420 |
| Stop Loss | $3,505 |
| Take Profit | $3,250 |
| R:R | 2:1 |

- Losing 200D EMA
- OI building into resistance
- Correlated risk if BTC rejects $68k

---

$SOL | LONG | Speculative
Entry: $152.40
Stop Loss: $148.00
Take Profit: $161.80
R:R: 2.1

- Relative strength vs ETH
- Clear break of the Asia range
- Needs BTC to hold bid

---

⚠️ Session Risk Flag: US CPI prints at 13:30 UTC. Reduce size into the event; avoid adding
exposure 30 min before the release.
"""


SAMPLE_PIPE_HEADER = """
$WIF | LONG | High Conviction
Entry | $2.80
Stop | $2.55
Target | $3.45
R/R | 2.3:1

- Smart money accumulating
- Bullish divergence on daily
"""


SAMPLE_NO_TRADES = """
Markets are chopping sideways. No high-conviction setups right now.
Risk flag: FOMC minutes release tomorrow.
"""


def test_parse_trades_extracts_three_setups():
    trades = bot.parse_trades_from_text(SAMPLE_BRIEF)
    tickers = [t["ticker"] for t in trades]
    assert tickers == ["BTC", "ETH", "SOL"], f"unexpected tickers: {tickers}"


def test_parse_trades_directions():
    trades = bot.parse_trades_from_text(SAMPLE_BRIEF)
    directions = {t["ticker"]: t["direction"] for t in trades}
    assert directions == {"BTC": "LONG", "ETH": "SHORT", "SOL": "LONG"}


def test_parse_trades_table_values():
    """Prices are stored without a leading $; _fmt_price adds it exactly once at display."""
    trades = bot.parse_trades_from_text(SAMPLE_BRIEF)
    btc = next(t for t in trades if t["ticker"] == "BTC")
    assert btc["entry"] == "67,200"
    assert btc["sl"] == "66,300"
    assert btc["tp"] == "69,800"
    assert bot._fmt_price(btc["entry"]) == "$67,200"
    assert bot._fmt_price(btc["sl"]) == "$66,300"
    assert bot._fmt_price(btc["tp"]) == "$69,800"
    assert "2.9" in btc["rr"]
    assert "High Conviction" in btc["conviction"]


def test_parse_trades_label_fallback_parses_inline_prices():
    """SOL block uses inline 'Entry: $X' style, not pipe tables."""
    trades = bot.parse_trades_from_text(SAMPLE_BRIEF)
    sol = next(t for t in trades if t["ticker"] == "SOL")
    assert sol["entry"] and "152" in sol["entry"]
    assert sol["sl"] and "148" in sol["sl"]
    assert sol["tp"] and "161" in sol["tp"]


def test_parse_trades_notes_populated():
    trades = bot.parse_trades_from_text(SAMPLE_BRIEF)
    btc = next(t for t in trades if t["ticker"] == "BTC")
    assert len(btc["notes"]) >= 2
    assert any("VWAP" in n or "Funding" in n or "$68k" in n for n in btc["notes"])


def test_parse_trades_pipe_header_only():
    trades = bot.parse_trades_from_text(SAMPLE_PIPE_HEADER)
    assert len(trades) == 1
    t = trades[0]
    assert t["ticker"] == "WIF"
    assert t["direction"] == "LONG"
    assert t["entry"] == "2.80"
    assert t["sl"] == "2.55"
    assert t["tp"] == "3.45"
    assert bot._fmt_price(t["entry"]) == "$2.80"
    assert "2.3" in t["rr"]


def test_parse_trades_empty_when_no_setups():
    assert bot.parse_trades_from_text(SAMPLE_NO_TRADES) == []


def test_extract_risk_flag_session_format():
    risk = bot.extract_risk_flag(SAMPLE_BRIEF)
    assert risk is not None
    assert "CPI" in risk
    assert "13:30" in risk


def test_extract_risk_flag_short_form():
    text = "Some analysis here.\n\nRisk Flag: High volatility expected at 8am ET."
    risk = bot.extract_risk_flag(text)
    assert risk is not None
    assert "High volatility" in risk


def test_extract_risk_flag_none_when_absent():
    text = "Just a normal market brief with no risk section."
    assert bot.extract_risk_flag(text) is None


def test_decode_jwt_exp_valid():
    """A JWT payload with exp=2000000000 (2033-05-18) should decode to a tz-aware datetime."""
    import base64, json
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 2000000000}).encode()).decode().rstrip("=")
    token = f"{header}.{payload}.signature"
    exp = bot.decode_jwt_exp(token)
    assert exp is not None
    assert exp.year == 2033


def test_decode_jwt_exp_malformed_returns_none():
    assert bot.decode_jwt_exp("not-a-jwt") is None
    assert bot.decode_jwt_exp("") is None
    assert bot.decode_jwt_exp("a.b") is None


# --- Regression tests for live bugs ---

def test_prices_are_not_double_dollar_prefixed():
    """Regression: Trade prices must render with exactly one leading $."""
    trades = bot.parse_trades_from_text(SAMPLE_BRIEF)
    for t in trades:
        for field in ("entry", "sl", "tp"):
            v = t[field]
            assert v, f"{t['ticker']} {field} empty"
            assert not v.startswith("$"), (
                f"{t['ticker']} {field} still has leading $ in parsed value: {v!r} — "
                "embed layer would double it to $$."
            )

    # _fmt_price is the display helper; it must produce exactly one $.
    assert bot._fmt_price("$110.50") == "$110.50"
    assert bot._fmt_price("110.50") == "$110.50"
    assert bot._fmt_price("  $$110.50  ") == "$110.50"
    assert bot._fmt_price("") == ""


SAMPLE_WITH_TOKEN_TAGS = """
$AAVE | LONG | ⚡ High Conviction
| Entry | $110.50 |
| Stop Loss | $105.00 |
| Take Profit | $125.00 |
| R:R | 2.6:1 |

- <Token tokenAddress="aave" tokenSymbol="AAVE" exchangeId="binance_futures" /> reclaimed its 50D MA
- Relative strength vs <Token tokenAddress="eth" tokenSymbol="ETH" exchangeId="binance_futures" />
- $AAVE | LONG | ⚡ High Conviction
- OI building without excessive funding
"""


def test_token_xml_tags_replaced_in_notes():
    trades = bot.parse_trades_from_text(SAMPLE_WITH_TOKEN_TAGS)
    assert len(trades) == 1
    notes = trades[0]["notes"]
    joined = " | ".join(notes)
    assert "<Token" not in joined, f"stray Token XML in notes: {joined!r}"
    assert "tokenSymbol" not in joined
    # Symbols should be preserved in $TICKER form
    assert "$AAVE" in joined
    assert "$ETH" in joined


def test_ticker_header_line_not_captured_as_note():
    trades = bot.parse_trades_from_text(SAMPLE_WITH_TOKEN_TAGS)
    for n in trades[0]["notes"]:
        assert not re.match(r'^\s*\$[A-Z]+\s*\|\s*(LONG|SHORT)', n), (
            f"ticker header leaked into notes: {n!r}"
        )


def test_strip_token_tags_standalone():
    text = 'buy <Token tokenAddress="x" tokenSymbol="btc" /> now'
    assert bot.strip_token_tags(text) == "buy $BTC now"
    # Missing tokenSymbol → tag removed entirely
    text2 = 'mystery <Token tokenAddress="foo" /> asset'
    assert bot.strip_token_tags(text2) == "mystery  asset"
    # Closing tags dropped
    assert bot.strip_token_tags("<Token/></Token>") == ""


def test_tn_state_dict_initial_values():
    """Observability counters live in a mutable dict so global rebinding bugs are impossible."""
    assert isinstance(bot.tn_state, dict)
    for key in ("last_success_at", "last_error_at", "last_error", "last_alert_at"):
        assert key in bot.tn_state


# --- Task 5: API-first thread creation with UUID fallback ---

def test_thread_create_endpoint_candidates_target_adventai():
    """All candidate endpoints must be on the AdventAI host so we never leak tokens elsewhere."""
    assert bot.TN_THREAD_CREATE_CANDIDATES, "candidates list must not be empty"
    for url in bot.TN_THREAD_CREATE_CANDIDATES:
        assert url.startswith("https://api.adventai.io/"), f"non-adventai endpoint: {url}"


def test_ensure_thread_id_returns_none_when_no_source_available(tmp_path, monkeypatch):
    """Critical regression: ensure_thread_id MUST NOT mint a random UUID.

    TN responds with HTTP 200 + zero chunks for unknown UUIDs, which used to
    burn ~25 minutes of useless retries. Returning None forces fast-fail.
    """
    cache_file = tmp_path / "thread_cache.json"
    monkeypatch.setattr(bot, "THREAD_CACHE_PATH", str(cache_file))
    monkeypatch.setattr(bot, "TN_THREAD_ENV", "")
    monkeypatch.setattr(bot, "_tn_thread_current", None)
    assert bot.ensure_thread_id() is None
    # No cache file should have been written.
    assert not cache_file.exists()


def test_get_tn_thread_does_not_generate_per_conv_uuid(tmp_path, monkeypatch):
    """Per-conversation calls must not synthesize random UUIDs either."""
    cache_file = tmp_path / "thread_cache.json"
    monkeypatch.setattr(bot, "THREAD_CACHE_PATH", str(cache_file))
    monkeypatch.setattr(bot, "TN_THREAD_ENV", "")
    monkeypatch.setattr(bot, "_tn_thread_current", None)
    bot.tn_thread_map.clear()
    assert bot.get_tn_thread(12345) is None
    assert bot.get_tn_thread(12345) is None  # idempotent


# --- Task 6: !brief confirmation routes to invoking channel ---

def test_channel_mention_falls_back_when_channel_missing():
    """_channel_mention should always produce a non-empty string, even on cache miss."""
    # bot.get_channel returns None for unknown ids in our test stub; helper must fall back.
    out = bot._channel_mention(999999999)
    assert isinstance(out, str) and out


# --- Extra coverage for the three categories the task list calls out ---

def test_dollar_strip_handles_nested_and_whitespace():
    """_clean_price strips leading $ and surrounding whitespace; idempotent."""
    assert bot._clean_price("$$$110.50") == "110.50"
    assert bot._clean_price("  $ 110.50  ") == "110.50"
    assert bot._clean_price(bot._clean_price("$110.50")) == "110.50"


def test_token_tag_inside_pipe_table_is_stripped():
    """Token tags inside table rows must not leak to embed values."""
    text = (
        '$AAVE | LONG | High Conviction\n'
        '| Entry | <Token tokenSymbol="AAVE" /> @ $110.50 |\n'
        '| Stop Loss | $105.00 |\n'
        '| Take Profit | $125.00 |\n'
        '| R:R | 2.6:1 |\n'
    )
    trades = bot.parse_trades_from_text(text)
    assert len(trades) == 1
    for v in (trades[0]["entry"], trades[0]["sl"], trades[0]["tp"]):
        assert "<Token" not in v and "tokenSymbol" not in v


def test_decode_jwt_exp_invalid_base64_returns_none():
    """A token whose payload segment isn't valid base64 must not raise."""
    assert bot.decode_jwt_exp("aaa.!!!not-base64!!!.bbb") is None
    # Payload that decodes but is not JSON
    import base64
    bogus = base64.urlsafe_b64encode(b"not json at all").decode().rstrip("=")
    assert bot.decode_jwt_exp(f"hdr.{bogus}.sig") is None


# --- Task 1: sanitize_tn_text covers Token + Anchor + generic tags ---

SAMPLE_WITH_ANCHORS = """
**BTC Update**

Bitcoin <Token tokenAddress="btc" tokenSymbol="BTC" exchangeId="binance_futures" /> is
testing the <Anchor annotationId="sr-bitcoin-resistance-1-76770-4h-short-liq-cluster" /> liquidation cluster.

Watching <Token tokenAddress="ethena" tokenSymbol="ENA" /> closely against
<Anchor annotationId="ena-pivot-3" /> support.
"""


def test_sanitize_tn_text_handles_token_anchor_and_generic_tags():
    out = bot.sanitize_tn_text(SAMPLE_WITH_ANCHORS)
    assert "<Token" not in out and "tokenSymbol" not in out
    assert "<Anchor" not in out and "annotationId" not in out
    assert "$BTC" in out
    assert "$ENA" in out
    # No double spaces left over from removed tags
    assert "  " not in out
    # Generic capitalised self-closing tag also stripped
    assert bot.sanitize_tn_text("hi <Foo bar='x' /> there") == "hi there"


def test_sanitize_tn_text_idempotent_and_safe_on_empty():
    """Sanitizing twice should produce the same output, and empty input is a no-op."""
    out = bot.sanitize_tn_text(SAMPLE_WITH_ANCHORS)
    assert bot.sanitize_tn_text(out) == out
    assert bot.sanitize_tn_text("") == ""
    # Plain prose without tags must pass through unchanged.
    plain = "BTC reclaimed VWAP and is consolidating at 67k."
    assert bot.sanitize_tn_text(plain) == plain


# --- Task 4: markdown tables wrapped in code blocks ---

def test_wrap_markdown_tables_wraps_consecutive_pipe_lines():
    text = (
        "Intro line.\n\n"
        "| Metric | Value | Read |\n"
        "|--------|-------|------|\n"
        "| Regime | Bearish | 🔴 |\n\n"
        "Trailing line."
    )
    out = bot.wrap_markdown_tables(text)
    assert "```\n| Metric" in out
    assert "🔴 |\n```" in out
    # Trailing line must remain outside the fence
    assert out.rstrip().endswith("Trailing line.")


def test_wrap_markdown_tables_leaves_single_pipe_alone():
    """A single pipe line is not a table — don't wrap it."""
    text = "Note: BTC | ETH dominance flip incoming."
    out = bot.wrap_markdown_tables(text)
    assert "```" not in out


def test_wrap_markdown_tables_empty_safe():
    assert bot.wrap_markdown_tables("") == ""
    assert bot.wrap_markdown_tables("plain text no pipes") == "plain text no pipes"


# --- Task 5: extract_risk_flag captures the full body ---

REALISTIC_BRIEF_WITH_RISK = """
**London Pre-Open Brief**

BTC trading at 67,450 with neutral 1H RSI.

| Metric | Value | Read |
|--------|-------|------|
| Regime | Bearish trend | 🔴 Bear with bounce risk |
| BTC.D  | 58.2%        | ⚠️ rotating into majors |

---

$BTC | LONG | High Conviction
| Entry | $67,200 |
| Stop Loss | $66,300 |
| Take Profit | $69,800 |

- Reclaimed weekly VWAP

---

⚠️ Session Risk Flags

| Catalyst | Time UTC | Impact |
|----------|----------|--------|
| US CPI   | 13:30    | High   |
| FOMC mins| 18:00    | Medium |

Reduce size into the CPI print; expect 1-2% intraday range expansion.

## Disclaimer
Not financial advice.
"""


def test_extract_risk_flag_captures_full_body_not_just_s():
    """Regression: previous regex captured only the trailing 's' of 'Flags'."""
    body = bot.extract_risk_flag(REALISTIC_BRIEF_WITH_RISK)
    assert body is not None
    assert body != "s", "regex regression — captured only the plural-s suffix"
    # Body must contain the actual risk content, not the heading word
    assert "CPI" in body
    assert "13:30" in body
    assert "Reduce size" in body
    # The terminator must stop at the next heading — the disclaimer must NOT be in the body
    assert "Not financial advice" not in body
    assert "Disclaimer" not in body
    # Tables in the body must be preserved (so build_risk_embed can wrap them)
    assert "| Catalyst |" in body


def test_extract_risk_flag_singular_form_still_works():
    text = "Some prose.\n\n**Risk Flag:**\nFOMC tomorrow at 18:00 UTC."
    body = bot.extract_risk_flag(text)
    assert body is not None
    assert "FOMC" in body


# --- Task 2: Privy refresh headers include Origin / Referer ---

def test_privy_headers_include_origin_and_referer():
    h = bot._privy_headers()
    assert h["privy-app-id"] == bot.PRIVY_APP_ID
    assert h["Content-Type"] == "application/json"
    assert h["Origin"] == bot.TN_APP_ORIGIN
    assert h["Referer"].startswith(bot.TN_APP_ORIGIN)
    assert h["Referer"].endswith("/")


# --- Task 3: false-positive alert suppression heuristics ---

def test_looks_like_preamble_detects_thinking_prefixes():
    assert bot._looks_like_preamble("Let me scan current market conditions for you...")
    assert bot._looks_like_preamble("Let me check BTC funding right now")
    assert not bot._looks_like_preamble("BTC is trading at 67,450 with positive funding.")
    assert not bot._looks_like_preamble("")
    # Very long text — not preamble even if it starts with one of the phrases
    long_text = "Let me scan " + ("the market " * 200)
    assert not bot._looks_like_preamble(long_text)


def test_tn_default_read_timeout_is_at_least_240():
    """Session briefs / !trades must have >=240s read timeout."""
    assert bot.TN_DEFAULT_READ_TIMEOUT >= 240
    assert bot.TN_RETRY_READ_TIMEOUT >= bot.TN_DEFAULT_READ_TIMEOUT


# --- Task 2: lowercase tags + <sp> tool-use markup ---

def test_sanitize_strips_sp_paired_tool_markup():
    """The <sp p="…">…</sp> Claude tool-use markup must vanish entirely from TN output."""
    text = (
        "Brief content.\n"
        '<sp p="deriv">Full liq cascade path: BTC at $76k</sp>\n'
        '<sp p="setup">Long bias unless 4H closes below 75k</sp>\n'
        "More analysis here."
    )
    out = bot.sanitize_tn_text(text)
    assert "<sp" not in out
    assert "</sp>" not in out
    assert "deriv" not in out, f"sp inner content leaked: {out!r}"
    assert "setup" not in out
    assert "Brief content." in out
    assert "More analysis here." in out


def test_sanitize_strips_lowercase_self_closing_and_paired():
    # Lowercase self-closing tag — drop entirely.
    assert bot.sanitize_tn_text("a <foo bar='x' /> b") == "a b"
    # Lowercase paired tag — keep inner text, drop wrapper (only <sp> is special-cased).
    assert bot.sanitize_tn_text("a <em>hello</em> b") == "a hello b"
    # Orphan <sp> opener (truncated stream) is also stripped.
    assert bot.sanitize_tn_text("text <sp p='deriv'> trailing") == "text trailing"


# --- Task 3: SSE chunk stitching + dedup ---

def test_stitch_sse_chunks_dedupes_overlapping_tail_head():
    chunks = [
        "Events are down. Let me pull TA on the top RS names.",
        "Events are down. Let me pull TA on the top RS names. Now BTC sits at 67k.",
    ]
    out = bot.stitch_sse_chunks(chunks)
    # The duplicate prefix from chunk[1] must be removed.
    assert out.count("Events are down. Let me pull TA") == 1
    assert "Now BTC sits at 67k." in out


def test_stitch_sse_chunks_handles_empty_and_unique():
    assert bot.stitch_sse_chunks([]) == ""
    assert bot.stitch_sse_chunks(["", "hello", "", " world"]) == "hello world"


def test_dedupe_contiguous_lines_drops_immediate_repeats():
    text = "Foo\nBar\nBar\nBaz\nBaz\nBaz\nQux"
    out = bot.dedupe_contiguous_lines(text)
    lines = [l for l in out.split("\n") if l]
    assert lines == ["Foo", "Bar", "Baz", "Qux"]


def test_dedupe_repeated_substrings_collapses_adjacent_blocks():
    block = "BTC reclaimed VWAP and is consolidating at 67k."
    text = block + block  # two identical blocks back-to-back
    out = bot.dedupe_repeated_substrings(text)
    assert out.count(block) == 1


def test_dedupe_tn_text_pipeline_no_change_for_clean_text():
    """Clean prose must round-trip unchanged (idempotent on non-duplicated input)."""
    clean = "BTC at 67k. ETH at 3.4k. Risk-on tone holding."
    assert bot.dedupe_tn_text(clean) == clean


# --- Task 5: brief embed prose extraction ---

def test_first_prose_paragraph_skips_tables_and_bullets():
    text = (
        "| col1 | col2 |\n|------|------|\n| 1 | 2 |\n\n"
        "- bullet one\n- bullet two\n\n"
        "Bitcoin is trading at 67,450 with neutral RSI on the 1H.\n\n"
        "More content."
    )
    summary = bot._first_prose_paragraph(text)
    assert summary.startswith("Bitcoin")


def test_truncate_at_sentence_prefers_sentence_boundary():
    text = "First sentence. Second sentence. Third sentence."
    out = bot._truncate_at_sentence(text, 20)
    # Should end with the ellipsis after a sentence terminator (period/exclaim/question).
    assert out.endswith("…")
    # The text body before the ellipsis must end at a sentence terminator.
    body = out[:-1].rstrip()
    assert body and body[-1] in ".!?", f"truncated body {body!r} did not end at sentence boundary"


# --- Task 1: Privy refresh variant probing ---

def test_privy_refresh_variants_cover_required_shapes():
    variants = bot._privy_refresh_variants("rt-fake", "at-fake")
    names = [v["name"] for v in variants]
    assert "snake_body" in names
    assert "snake_body+bearer_refresh" in names
    assert "camel_body" in names
    assert "bearer_refresh_only" in names
    assert "snake_body+bearer_access" in names

    # Verify the body/header shapes themselves.
    by_name = {v["name"]: v for v in variants}
    assert by_name["snake_body"]["body"] == {"refresh_token": "rt-fake"}
    assert by_name["camel_body"]["body"] == {"refreshToken": "rt-fake"}
    assert by_name["bearer_refresh_only"]["body"] == {}
    assert by_name["snake_body+bearer_refresh"]["headers"]["Authorization"] == "Bearer rt-fake"
    assert by_name["snake_body+bearer_access"]["headers"]["Authorization"] == "Bearer at-fake"
    # Origin/Referer must be present in every variant (Task 2 from prior round).
    for v in variants:
        assert v["headers"]["Origin"] == bot.TN_APP_ORIGIN


def test_privy_refresh_variants_omit_access_variant_when_no_access_token():
    variants = bot._privy_refresh_variants("rt-only", "")
    names = [v["name"] for v in variants]
    assert "snake_body+bearer_access" not in names


# --- Round 5 Task 1: orphan opening/closing tags ---

def test_sanitize_strips_orphan_anchor_close_in_sentence():
    text = (
        "BTC is testing the $74.4K long liquidation cluster gets hunted.</Anchor>\n"
        "Next sentence continues."
    )
    out = bot.sanitize_tn_text(text)
    assert "</Anchor>" not in out
    assert "Anchor" not in out
    # Surrounding text must be intact.
    assert "$74.4K" in out
    assert "Next sentence continues." in out


def test_sanitize_strips_orphan_opening_tag():
    """An orphan <Foo attr='x'> with no matching close still gets stripped."""
    text = "before <Foo attr='x'> after"
    out = bot.sanitize_tn_text(text)
    assert "<Foo" not in out and ">" not in out
    assert "before" in out and "after" in out


def test_sanitize_keeps_paired_inner_text_before_orphan_pass():
    """Paired tags must still keep their inner text — the orphan pass doesn't strip them first."""
    text = "say <em>hello</em> world"
    assert bot.sanitize_tn_text(text) == "say hello world"


# --- Round 5 Task 2: stale date override ---

def test_strip_stale_dates_removes_long_form_dates():
    text = "As of Apr 19, 2025, BTC trades at 67k.\nNext line."
    out = bot._strip_stale_dates(text)
    assert "Apr 19, 2025" not in out
    assert "BTC trades at 67k" in out
    assert "Next line." in out


def test_strip_stale_dates_removes_iso_dates_and_orphan_labels():
    text = "Date: 2025-04-19\nReport: BTC steady."
    out = bot._strip_stale_dates(text)
    assert "2025-04-19" not in out
    assert "Date:" not in out
    assert "BTC steady" in out


def test_strip_stale_dates_no_op_on_clean_text():
    text = "BTC reclaimed VWAP. ETH lagging."
    assert bot._strip_stale_dates(text) == text


# --- Round 5 Task 3: section-aware truncation ---

def test_truncate_does_not_leave_dangling_section_header():
    """A truncate that lands right after '5.' must back up to the previous sentence."""
    text = (
        "1. Macro: dovish tone holding.\n"
        "2. BTC: reclaimed weekly VWAP at 67k.\n"
        "3. ETH: lagging the majors.\n"
        "4. SOL: relative strength.\n"
        "5. Risks ahead — multiple catalysts queued for the next 48 hours.\n"
    )
    # Force a truncation right around section "5.". Pick a limit that lands just
    # after the "5." marker but before the body.
    truncate_at = text.index("5.") + 3
    out = bot._truncate_at_sentence(text, truncate_at)
    # Output must not end with a dangling "5." style header.
    body = out.rstrip("…").rstrip()
    last_line = body.rsplit("\n", 1)[-1].strip()
    assert not re.match(r'^\d+\.+\s*$', last_line), f"dangling header in {out!r}"


def test_strip_dangling_header_removes_orphan_number_lines():
    assert bot._strip_dangling_header("body.\n5.") == "body."
    assert bot._strip_dangling_header("body.\n5....") == "body."
    assert bot._strip_dangling_header("body.") == "body."
    assert bot._strip_dangling_header("body.\n5. body") == "body.\n5. body"


# --- Round 5 Task 4: leading/trailing --- stripped from risk flag ---

def test_extract_risk_flag_strips_trailing_horizontal_rule():
    text = (
        "**Session Risk Flag**\n\n"
        "FOMC at 18:00 UTC. Reduce size into print.\n\n"
        "---\n"
    )
    body = bot.extract_risk_flag(text)
    assert body is not None
    assert body.endswith("Reduce size into print."), f"trailing --- leaked: {body!r}"


def test_extract_risk_flag_strips_leading_horizontal_rule():
    text = (
        "**Session Risk Flag**\n\n"
        "---\n\n"
        "FOMC at 18:00 UTC. Reduce size."
    )
    body = bot.extract_risk_flag(text)
    assert body is not None
    assert body.startswith("FOMC")


def test_classify_failure_buckets_each_failure_mode():
    """Each known failure mode must map to its tag so the embed picks the right copy."""
    # ok path: status 200 with text and no SSE error
    assert bot._classify_failure(200, "", False, None, "real content", True) == "ok"
    # auth
    assert bot._classify_failure(401, "Unauthorized", False, None, "", False) == "auth"
    assert bot._classify_failure(403, "Forbidden", False, None, "", False) == "auth"
    # generic http error
    assert bot._classify_failure(500, "boom", False, None, "", False) == "http_error"
    # explicit SSE error event
    assert bot._classify_failure(200, '"detail": "denied"', True, None, "", False) == "sse_error"
    # empty 200 (the unknown-thread signature)
    assert bot._classify_failure(200, "", False, None, "", False) == "empty_200"
    # incomplete preamble heuristic
    assert bot._classify_failure(200, "", False, None, "Let me scan…", False) == "incomplete_preamble"
    # exception path classifies as exception unless timeout
    import httpx as _httpx
    assert bot._classify_failure(0, "", False, _httpx.ReadTimeout("slow"), "", False) == "timeout"
    assert bot._classify_failure(0, "", False, RuntimeError("boom"), "", False) == "exception"


def test_format_failure_reason_uses_actionable_copy():
    auth_msg = bot._format_failure_reason(
        {"tag": "auth", "status": 401, "err": "Unauthorized"}
    )
    assert "Run !refreshtoken" in auth_msg

    empty_msg = bot._format_failure_reason({"tag": "empty_200", "status": 200, "err": ""})
    assert "TN_THREAD_ID is invalid" in empty_msg
    assert "app.true-north.xyz" in empty_msg

    timeout_msg = bot._format_failure_reason(
        {"tag": "timeout", "status": 0, "err": "", "exc": None}
    )
    assert "timed out" in timeout_msg.lower()


def test_invalidate_thread_marks_state_and_clears_current():
    """invalidate_thread() must flip the flag, clear the current id, and record a reason."""
    bot._tn_thread_current = "some-id"
    bot.tn_state["thread_invalid"] = False
    bot.tn_state["thread_invalid_reason"] = ""
    bot.tn_state["boot_warning_posted"] = False
    bot.invalidate_thread("manual test")
    assert bot.tn_state["thread_invalid"] is True
    assert "manual test" in bot.tn_state["thread_invalid_reason"]
    assert bot._tn_thread_current is None


def test_retry_policy_constants_match_spec():
    """Spec: max 2 retries (3 attempts total) with backoff 10s, 30s."""
    assert bot.TN_MAX_ATTEMPTS == 3
    assert bot.TN_RETRY_BACKOFF_S == (10.0, 30.0)


def test_thread_invalid_hint_includes_actionable_steps():
    """The user-facing instruction must point at the right URL + steps."""
    hint = bot.THREAD_INVALID_HINT
    assert "app.true-north.xyz" in hint
    assert "TN_THREAD_ID" in hint
    assert "Railway" in hint


def test_extract_risk_flag_preserves_inner_table_rows():
    """Tables inside the body must stay (they get fenced by build_risk_embed)."""
    text = (
        "**Risk Flag**\n\n"
        "| Catalyst | Time |\n"
        "|----------|------|\n"
        "| CPI      | 13:30|\n\n"
        "Reduce size.\n"
        "---\n"
    )
    body = bot.extract_risk_flag(text)
    assert body is not None
    assert "| Catalyst" in body
    assert body.endswith("Reduce size.")


# re needs to be accessible in the test module for regex-based assertions above.
import re  # noqa: E402
