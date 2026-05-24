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
    _T212_REBUY_COOLDOWN_HOURS,
    _T212_SETTLE_MINUTES,
    _calendar_days_since,
    _exit_profile,
    _make_edgar_client,
    _minutes_since,
    _prune_recently_sold,
    _signal_key,
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
        with patch("switching.paper_trader.get_intraday_data",
                   return_value={"open": 100.0, "high": 99.5, "low": 99.0, "close": 99.5}), \
             patch("switching.paper_trader.is_market_hours", return_value=True), \
             patch("switching.paper_trader.trading_days_since", return_value=0):
            for _ in range(10):
                closed = check_exits(portfolio)
                assert closed == []
        assert len(portfolio.positions) == 1

    def test_closes_on_hold_expiry_after_trading_days(self):
        """hold_expiry fires when trading_days_since >= hold_days during market hours."""
        now = datetime.now(tz=timezone.utc)
        portfolio = Portfolio(
            cash=800.0,
            positions=[_make_position(now, hold_days=5, first_green=False)],
        )
        with patch("switching.paper_trader.get_intraday_data",
                   return_value={"open": 100.0, "high": 99.5, "low": 99.0, "close": 99.5}), \
             patch("switching.paper_trader.is_market_hours", return_value=True), \
             patch("switching.paper_trader.trading_days_since", return_value=6):
            closed = check_exits(portfolio)
        assert len(closed) == 1
        assert closed[0].exit_reason == "hold_expiry"
        assert len(portfolio.positions) == 0

    def test_hold_expiry_does_not_fire_outside_market_hours(self):
        """hold_expiry must NOT fire on weekends / bank holidays."""
        now = datetime.now(tz=timezone.utc)
        portfolio = Portfolio(
            cash=800.0,
            positions=[_make_position(now, hold_days=5, first_green=False)],
        )
        with patch("switching.paper_trader.get_intraday_data",
                   return_value={"open": 100.0, "high": 99.5, "low": 99.0, "close": 99.5}), \
             patch("switching.paper_trader.is_market_hours", return_value=False), \
             patch("switching.paper_trader.trading_days_since", return_value=6):
            closed = check_exits(portfolio)
        # Market closed — hold_expiry should NOT fire even though days elapsed
        assert closed == []

    def test_closes_on_stop_loss_regardless_of_market_hours(self):
        """Stop-loss fires even on weekends (last known price, defensive exit)."""
        now = datetime.now(tz=timezone.utc)
        portfolio = Portfolio(
            cash=800.0,
            positions=[_make_position(now, stop_loss=0.05)],
        )
        with patch("switching.paper_trader.get_intraday_data",
                   return_value={"open": 100.0, "high": 100.0, "low": 94.0, "close": 94.5}), \
             patch("switching.paper_trader.is_market_hours", return_value=False), \
             patch("switching.paper_trader.trading_days_since", return_value=0):
            closed = check_exits(portfolio)
        assert len(closed) == 1
        assert closed[0].exit_reason == "stop_loss"

    def test_closes_on_first_green(self):
        now = datetime.now(tz=timezone.utc)
        portfolio = Portfolio(
            cash=800.0,
            positions=[_make_position(now, first_green=True)],
        )
        with patch("switching.paper_trader.get_intraday_data",
                   return_value={"open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0}), \
             patch("switching.paper_trader.is_market_hours", return_value=True), \
             patch("switching.paper_trader.trading_days_since", return_value=1):
            closed = check_exits(portfolio)
        assert len(closed) == 1
        assert closed[0].exit_reason == "first_green"
        assert closed[0].pnl > 0

    def test_first_green_does_not_fire_outside_market_hours(self):
        now = datetime.now(tz=timezone.utc)
        portfolio = Portfolio(
            cash=800.0,
            positions=[_make_position(now, first_green=True)],
        )
        with patch("switching.paper_trader.get_intraday_data",
                   return_value={"open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0}), \
             patch("switching.paper_trader.is_market_hours", return_value=False), \
             patch("switching.paper_trader.trading_days_since", return_value=1):
            closed = check_exits(portfolio)
        assert closed == []

    def test_updates_days_held_on_open_position(self):
        now = datetime.now(tz=timezone.utc)
        portfolio = Portfolio(
            cash=800.0,
            positions=[_make_position(now, hold_days=5, first_green=False)],
        )
        with patch("switching.paper_trader.get_intraday_data",
                   return_value={"open": 100.0, "high": 99.5, "low": 99.0, "close": 99.5}), \
             patch("switching.paper_trader.is_market_hours", return_value=True), \
             patch("switching.paper_trader.trading_days_since", return_value=3):
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
        assert p["hold_days"] == 3  # raised from 2 — live data showed drift continues

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

    def test_guidance_raise_raised_threshold(self):
        p = _exit_profile("guidance_raise", 100.0)
        assert p["first_green_pct"] == 0.05   # raised from 0.02 — live data
        assert p["hold_days"] == 5             # raised from 3

    def test_dividend_surprise_widens_stop(self):
        p = _exit_profile("dividend_surprise", 10.0)
        assert p["stop_loss_extra"] == 0.01   # absorbs day-0 intraday noise

    def test_dividend_surprise_stop_applied_in_open_position(self):
        """Dividend surprise position should have a wider effective stop-loss."""
        from switching.signal import Signal
        portfolio = Portfolio(cash=1000.0)
        sig = Signal(
            detector="dividend_surprise", ticker="DIV",
            company="Div Co", event_dt=__import__("datetime").datetime.now(
                tz=__import__("datetime").timezone.utc),
            headline="Div headline", url="", evidence="test", severity=0.7,
        )
        pos = open_position(portfolio, sig, 15.0, stop_loss=0.026)
        assert pos is not None
        # tiered stop for $15 = 3.6%, plus stop_loss_extra 1% = 4.6%
        assert abs(pos.stop_loss - 0.046) < 0.001

    def test_earnings_surprise_first_green_2pct(self):
        p = _exit_profile("earnings_surprise", 100.0)
        assert p["first_green_pct"] == 0.02   # raised from 0.005 — live data (SNEX)


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

    def test_earnings_surprise_gets_3day_hold(self):
        portfolio = Portfolio(cash=1000.0)
        sig = self._make_signal("earnings_surprise")
        pos = open_position(portfolio, sig, 100.0)
        assert pos is not None
        assert pos.hold_days == 3  # raised from 2 — live data showed drift continues
        assert pos.first_green is True

    def test_first_green_blocked_on_entry_day(self):
        """First-green must not fire on the same calendar day as entry."""
        now = datetime.now(tz=timezone.utc)
        portfolio = Portfolio(
            cash=800.0,
            positions=[_make_position(now, first_green=True, first_green_pct=0.0)],
        )
        with patch(
            "switching.paper_trader.get_intraday_data",
            return_value={"open": 100.0, "high": 103.0, "low": 99.5, "close": 102.0},
        ):
            closed = check_exits(portfolio)
        assert closed == []
        assert len(portfolio.positions) == 1

    def test_price_floor_rejects_penny_stock(self):
        """Stocks under $1.00 should be rejected."""
        portfolio = Portfolio(cash=1000.0)
        sig = self._make_signal("ai_pivot")
        pos = open_position(portfolio, sig, 0.50)
        assert pos is None
        assert portfolio.cash == 1000.0

    def test_first_green_pct_prevents_early_exit(self):
        """A +1% return should NOT trigger first_green when threshold is 2%."""
        yesterday = datetime.now(tz=timezone.utc) - timedelta(days=1)
        portfolio = Portfolio(
            cash=800.0,
            positions=[_make_position(yesterday, first_green=True, first_green_pct=0.02)],
        )
        with patch("switching.paper_trader.get_intraday_data",
                   return_value={"open": 100.0, "high": 101.5, "low": 99.5, "close": 101.0}), \
             patch("switching.paper_trader.is_market_hours", return_value=True), \
             patch("switching.paper_trader.trading_days_since", return_value=1):
            closed = check_exits(portfolio)
        assert closed == []
        assert len(portfolio.positions) == 1

    def test_first_green_pct_triggers_at_threshold(self):
        """A +2% return SHOULD trigger first_green when threshold is 2%."""
        yesterday = datetime.now(tz=timezone.utc) - timedelta(days=1)
        portfolio = Portfolio(
            cash=800.0,
            positions=[_make_position(yesterday, first_green=True, first_green_pct=0.02)],
        )
        with patch("switching.paper_trader.get_intraday_data",
                   return_value={"open": 100.0, "high": 103.0, "low": 99.5, "close": 102.0}), \
             patch("switching.paper_trader.is_market_hours", return_value=True), \
             patch("switching.paper_trader.trading_days_since", return_value=1):
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


class TestSignalKey:
    """_signal_key must produce stable, content-based identifiers."""

    def _make_signal(self, **overrides):
        from switching.signal import Signal
        defaults = dict(
            detector="analyst_upgrade",
            ticker="NVDA",
            company="Nvidia",
            event_dt=datetime.now(tz=timezone.utc),
            headline="Analyst upgrades NVDA to Buy",
            url="https://example.com/nvda-upgrade",
            evidence="upgrades NVDA",
            severity=0.75,
        )
        defaults.update(overrides)
        return Signal(**defaults)

    def test_url_based_key_is_stable_across_dates(self):
        """Same URL → same key, even if event_dt shifts to a new day."""
        sig_day1 = self._make_signal(
            event_dt=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
        )
        sig_day2 = self._make_signal(
            event_dt=datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc),
        )
        assert _signal_key(sig_day1) == _signal_key(sig_day2)

    def test_different_urls_give_different_keys(self):
        sig1 = self._make_signal(url="https://example.com/article1")
        sig2 = self._make_signal(url="https://example.com/article2")
        assert _signal_key(sig1) != _signal_key(sig2)

    def test_different_detectors_give_different_keys(self):
        sig1 = self._make_signal(detector="analyst_upgrade")
        sig2 = self._make_signal(detector="earnings_surprise")
        assert _signal_key(sig1) != _signal_key(sig2)

    def test_different_tickers_give_different_keys(self):
        sig1 = self._make_signal(ticker="NVDA")
        sig2 = self._make_signal(ticker="AAPL")
        assert _signal_key(sig1) != _signal_key(sig2)

    def test_no_url_falls_back_to_headline_hash(self):
        """When URL is empty, key is derived from headline (not from shifting date)."""
        sig_day1 = self._make_signal(
            url="",
            event_dt=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
        )
        sig_day2 = self._make_signal(
            url="",
            event_dt=datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc),
        )
        assert _signal_key(sig_day1) == _signal_key(sig_day2)

    def test_no_url_different_headline_different_key(self):
        sig1 = self._make_signal(url="", headline="Analyst upgrades NVDA to Buy")
        sig2 = self._make_signal(url="", headline="Analyst upgrades NVDA to Strong Buy")
        assert _signal_key(sig1) != _signal_key(sig2)


class TestMinutesSince:
    def test_none_for_empty(self):
        now = datetime.now(tz=timezone.utc)
        assert _minutes_since("", now) is None

    def test_none_for_garbage(self):
        now = datetime.now(tz=timezone.utc)
        assert _minutes_since("not-a-date", now) is None

    def test_computes_minutes(self):
        now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
        ten_min_ago = datetime(2026, 5, 24, 11, 50, tzinfo=timezone.utc)
        assert abs(_minutes_since(ten_min_ago.isoformat(), now) - 10.0) < 0.01

    def test_handles_naive_timestamp_as_utc(self):
        now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
        naive = datetime(2026, 5, 24, 11, 0)  # no tzinfo
        assert abs(_minutes_since(naive.isoformat(), now) - 60.0) < 0.01

    def test_handles_z_suffix(self):
        now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
        z_ts = "2026-05-24T11:30:00Z"
        assert abs(_minutes_since(z_ts, now) - 30.0) < 0.01


class TestPruneRecentlySold:
    def test_removes_stale_entries(self):
        now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
        old = (now - timedelta(hours=_T212_REBUY_COOLDOWN_HOURS + 1)).isoformat()
        fresh = (now - timedelta(minutes=5)).isoformat()
        rs = {"OLD": old, "FRESH": fresh}
        _prune_recently_sold(rs, now)
        assert "OLD" not in rs
        assert "FRESH" in rs

    def test_removes_unparseable(self):
        now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
        rs = {"BAD": "garbage", "GOOD": now.isoformat()}
        _prune_recently_sold(rs, now)
        assert "BAD" not in rs
        assert "GOOD" in rs

    def test_keeps_all_when_fresh(self):
        now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
        rs = {
            "A": (now - timedelta(minutes=1)).isoformat(),
            "B": (now - timedelta(hours=1)).isoformat(),
        }
        _prune_recently_sold(rs, now)
        assert len(rs) == 2

    def test_settle_window_shorter_than_cooldown(self):
        """Sanity: the settlement window must be shorter than the re-buy cooldown."""
        assert _T212_SETTLE_MINUTES < _T212_REBUY_COOLDOWN_HOURS * 60


class TestPortfolioRecentlySoldPersistence:
    def test_round_trips_through_save_load(self, tmp_path):
        from pathlib import Path
        p = Portfolio(cash=1000.0)
        p.recently_sold = {"NVDA": "2026-05-24T12:00:00+00:00"}
        path = tmp_path / "t212_portfolio.json"
        p.save(path)
        loaded = Portfolio.load(path)
        assert loaded.recently_sold == {"NVDA": "2026-05-24T12:00:00+00:00"}

    def test_defaults_empty_when_absent(self, tmp_path):
        import json
        path = tmp_path / "old_portfolio.json"
        # Simulate an older state file with no recently_sold key
        path.write_text(json.dumps({"cash": 500.0}), encoding="utf-8")
        loaded = Portfolio.load(path)
        assert loaded.recently_sold == {}
