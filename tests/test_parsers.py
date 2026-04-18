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


# re needs to be accessible in the test module for regex-based assertions above.
import re  # noqa: E402
