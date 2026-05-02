"""Tests for the web dashboard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from switching.paper_trader import ClosedTrade, Portfolio, Position
from switching.web import create_app


@pytest.fixture()
def portfolio_path(tmp_path: Path) -> Path:
    return tmp_path / "portfolio.json"


@pytest.fixture()
def app(portfolio_path: Path):
    flask_app = create_app(state_path=portfolio_path)
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


def _save_portfolio(path: Path, **kwargs) -> Portfolio:
    p = Portfolio(**kwargs)
    p.save(path)
    return p


class TestDashboard:
    def test_index_returns_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert b"Switching" in r.data
        assert b"Paper Trading" in r.data

    def test_portfolio_empty(self, client, portfolio_path):
        _save_portfolio(portfolio_path, cash=1000.0)
        r = client.get("/api/portfolio")
        assert r.status_code == 200
        data = r.get_json()
        assert data["cash"] == 1000.0
        assert data["open_count"] == 0
        assert data["trade_count"] == 0

    def test_portfolio_with_positions(self, client, portfolio_path):
        _save_portfolio(
            portfolio_path,
            cash=800.0,
            positions=[
                Position(
                    ticker="AAPL", detector="ai_pivot",
                    entry_price=150.0, shares=1.33,
                    entry_dt="2024-01-01T00:00:00Z",
                    headline="Apple pivots to AI",
                    severity=0.85, stop_loss=0.05, hold_days=5,
                    days_held=2,
                ),
            ],
        )
        r = client.get("/api/portfolio")
        data = r.get_json()
        assert data["open_count"] == 1
        assert data["positions"][0]["ticker"] == "AAPL"
        assert data["positions"][0]["entry_price"] == 150.0
        assert data["positions"][0]["days_held"] == 2

    def test_trades_empty(self, client, portfolio_path):
        _save_portfolio(portfolio_path)
        r = client.get("/api/trades")
        data = r.get_json()
        assert data["trades"] == []

    def test_trades_with_history(self, client, portfolio_path):
        _save_portfolio(
            portfolio_path,
            cash=1050.0,
            trades=[
                ClosedTrade(
                    ticker="MSFT", detector="buyback",
                    entry_price=300.0, exit_price=315.0,
                    shares=0.66, entry_dt="2024-01-01T00:00:00Z",
                    exit_dt="2024-01-06T00:00:00Z",
                    pnl=9.90, pct_return=0.05,
                    exit_reason="first_green",
                    headline="Microsoft buyback",
                ),
                ClosedTrade(
                    ticker="GOOG", detector="earnings_surprise",
                    entry_price=140.0, exit_price=135.0,
                    shares=1.0, entry_dt="2024-02-01T00:00:00Z",
                    exit_dt="2024-02-06T00:00:00Z",
                    pnl=-5.0, pct_return=-0.0357,
                    exit_reason="stop_loss",
                    headline="Google misses earnings",
                ),
            ],
        )
        r = client.get("/api/trades")
        data = r.get_json()
        assert len(data["trades"]) == 2
        assert data["trades"][0]["ticker"] == "GOOG"
        assert data["trades"][1]["ticker"] == "MSFT"

    def test_portfolio_win_rate(self, client, portfolio_path):
        _save_portfolio(
            portfolio_path,
            cash=1050.0,
            trades=[
                ClosedTrade(
                    ticker="A", detector="x",
                    entry_price=10.0, exit_price=11.0, shares=1.0,
                    entry_dt="2024-01-01", exit_dt="2024-01-05",
                    pnl=1.0, pct_return=0.10,
                    exit_reason="first_green", headline="win",
                ),
                ClosedTrade(
                    ticker="B", detector="x",
                    entry_price=10.0, exit_price=9.0, shares=1.0,
                    entry_dt="2024-01-01", exit_dt="2024-01-05",
                    pnl=-1.0, pct_return=-0.10,
                    exit_reason="stop_loss", headline="loss",
                ),
                ClosedTrade(
                    ticker="C", detector="x",
                    entry_price=10.0, exit_price=10.5, shares=1.0,
                    entry_dt="2024-01-01", exit_dt="2024-01-05",
                    pnl=0.5, pct_return=0.05,
                    exit_reason="hold", headline="win2",
                ),
            ],
        )
        r = client.get("/api/portfolio")
        data = r.get_json()
        assert data["wins"] == 2
        assert data["trade_count"] == 3
        assert abs(data["win_rate"] - 66.67) < 1

    def test_equity_curve_empty(self, client, portfolio_path):
        _save_portfolio(portfolio_path)
        r = client.get("/api/equity-curve")
        data = r.get_json()
        assert data["points"] == []

    def test_equity_curve_with_trades(self, client, portfolio_path):
        _save_portfolio(
            portfolio_path,
            cash=1010.0,
            trades=[
                ClosedTrade(
                    ticker="X", detector="d",
                    entry_price=100.0, exit_price=110.0, shares=1.0,
                    entry_dt="2024-01-01", exit_dt="2024-01-05",
                    pnl=10.0, pct_return=0.10,
                    exit_reason="first_green", headline="h",
                ),
            ],
        )
        r = client.get("/api/equity-curve")
        data = r.get_json()
        assert len(data["points"]) == 2
        assert data["points"][0]["value"] == 1000.0
        assert data["points"][1]["value"] == 1010.0

    def test_signals_empty(self, client, portfolio_path):
        _save_portfolio(portfolio_path)
        r = client.get("/api/signals")
        data = r.get_json()
        assert data["signals"] == []

    def test_signals_from_state(self, client, portfolio_path):
        _save_portfolio(
            portfolio_path,
            last_signals=[
                {"ticker": "AAPL", "detector": "ai_pivot", "severity": 0.85,
                 "headline": "Apple AI", "company": "Apple", "event_dt": "2024-01-01",
                 "url": "", "evidence": "", "extra": {}, "price_reaction": None},
            ],
            last_scan_dt="2024-01-01T12:00:00+00:00",
        )
        r = client.get("/api/signals")
        data = r.get_json()
        assert len(data["signals"]) == 1
        assert data["signals"][0]["ticker"] == "AAPL"
        assert data["scanned_at"] == "2024-01-01T12:00:00+00:00"

    def test_no_state_file(self, client):
        r = client.get("/api/portfolio")
        assert r.status_code == 200
        data = r.get_json()
        assert data["cash"] == 1000.0
