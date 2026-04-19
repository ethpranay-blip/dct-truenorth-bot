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


def test_rotate_thread_id_persists_uuid_fallback(tmp_path, monkeypatch):
    """The synchronous rotate_thread_id() always produces a persisted UUID."""
    cache_file = tmp_path / "thread_cache.json"
    monkeypatch.setattr(bot, "THREAD_CACHE_PATH", str(cache_file))
    new_id = bot.rotate_thread_id()
    import uuid as _uuid
    _uuid.UUID(new_id)  # raises if not a valid UUID
    assert cache_file.exists()
    cached = bot.load_cached_thread()
    assert cached == new_id


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


# re needs to be accessible in the test module for regex-based assertions above.
import re  # noqa: E402
