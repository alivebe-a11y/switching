"""Tests for broker_trading212.py — unit tests using mocked HTTP."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from switching.broker_trading212 import (
    T212AuthError,
    T212OrderError,
    T212RateLimitError,
    Trading212Client,
    _from_t212_ticker,
    _matches_market,
    _retry_after_seconds,
    _safe_float,
    _to_t212_ticker,
)


# ---------------------------------------------------------------------------
# Ticker conversion helpers
# ---------------------------------------------------------------------------


def test_to_t212_ticker_plain():
    assert _to_t212_ticker("AAPL") == "AAPL_US_EQ"


def test_to_t212_ticker_lowercase():
    assert _to_t212_ticker("msft") == "MSFT_US_EQ"


def test_to_t212_ticker_passthrough():
    """Already formatted tickers should pass through unchanged."""
    assert _to_t212_ticker("NVDA_US_EQ") == "NVDA_US_EQ"


def test_from_t212_ticker():
    assert _from_t212_ticker("AAPL_US_EQ") == "AAPL"
    assert _from_t212_ticker("TSLA_US_EQ") == "TSLA"


def test_from_t212_ticker_roundtrip():
    sym = "NVDA"
    assert _from_t212_ticker(_to_t212_ticker(sym)) == sym


def test_to_t212_ticker_rejects_invalid():
    """Symbols with non-alphanumeric chars other than underscore must be rejected."""
    with pytest.raises(ValueError, match="Invalid ticker"):
        _to_t212_ticker("../../etc/passwd")


def test_to_t212_ticker_rejects_too_long():
    with pytest.raises(ValueError, match="Invalid ticker"):
        _to_t212_ticker("A" * 21)


# ---------------------------------------------------------------------------
# UK ticker conversion (LSE: {TICKER}L_EQ)
# ---------------------------------------------------------------------------


def test_to_t212_ticker_uk_with_dot_l():
    """UK input "MKS.L" should map to "MKSL_EQ"."""
    assert _to_t212_ticker("MKS.L", "uk") == "MKSL_EQ"


def test_to_t212_ticker_uk_without_dot_l():
    """UK input "MKS" should also map to "MKSL_EQ"."""
    assert _to_t212_ticker("MKS", "uk") == "MKSL_EQ"


def test_to_t212_ticker_uk_multi_char():
    """Longer UK tickers (BARC, BARCL_EQ) behave the same."""
    assert _to_t212_ticker("BARC.L", "uk") == "BARCL_EQ"
    assert _to_t212_ticker("VOD.L", "uk") == "VODL_EQ"


def test_to_t212_ticker_uk_passthrough():
    assert _to_t212_ticker("MKSL_EQ", "uk") == "MKSL_EQ"


def test_to_t212_ticker_uk_rejects_dot_in_middle():
    with pytest.raises(ValueError, match="Invalid UK ticker"):
        _to_t212_ticker("M.K.S", "uk")


def test_to_t212_ticker_us_rejects_dot():
    """US tickers should never carry a dot."""
    with pytest.raises(ValueError, match="Invalid US ticker"):
        _to_t212_ticker("MKS.L", "us")


def test_to_t212_ticker_rejects_unknown_market():
    with pytest.raises(ValueError, match="Unsupported market"):
        _to_t212_ticker("AAPL", "de")


def test_from_t212_ticker_uk():
    assert _from_t212_ticker("MKSL_EQ", "uk") == "MKS.L"
    assert _from_t212_ticker("BARCL_EQ", "uk") == "BARC.L"
    assert _from_t212_ticker("VODL_EQ", "uk") == "VOD.L"


def test_uk_roundtrip():
    """`.L` ticker should roundtrip through T212 ID unchanged."""
    for sym in ("MKS.L", "VOD.L", "BARC.L", "JUP.L", "SHI.L"):
        assert _from_t212_ticker(_to_t212_ticker(sym, "uk"), "uk") == sym


# ---------------------------------------------------------------------------
# Bulkhead — _matches_market filter
# ---------------------------------------------------------------------------


def test_matches_market_us():
    assert _matches_market("AAPL_US_EQ", "us") is True
    assert _matches_market("MKSL_EQ", "us") is False
    assert _matches_market("VODL_EQ", "us") is False
    assert _matches_market("VOD_US_EQ", "us") is True


def test_matches_market_uk():
    assert _matches_market("MKSL_EQ", "uk") is True
    assert _matches_market("VODL_EQ", "uk") is True
    assert _matches_market("BARCL_EQ", "uk") is True
    # The US ADR for Vodafone must NOT match the UK service
    assert _matches_market("VOD_US_EQ", "uk") is False
    assert _matches_market("AAPL_US_EQ", "uk") is False


def test_matches_market_other_markets_excluded():
    """EU / other markets match neither US nor UK and are filtered from both."""
    # T212 uses _DE_EQ for Xetra, _FR_EQ for Euronext Paris, etc.
    for foreign in ("SAP_DE_EQ", "MC_FR_EQ", "ASML_NL_EQ"):
        assert _matches_market(foreign, "us") is False
        assert _matches_market(foreign, "uk") is False


def test_matches_market_empty_ticker():
    assert _matches_market("", "us") is False
    assert _matches_market("", "uk") is False


# ---------------------------------------------------------------------------
# _safe_float
# ---------------------------------------------------------------------------


def test_safe_float_normal():
    assert _safe_float(1.5) == 1.5


def test_safe_float_none_returns_default():
    assert _safe_float(None) == 0.0
    assert _safe_float(None, default=99.9) == 99.9


def test_safe_float_string_number():
    assert _safe_float("3.14") == 3.14


def test_safe_float_bad_string_returns_default():
    assert _safe_float("not-a-number") == 0.0


def test_safe_float_corrupt_api_value():
    """Simulate T212 returning null/object in a numeric field — must not crash."""
    assert _safe_float({}) == 0.0
    assert _safe_float([]) == 0.0


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def test_missing_api_key_raises():
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("T212_API_KEY", None)
        os.environ.pop("T212_API_SECRET", None)
        with pytest.raises(T212AuthError, match="T212_API_KEY"):
            Trading212Client()


def test_missing_api_secret_raises(monkeypatch):
    monkeypatch.setenv("T212_API_KEY", "test-key")
    monkeypatch.delenv("T212_API_SECRET", raising=False)
    with pytest.raises(T212AuthError, match="T212_API_SECRET"):
        Trading212Client()


def test_demo_default(monkeypatch):
    monkeypatch.setenv("T212_API_KEY", "test-key")
    monkeypatch.setenv("T212_API_SECRET", "test-secret")
    monkeypatch.delenv("T212_DEMO", raising=False)
    client = Trading212Client()
    assert client.demo is True
    assert "demo" in client._base


def test_live_mode(monkeypatch):
    monkeypatch.setenv("T212_API_KEY", "test-key")
    monkeypatch.setenv("T212_API_SECRET", "test-secret")
    monkeypatch.setenv("T212_DEMO", "false")
    client = Trading212Client()
    assert client.demo is False
    assert "live" in client._base


def test_demo_true_explicit(monkeypatch):
    monkeypatch.setenv("T212_API_KEY", "test-key")
    monkeypatch.setenv("T212_API_SECRET", "test-secret")
    monkeypatch.setenv("T212_DEMO", "true")
    client = Trading212Client()
    assert client.demo is True


def test_uses_basic_auth(monkeypatch):
    """Session must use HTTP Basic Auth (key:secret), not a raw header."""
    monkeypatch.setenv("T212_API_KEY", "mykey")
    monkeypatch.setenv("T212_API_SECRET", "mysecret")
    client = Trading212Client()
    assert client._session.auth == ("mykey", "mysecret")
    assert "Authorization" not in client._session.headers


# ---------------------------------------------------------------------------
# Fixture: patched client with mocked session
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("T212_API_KEY", "fake-key")
    monkeypatch.setenv("T212_API_SECRET", "fake-secret")
    monkeypatch.setenv("T212_DEMO", "true")
    c = Trading212Client()
    c._session = MagicMock()
    return c


def _mock_response(json_data, status_code=200):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = str(json_data)
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# get_account
# ---------------------------------------------------------------------------


def test_get_account(client):
    # Real T212 schema: GET /equity/account/summary
    client._session.get.return_value = _mock_response({
        "id": 123,
        "currency": "GBP",
        "totalValue": 10200.0,
        "cash": {
            "availableToTrade": 9500.0,
            "inPies": 0.0,
            "reservedForOrders": 0.0,
        },
        "investments": {
            "currentValue": 700.0,
            "totalCost": 650.0,
            "unrealizedProfitLoss": 50.0,
            "realizedProfitLoss": 0.0,
        },
    })
    acct = client.get_account()
    assert acct.free == 9500.0
    assert acct.total == 10200.0
    assert acct.invested == 700.0
    assert acct.ppl == 50.0
    assert acct.currency == "GBP"   # account currency captured for display


def test_currency_symbol_helper():
    from switching.broker_trading212 import currency_symbol
    assert currency_symbol("GBP") == "£"
    assert currency_symbol("USD") == "$"
    assert currency_symbol("EUR") == "€"
    assert currency_symbol(None) == "$"          # safe fallback
    assert currency_symbol("AUD") == "AUD"       # unknown ISO -> shown as-is


def test_get_account_missing_fields(client):
    """Partial response should not crash — missing sections default to 0."""
    client._session.get.return_value = _mock_response({"totalValue": 1000.0})
    acct = client.get_account()
    assert acct.free == 0.0
    assert acct.total == 1000.0


# ---------------------------------------------------------------------------
# get_positions
# ---------------------------------------------------------------------------


def _make_position_item(t212_ticker, quantity, avg_price, current_price, ppl):
    """Build a position dict matching the real T212 API schema."""
    return {
        "instrument": {"ticker": t212_ticker, "currency": "USD",
                       "isin": "US0378331005", "name": "Apple Inc."},
        "quantity": quantity,
        "averagePricePaid": avg_price,   # real field name (not averagePrice)
        "currentPrice": current_price,
        "quantityAvailableForTrading": quantity,
        "quantityInPies": 0.0,
        "walletImpact": {                # real field name (not ppl)
            "currency": "GBP",
            "currentValue": quantity * current_price,
            "totalCost": quantity * avg_price,
            "unrealizedProfitLoss": ppl,
            "fxImpact": 0.0,
        },
    }


def test_get_positions_list_format(client):
    """T212 may return a bare list or a paginated dict."""
    client._session.get.return_value = _mock_response([
        _make_position_item("AAPL_US_EQ", 5.0, 180.0, 190.0, 50.0)
    ])
    positions = client.get_positions()
    assert len(positions) == 1
    p = positions[0]
    assert p.symbol == "AAPL"
    assert p.t212_ticker == "AAPL_US_EQ"
    assert p.quantity == 5.0
    assert p.avg_entry_price == 180.0
    assert p.current_price == 190.0
    assert p.unrealized_pnl == 50.0
    assert abs(p.unrealized_pnl_pct - (190 - 180) / 180) < 0.0001


def test_get_positions_paginated_format(client):
    """Paginated response with items key."""
    client._session.get.return_value = _mock_response({
        "items": [_make_position_item("MSFT_US_EQ", 2.0, 400.0, 410.0, 20.0)],
        "nextPagePath": None,
    })
    positions = client.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "MSFT"


def test_get_positions_empty(client):
    client._session.get.return_value = _mock_response([])
    assert client.get_positions() == []


def test_get_position_by_symbol(client):
    client._session.get.return_value = _mock_response([
        _make_position_item("NVDA_US_EQ", 1.0, 900.0, 950.0, 50.0),
        _make_position_item("AAPL_US_EQ", 3.0, 170.0, 175.0, 15.0),
    ])
    pos = client.get_position("NVDA")
    assert pos is not None
    assert pos.symbol == "NVDA"


def test_get_position_not_held(client):
    client._session.get.return_value = _mock_response([])
    assert client.get_position("TSLA") is None


# ---------------------------------------------------------------------------
# buy_market
# ---------------------------------------------------------------------------


def test_buy_market(client):
    client._session.post.return_value = _mock_response({
        "id": "ord-123", "ticker": "AAPL_US_EQ", "status": "CONFIRMED",
    })
    order = client.buy_market("AAPL", 2.5)
    assert order.id == "ord-123"
    assert order.status == "CONFIRMED"
    # Verify payload sent
    payload = client._session.post.call_args[1]["json"]
    assert payload["ticker"] == "AAPL_US_EQ"
    assert payload["quantity"] == 2.5
    assert payload["quantity"] > 0   # buy = positive


def test_buy_market_converts_ticker(client):
    client._session.post.return_value = _mock_response({"id": "x", "status": "CONFIRMED"})
    client.buy_market("tsla", 1.0)
    payload = client._session.post.call_args[1]["json"]
    assert payload["ticker"] == "TSLA_US_EQ"


def test_buy_market_tiny_quantity_raises(client):
    with pytest.raises(T212OrderError, match="too small"):
        client.buy_market("AAPL", 0.00001)


# ---------------------------------------------------------------------------
# sell_all
# ---------------------------------------------------------------------------


def test_sell_all(client):
    client._session.post.return_value = _mock_response({
        "id": "ord-456", "ticker": "AAPL_US_EQ", "status": "CONFIRMED",
    })
    order = client.sell_all("AAPL", 5.0)
    assert order.status == "CONFIRMED"
    payload = client._session.post.call_args[1]["json"]
    assert payload["ticker"] == "AAPL_US_EQ"
    assert payload["quantity"] == -5.0   # sell = negative


def test_sell_all_quantity_is_negative(client):
    """sell_all must always send a negative quantity regardless of input sign."""
    client._session.post.return_value = _mock_response({"id": "x", "status": "CONFIRMED"})
    client.sell_all("MSFT", -3.0)  # caller accidentally passes negative
    payload = client._session.post.call_args[1]["json"]
    assert payload["quantity"] == -3.0


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_401_raises_auth_error(client):
    resp = _mock_response({"error": "Unauthorized"}, status_code=401)
    client._session.get.return_value = resp
    with pytest.raises(T212AuthError):
        client.get_account()


def test_400_raises_order_error(client):
    resp = _mock_response({"error": "Bad quantity"}, status_code=400)
    client._session.post.return_value = resp
    with pytest.raises(T212OrderError):
        client.buy_market("AAPL", 1.0)


# ---------------------------------------------------------------------------
# Market hours
# ---------------------------------------------------------------------------


def test_market_open_weekday_during_hours(monkeypatch, client):
    # is_market_open() now delegates to market_calendar.is_market_hours()
    # — patch that function directly rather than mocking datetime internals.
    with patch("switching.market_calendar.is_market_hours", return_value=True):
        assert client.is_market_open() is True


def test_market_closed_weekend(monkeypatch, client):
    with patch("switching.market_calendar.is_market_hours", return_value=False):
        assert client.is_market_open() is False


def test_market_closed_after_hours(monkeypatch, client):
    with patch("switching.market_calendar.is_market_hours", return_value=False):
        assert client.is_market_open() is False


def test_market_open_dst_first_hour(client):
    """During EDT the first hour (9:30–10:30 AM EDT = 13:30–14:30 UTC) must
    return True — this was broken by the hardcoded 14:30 UTC threshold that
    only worked in EST (winter time).

    We verify this at the market_calendar layer (which is_market_open delegates
    to) by passing an explicit 'now' datetime.  The broker just delegates, so
    if is_market_hours(13:45 UTC on a Thursday) is True the broker is correct.
    """
    from datetime import datetime, timezone
    from switching.market_calendar import is_market_hours

    # Thursday 13:45 UTC = 9:45 AM EDT — inside the old broken dead zone
    fixed = datetime(2026, 5, 14, 13, 45, 0, tzinfo=timezone.utc)
    assert is_market_hours(now=fixed) is True


# ---------------------------------------------------------------------------
# UK client — construction + market-hours dispatch + bulkhead
# ---------------------------------------------------------------------------


@pytest.fixture
def uk_client(monkeypatch):
    monkeypatch.setenv("T212_API_KEY", "fake-key")
    monkeypatch.setenv("T212_API_SECRET", "fake-secret")
    monkeypatch.setenv("T212_DEMO", "true")
    c = Trading212Client(market="uk")
    c._session = MagicMock()
    return c


def test_client_market_default_is_us(client):
    assert client.market == "us"


def test_client_market_uk(uk_client):
    assert uk_client.market == "uk"


def test_client_market_rejects_unknown(monkeypatch):
    monkeypatch.setenv("T212_API_KEY", "fake-key")
    monkeypatch.setenv("T212_API_SECRET", "fake-secret")
    with pytest.raises(ValueError, match="Unsupported T212 market"):
        Trading212Client(market="de")


def test_us_client_market_open_uses_nyse(client):
    """US client's is_market_open delegates to is_market_hours (NYSE)."""
    with patch("switching.market_calendar.is_market_hours", return_value=True), \
         patch("switching.market_calendar.is_lse_hours", return_value=False):
        assert client.is_market_open() is True


def test_uk_client_market_open_uses_lse(uk_client):
    """UK client's is_market_open delegates to is_lse_hours, NOT NYSE."""
    with patch("switching.market_calendar.is_market_hours", return_value=False), \
         patch("switching.market_calendar.is_lse_hours", return_value=True):
        assert uk_client.is_market_open() is True


def test_uk_client_market_open_lse_closed(uk_client):
    with patch("switching.market_calendar.is_market_hours", return_value=True), \
         patch("switching.market_calendar.is_lse_hours", return_value=False):
        # NYSE open but LSE closed — UK client should see closed
        assert uk_client.is_market_open() is False


# Bulkhead — the most important behaviour for two services sharing one account


def _positions_response(*tickers):
    """Build a minimal /equity/positions response with the given T212 ticker IDs."""
    return [
        {
            "instrument": {"ticker": t},
            "quantity": 1.0,
            "averagePricePaid": 100.0,
            "currentPrice": 105.0,
            "walletImpact": {"unrealizedProfitLoss": 5.0},
        }
        for t in tickers
    ]


def test_get_positions_bulkhead_us_filters_uk(client):
    """US client must not see UK positions (would otherwise be ghost-closed)."""
    client._session.get.return_value = _mock_response(
        _positions_response("AAPL_US_EQ", "MKSL_EQ", "VODL_EQ", "TSLA_US_EQ")
    )
    positions = client.get_positions()
    symbols = sorted(p.symbol for p in positions)
    assert symbols == ["AAPL", "TSLA"]


def test_get_positions_bulkhead_uk_filters_us(uk_client):
    """UK client must not see US positions (would otherwise be ghost-closed)."""
    uk_client._session.get.return_value = _mock_response(
        _positions_response("AAPL_US_EQ", "MKSL_EQ", "VODL_EQ", "TSLA_US_EQ")
    )
    positions = uk_client.get_positions()
    symbols = sorted(p.symbol for p in positions)
    assert symbols == ["MKS.L", "VOD.L"]


def test_get_positions_dual_listing_vod(uk_client, client):
    """Vodafone dual-listing: UK service sees VODL_EQ as VOD.L (GBX),
    US service sees VOD_US_EQ as VOD (USD ADR). The two never collide.
    """
    response = _mock_response(_positions_response("VOD_US_EQ", "VODL_EQ"))

    uk_client._session.get.return_value = response
    uk_positions = uk_client.get_positions()
    assert [p.symbol for p in uk_positions] == ["VOD.L"]
    assert uk_positions[0].t212_ticker == "VODL_EQ"

    client._session.get.return_value = response
    us_positions = client.get_positions()
    assert [p.symbol for p in us_positions] == ["VOD"]
    assert us_positions[0].t212_ticker == "VOD_US_EQ"


def test_get_positions_bulkhead_excludes_eu(client, uk_client):
    """Markets we don't trade (Xetra/Euronext) are filtered from BOTH services."""
    response = _mock_response(
        _positions_response("AAPL_US_EQ", "SAP_DE_EQ", "MC_FR_EQ", "MKSL_EQ")
    )

    client._session.get.return_value = response
    assert [p.symbol for p in client.get_positions()] == ["AAPL"]

    uk_client._session.get.return_value = response
    assert [p.symbol for p in uk_client.get_positions()] == ["MKS.L"]


def test_uk_client_buy_uses_l_eq_suffix(uk_client):
    """A UK client buying MKS.L must hit T212 with ticker=MKSL_EQ."""
    uk_client._session.post.return_value = _mock_response(
        {"id": "1", "ticker": "MKSL_EQ", "status": "CONFIRMED"}
    )
    uk_client.buy_market("MKS.L", 5.0)
    posted = uk_client._session.post.call_args.kwargs["json"]
    assert posted["ticker"] == "MKSL_EQ"
    assert posted["quantity"] == 5.0


def test_us_client_buy_uses_us_eq_suffix(client):
    """A US client buying AAPL must hit T212 with ticker=AAPL_US_EQ."""
    client._session.post.return_value = _mock_response(
        {"id": "1", "ticker": "AAPL_US_EQ", "status": "CONFIRMED"}
    )
    client.buy_market("AAPL", 5.0)
    posted = client._session.post.call_args.kwargs["json"]
    assert posted["ticker"] == "AAPL_US_EQ"


def test_uk_client_sell_uses_l_eq_suffix(uk_client):
    uk_client._session.post.return_value = _mock_response(
        {"id": "1", "ticker": "VODL_EQ", "status": "CONFIRMED"}
    )
    uk_client.sell_all("VOD.L", 3.0)
    posted = uk_client._session.post.call_args.kwargs["json"]
    assert posted["ticker"] == "VODL_EQ"
    assert posted["quantity"] == -3.0   # sells are negative


# ---------------------------------------------------------------------------
# Rate limiting: Retry-After parsing
# ---------------------------------------------------------------------------


def _resp(json_data=None, status_code=200, headers=None):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = str(json_data)
    resp.headers = headers if headers is not None else {}
    resp.raise_for_status = MagicMock()
    return resp


def test_retry_after_numeric():
    assert _retry_after_seconds(_resp(headers={"Retry-After": "3"})) == 3.0


def test_retry_after_absent_returns_none():
    assert _retry_after_seconds(_resp(headers={})) is None


def test_retry_after_garbage_returns_none():
    # HTTP-date form (not delta-seconds) — we don't parse it, fall back to backoff
    assert _retry_after_seconds(_resp(headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})) is None


def test_retry_after_clamped_to_max():
    assert _retry_after_seconds(_resp(headers={"Retry-After": "99999"})) == 120.0


def test_retry_after_negative_clamped_to_zero():
    assert _retry_after_seconds(_resp(headers={"Retry-After": "-5"})) == 0.0


# ---------------------------------------------------------------------------
# Rate limiting: per-endpoint throttle (stagger)
# ---------------------------------------------------------------------------


def test_throttle_first_call_no_sleep(client):
    with patch("switching.broker_trading212.time.sleep") as msleep:
        client._throttle("/equity/positions")
    msleep.assert_not_called()


def test_throttle_staggers_same_endpoint(client):
    # Two rapid calls to the same endpoint: the second must sleep ~min_interval (5s).
    with patch("switching.broker_trading212.time.monotonic", side_effect=[100.0, 100.0, 105.0]), \
         patch("switching.broker_trading212.time.sleep") as msleep:
        client._throttle("/equity/positions")   # first — sets timestamp, no sleep
        client._throttle("/equity/positions")   # second — must wait
    msleep.assert_called_once()
    waited = msleep.call_args[0][0]
    assert 4.9 <= waited <= 5.1


def test_throttle_per_endpoint_independent(client):
    # Different endpoints don't block each other.
    with patch("switching.broker_trading212.time.monotonic", side_effect=[100.0, 100.0]), \
         patch("switching.broker_trading212.time.sleep") as msleep:
        client._throttle("/equity/positions")
        client._throttle("/equity/account/summary")
    msleep.assert_not_called()


# ---------------------------------------------------------------------------
# Rate limiting: 429 retry / backoff
# ---------------------------------------------------------------------------


def test_429_retries_then_succeeds(client):
    r429 = _resp(status_code=429, headers={"Retry-After": "1"})
    rok = _resp({"totalValue": 5.0}, status_code=200)
    client._session.get.side_effect = [r429, rok]
    with patch("switching.broker_trading212.time.sleep") as msleep:
        data = client._get("/equity/account/summary")
    assert data == {"totalValue": 5.0}
    assert client._session.get.call_count == 2
    msleep.assert_called()   # backed off at least once


def test_429_exhausts_retries_raises(client):
    client._session.get.return_value = _resp(status_code=429, headers={"Retry-After": "1"})
    with patch("switching.broker_trading212.time.sleep"):
        with pytest.raises(T212RateLimitError):
            client._get("/equity/positions")
    # initial attempt + _MAX_RETRIES_429 retries
    from switching.broker_trading212 import _MAX_RETRIES_429
    assert client._session.get.call_count == _MAX_RETRIES_429 + 1


def test_429_uses_escalating_backoff_without_header(client):
    """No Retry-After header → escalating default backoff (5s, 10s, ...)."""
    client._session.get.side_effect = [
        _resp(status_code=429),                 # no headers
        _resp({"ok": True}, status_code=200),
    ]
    with patch("switching.broker_trading212.time.sleep") as msleep, \
         patch("switching.broker_trading212.time.monotonic", return_value=0.0):
        client._get("/equity/positions")
    # The backoff sleep (5s) should appear among the sleep calls
    waited_values = [c.args[0] for c in msleep.call_args_list]
    assert any(w >= 5.0 for w in waited_values)


def test_rate_limit_error_is_order_error_subclass():
    """Existing 'except T212OrderError' handlers must still catch rate-limit errors."""
    assert issubclass(T212RateLimitError, T212OrderError)
