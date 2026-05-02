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
    _EDGAR_DETECTORS,
    _calendar_days_since,
    _exit_profile,
    _make_edgar_client,
    _tiered_stop_loss,
    check_exits,
    open_position,
    scan_for_signals,
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


class TestExitProfile:
    def test_buyback_no_first_green(self):
        p = _exit_profile("buyback", 50.0)
        assert p["first_green"] is False
        assert p["hold_days"] == 5

    def test_earnings_surprise_short_hold(self):
        p = _exit_profile("earnings_surprise", 100.0)
        assert p["first_green"] is True
        assert p["hold_days"] == 2

    def test_ai_pivot_large_cap_needs_2pct(self):
        p = _exit_profile("ai_pivot", 50.0)
        assert p["first_green"] is True
        assert p["first_green_pct"] == 0.02
        assert p["hold_days"] == 5

    def test_ai_pivot_penny_stock_quick_exit(self):
        p = _exit_profile("ai_pivot", 10.0)
        assert p["first_green"] is True
        assert p["first_green_pct"] == 0.0
        assert p["hold_days"] == 3

    def test_ai_pivot_boundary_30(self):
        p = _exit_profile("ai_pivot", 30.0)
        assert p["first_green_pct"] == 0.02

    def test_ai_pivot_below_30(self):
        p = _exit_profile("ai_pivot", 29.99)
        assert p["first_green_pct"] == 0.0
        assert p["hold_days"] == 3

    def test_unknown_detector_defaults(self):
        p = _exit_profile("some_future_detector", 100.0)
        assert p["first_green"] is True
        assert p["first_green_pct"] == 0.0
        assert p["hold_days"] == 5


class TestOpenPositionProfiles:
    def _make_signal(self, detector: str, ticker: str = "TEST"):
        from switching.signal import Signal
        return Signal(
            detector=detector,
            ticker=ticker,
            company="Test Corp",
            event_dt=datetime.now(tz=timezone.utc),
            headline="Test headline",
            url="",
            evidence="test",
            severity=0.8,
        )

    def test_buyback_position_no_first_green(self):
        portfolio = Portfolio(cash=1000.0)
        sig = self._make_signal("buyback")
        pos = open_position(portfolio, sig, 50.0)
        assert pos is not None
        assert pos.first_green is False
        assert pos.hold_days == 5

    def test_ai_pivot_large_cap_gets_2pct_threshold(self):
        portfolio = Portfolio(cash=1000.0)
        sig = self._make_signal("ai_pivot")
        pos = open_position(portfolio, sig, 50.0)
        assert pos is not None
        assert pos.first_green is True
        assert pos.first_green_pct == 0.02

    def test_earnings_surprise_gets_2day_hold(self):
        portfolio = Portfolio(cash=1000.0)
        sig = self._make_signal("earnings_surprise")
        pos = open_position(portfolio, sig, 100.0)
        assert pos is not None
        assert pos.hold_days == 2
        assert pos.first_green is True

    def test_first_green_pct_prevents_early_exit(self):
        """A +1% return should NOT trigger first_green when threshold is 2%."""
        now = datetime.now(tz=timezone.utc)
        portfolio = Portfolio(
            cash=800.0,
            positions=[_make_position(now, first_green=True, first_green_pct=0.02)],
        )
        with patch(
            "switching.paper_trader.get_intraday_data",
            return_value={"open": 100.0, "high": 101.5, "low": 99.5, "close": 101.0},
        ):
            closed = check_exits(portfolio)
        assert closed == []
        assert len(portfolio.positions) == 1

    def test_first_green_pct_triggers_at_threshold(self):
        """A +2% return SHOULD trigger first_green when threshold is 2%."""
        now = datetime.now(tz=timezone.utc)
        portfolio = Portfolio(
            cash=800.0,
            positions=[_make_position(now, first_green=True, first_green_pct=0.02)],
        )
        with patch(
            "switching.paper_trader.get_intraday_data",
            return_value={"open": 100.0, "high": 103.0, "low": 99.5, "close": 102.0},
        ):
            closed = check_exits(portfolio)
        assert len(closed) == 1
        assert closed[0].exit_reason == "first_green"


class TestScanForSignals:
    def test_edgar_client_created_when_env_var_set(self):
        with patch.dict("os.environ", {"SWITCHING_EDGAR_UA": "test agent"}):
            client = _make_edgar_client()
        assert client is not None

    def test_edgar_client_none_without_env_var(self):
        with patch.dict("os.environ", {}, clear=True):
            client = _make_edgar_client()
        assert client is None

    def test_edgar_detectors_set_is_correct(self):
        assert "activist_13d" in _EDGAR_DETECTORS
        assert "insider_cluster" in _EDGAR_DETECTORS
        assert "ai_pivot" not in _EDGAR_DETECTORS

    def test_scan_passes_client_to_edgar_detector(self):
        """Verify activist_13d gets an EdgarClient instead of bare cls()."""
        with patch("switching.paper_trader._make_edgar_client") as mock_make, \
             patch("switching.registry.get") as mock_get:
            mock_make.return_value = None
            mock_cls = mock_get.return_value
            mock_cls.return_value.scan.return_value = iter([])
            scan_for_signals(["activist_13d"], datetime.now(tz=timezone.utc))
            mock_cls.assert_called_once_with(client=None)

    def test_scan_no_client_for_rss_detector(self):
        """ai_pivot should be instantiated with no args."""
        with patch("switching.registry.get") as mock_get:
            mock_cls = mock_get.return_value
            mock_cls.return_value.scan.return_value = iter([])
            scan_for_signals(["ai_pivot"], datetime.now(tz=timezone.utc))
            mock_cls.assert_called_once_with()
