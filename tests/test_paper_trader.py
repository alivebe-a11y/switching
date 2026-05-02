"""Tests for the paper-trading engine — specifically calendar-day hold logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from switching.paper_trader import (
    ClosedTrade,
    Portfolio,
    Position,
    _calendar_days_since,
    _tiered_stop_loss,
    check_exits,
)


def _make_position(entry_dt: datetime, **overrides) -> Position:
    defaults = dict(
        ticker="TEST",
        detector="ai_pivot",
        entry_price=100.0,
        shares=2.0,
        entry_dt=entry_dt.isoformat(),
        headline="Test headline",
        severity=0.8,
        stop_loss=0.05,
        hold_days=5,
        days_held=0,
        first_green=True,
    )
    defaults.update(overrides)
    return Position(**defaults)


class TestCalendarDaysSince:
    def test_same_day_returns_zero(self):
        now = datetime.now(tz=timezone.utc)
        assert _calendar_days_since(now.isoformat()) == 0

    def test_one_day_ago(self):
        yesterday = datetime.now(tz=timezone.utc) - timedelta(days=1)
        assert _calendar_days_since(yesterday.isoformat()) == 1

    def test_five_days_ago(self):
        past = datetime.now(tz=timezone.utc) - timedelta(days=5)
        assert _calendar_days_since(past.isoformat()) == 5

    def test_handles_z_suffix(self):
        now = datetime.now(tz=timezone.utc)
        iso_z = now.isoformat().replace("+00:00", "Z")
        assert _calendar_days_since(iso_z) == 0

    def test_naive_datetime_assumed_utc(self):
        past = (datetime.now(tz=timezone.utc) - timedelta(days=2)).replace(tzinfo=None)
        assert _calendar_days_since(past.isoformat()) in (1, 2)


class TestCheckExits:
    def test_does_not_close_after_just_scan_cycles(self):
        """Regression: days_held was incrementing per scan, not per day."""
        now = datetime.now(tz=timezone.utc)
        portfolio = Portfolio(
            cash=800.0,
            positions=[_make_position(now, hold_days=5, first_green=False)],
        )
        with patch(
            "switching.paper_trader.get_intraday_data",
            return_value={"open": 100.0, "high": 99.5, "low": 99.0, "close": 99.5},
        ):
            for _ in range(10):
                closed = check_exits(portfolio)
                assert closed == []
        assert len(portfolio.positions) == 1

    def test_closes_on_calendar_day_expiry(self):
        six_days_ago = datetime.now(tz=timezone.utc) - timedelta(days=6)
        portfolio = Portfolio(
            cash=800.0,
            positions=[_make_position(six_days_ago, hold_days=5, first_green=False)],
        )
        with patch(
            "switching.paper_trader.get_intraday_data",
            return_value={"open": 100.0, "high": 99.5, "low": 99.0, "close": 99.5},
        ):
            closed = check_exits(portfolio)
        assert len(closed) == 1
        assert closed[0].exit_reason == "hold_expiry"
        assert len(portfolio.positions) == 0

    def test_closes_on_stop_loss_regardless_of_days(self):
        now = datetime.now(tz=timezone.utc)
        portfolio = Portfolio(
            cash=800.0,
            positions=[_make_position(now, stop_loss=0.05)],
        )
        with patch(
            "switching.paper_trader.get_intraday_data",
            return_value={"open": 100.0, "high": 100.0, "low": 94.0, "close": 94.5},
        ):
            closed = check_exits(portfolio)
        assert len(closed) == 1
        assert closed[0].exit_reason == "stop_loss"

    def test_closes_on_first_green(self):
        now = datetime.now(tz=timezone.utc)
        portfolio = Portfolio(
            cash=800.0,
            positions=[_make_position(now, first_green=True)],
        )
        with patch(
            "switching.paper_trader.get_intraday_data",
            return_value={"open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0},
        ):
            closed = check_exits(portfolio)
        assert len(closed) == 1
        assert closed[0].exit_reason == "first_green"
        assert closed[0].pnl > 0

    def test_updates_days_held_on_open_position(self):
        three_days_ago = datetime.now(tz=timezone.utc) - timedelta(days=3)
        portfolio = Portfolio(
            cash=800.0,
            positions=[_make_position(three_days_ago, hold_days=5, first_green=False)],
        )
        with patch(
            "switching.paper_trader.get_intraday_data",
            return_value={"open": 100.0, "high": 99.5, "low": 99.0, "close": 99.5},
        ):
            check_exits(portfolio)
        assert portfolio.positions[0].days_held == 3


class TestTieredStopLoss:
    def test_large_cap_uses_base(self):
        assert _tiered_stop_loss(0.026, 150.0) == 0.026

    def test_mid_price_adds_1pct(self):
        assert abs(_tiered_stop_loss(0.026, 15.0) - 0.036) < 0.0001

    def test_penny_stock_adds_2pct(self):
        assert abs(_tiered_stop_loss(0.026, 2.50) - 0.046) < 0.0001

    def test_boundary_30_is_tight(self):
        assert _tiered_stop_loss(0.026, 30.0) == 0.026

    def test_boundary_5_is_mid(self):
        assert abs(_tiered_stop_loss(0.026, 5.0) - 0.036) < 0.0001

    def test_boundary_below_5_is_wide(self):
        assert abs(_tiered_stop_loss(0.026, 4.99) - 0.046) < 0.0001
