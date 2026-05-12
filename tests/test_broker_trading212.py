"""Tests for broker_trading212.py — unit tests using mocked HTTP."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from switching.broker_trading212 import (
    T212AuthError,
    T212OrderError,
    Trading212Client,
    _from_t212_ticker,
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


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def test_missing_api_key_raises():
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("T212_API_KEY", None)
        with pytest.raises(T212AuthError, match="T212_API_KEY"):
            Trading212Client()


def test_demo_default(monkeypatch):
    monkeypatch.setenv("T212_API_KEY", "test-key")
    monkeypatch.delenv("T212_DEMO", raising=False)
    client = Trading212Client()
    assert client.demo is True
    assert "demo" in client._base


def test_live_mode(monkeypatch):
    monkeypatch.setenv("T212_API_KEY", "test-key")
    monkeypatch.setenv("T212_DEMO", "false")
    client = Trading212Client()
    assert client.demo is False
    assert "live" in client._base


def test_demo_true_explicit(monkeypatch):
    monkeypatch.setenv("T212_API_KEY", "test-key")
    monkeypatch.setenv("T212_DEMO", "true")
    client = Trading212Client()
    assert client.demo is True


# ---------------------------------------------------------------------------
# Fixture: patched client with mocked session
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("T212_API_KEY", "fake-key")
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
