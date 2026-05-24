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
    from datetime import datetime, timezone
    # Wednesday 15:00 UTC = market open
    fixed = datetime(2026, 5, 13, 15, 0, 0, tzinfo=timezone.utc)
    with patch("switching.broker_trading212.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert client.is_market_open() is True


def test_market_closed_weekend(monkeypatch, client):
    from datetime import datetime, timezone
    # Saturday 15:00 UTC
    fixed = datetime(2026, 5, 16, 15, 0, 0, tzinfo=timezone.utc)
    with patch("switching.broker_trading212.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert client.is_market_open() is False


def test_market_closed_after_hours(monkeypatch, client):
    from datetime import datetime, timezone
    # Wednesday 22:00 UTC = after close
    fixed = datetime(2026, 5, 13, 22, 0, 0, tzinfo=timezone.utc)
    with patch("switching.broker_trading212.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert client.is_market_open() is False


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
