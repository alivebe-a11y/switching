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
    _MAX_SINGLE_POSITION_PCT,
    _check_peak_exits_only,
    _make_edgar_client,
    _minutes_since,
    _position_weight,
    _prune_recently_sold,
    _reconcile_t212_ghosts,
    _should_send_weekly_report,
    _signal_key,
    _tiered_stop_loss,
    check_exits,
    open_position,
    scan_for_signals,
)


def _make_position(entry_dt: datetime, **overrides) -> Position:
    defaults = dict(
        ticker="TEST",
        # analyst_upgrade = a simple first-green detector (no "ride mode"), so the
        # generic exit-mechanics tests below exercise the first_green/stop/hold
        # paths. Detector-specific behaviour is tested explicitly elsewhere.
        detector="analyst_upgrade",
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

    def test_neutralizes_implausible_return_as_data_error(self):
        """Bad price data (e.g. yfinance GBp/GBP flip) must NOT book a fake huge win —
        it's scratched to a zero-P&L data_error close + an alert is fired."""
        now = datetime.now(tz=timezone.utc)
        portfolio = Portfolio(cash=800.0, positions=[_make_position(now, first_green=True)])
        with patch("switching.paper_trader.get_intraday_data",
                   return_value={"open": 100000.0, "high": 100000.0, "low": 100000.0, "close": 100000.0}), \
             patch("switching.paper_trader.is_market_hours", return_value=True), \
             patch("switching.paper_trader.trading_days_since", return_value=2), \
             patch("switching.notifications.notify_alert") as notify:
            closed = check_exits(portfolio)
        assert len(closed) == 1
        t = closed[0]
        assert t.exit_reason == "data_error"
        assert t.pnl == 0.0
        assert t.pct_return == 0.0
        assert t.exit_price == 100.0          # scratched back to entry
        assert len(portfolio.positions) == 0
        notify.assert_called_once()            # loud alert fired

    def test_plausible_move_books_normally(self):
        """A normal-sized move is unaffected by the bad-data guard."""
        now = datetime.now(tz=timezone.utc)
        portfolio = Portfolio(cash=800.0, positions=[_make_position(now, first_green=True)])
        with patch("switching.paper_trader.get_intraday_data",
                   return_value={"open": 110.0, "high": 110.0, "low": 110.0, "close": 110.0}), \
             patch("switching.paper_trader.is_market_hours", return_value=True), \
             patch("switching.paper_trader.trading_days_since", return_value=2):
            closed = check_exits(portfolio)
        assert len(closed) == 1
        assert closed[0].exit_reason == "first_green"
        assert abs(closed[0].pct_return - 0.10) < 1e-9   # +10%, booked normally

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
        assert p["hold_days"] == 8           # extended for ride mode
        assert p["ride"] is True
        assert p["trail_pct"] == 0.03

    def test_ai_pivot_penny_stock_quick_exit(self):
        p = _exit_profile("ai_pivot", 10.0)
        assert p["first_green"] is True
        assert p["first_green_pct"] == 0.01  # small confirmation before riding
        assert p["hold_days"] == 6
        assert p["ride"] is True

    def test_ai_pivot_boundary_30(self):
        p = _exit_profile("ai_pivot", 30.0)
        assert p["first_green_pct"] == 0.02

    def test_ai_pivot_below_30(self):
        p = _exit_profile("ai_pivot", 29.99)
        assert p["first_green_pct"] == 0.01
        assert p["hold_days"] == 6

    def test_mna_target_rides(self):
        p = _exit_profile("mna_target", 50.0)
        assert p["ride"] is True
        assert p["trail_pct"] == 0.03
        assert p["hold_days"] == 8

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

    def test_uk_director_dealing_rides_not_scratches(self):
        # Was falling through to the 0% default and scratching at break-even.
        # Now ride mode with a real green threshold + wide LSE trail.
        p = _exit_profile("uk_director_dealing", 5.0)
        assert p["ride"] is True
        assert p["first_green_pct"] == 0.015   # no more 0% break-even scratches
        assert p["trail_pct"] == 0.04          # wider than US momentum (LSE noisier)
        assert p["hold_days"] == 6

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
        """US stocks under $1.00 should be rejected."""
        portfolio = Portfolio(cash=1000.0)
        sig = self._make_signal("ai_pivot")
        pos = open_position(portfolio, sig, 0.50)
        assert pos is None
        assert portfolio.cash == 1000.0

    def test_uk_has_no_price_floor(self):
        """UK penny stocks (AIM <£1) should be accepted — the floor was lifted
        2026-05-27 to collect data on whether the bot can trade them.

        yfinance returns LSE tickers in pence (GBX). A 50p stock arrives as
        price=50.0 and normalises to £0.50, which the old gate would reject.
        """
        portfolio = Portfolio(cash=1000.0)
        sig = self._make_signal("guidance_raise")
        # 50p (GBX = 50.0) → normalised £0.50 — below the old floor
        pos = open_position(portfolio, sig, 50.0, market="uk")
        assert pos is not None, "UK sub-£1 stock should now be tradeable"
        assert pos.entry_price == pytest.approx(0.50)

    def test_us_price_floor_still_active(self):
        """US sub-$1 stocks must still be rejected — the UK exemption is UK-only."""
        portfolio = Portfolio(cash=1000.0)
        sig = self._make_signal("ai_pivot")
        # $0.50 — below the US $1 floor (market="us" is the default)
        pos = open_position(portfolio, sig, 0.50, market="us")
        assert pos is None

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

    def test_scan_sets_market_during_scan_and_resets_after(self):
        """market='uk' must be the rss default while detectors run, then reset."""
        import switching.sources.rss as rss_mod
        seen = {}

        def _scan(since):
            seen["during"] = rss_mod._DEFAULT_MARKET
            return iter([])

        with patch("switching.registry.get") as mock_get:
            mock_cls = mock_get.return_value
            mock_cls.return_value.scan.side_effect = _scan
            scan_for_signals(["ai_pivot"], datetime.now(tz=timezone.utc), market="uk")
        assert seen["during"] == "uk"           # set while scanning
        assert rss_mod._DEFAULT_MARKET == "us"  # reset afterwards


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


class TestReconcileT212Ghosts:
    """Local positions T212 no longer reports = closed externally (corporate
    action / delisting / ticker change). They must be recorded + removed."""

    def _pf(self, *tickers):
        now = datetime.now(tz=timezone.utc)
        p = Portfolio(cash=1000.0)
        for t in tickers:
            p.positions.append(_make_position(now, ticker=t, detector="mna_target"))
        return p

    def test_held_position_kept(self):
        now = datetime.now(tz=timezone.utc)
        p = self._pf("NVDA")
        closed = _reconcile_t212_ghosts(p, {"NVDA"}, now)
        assert closed == []
        assert len(p.positions) == 1

    def test_external_close_recorded_and_removed(self):
        now = datetime.now(tz=timezone.utc)
        p = self._pf("CTRA")
        p.cached_prices["CTRA"] = 110.0   # last seen before T212 closed it
        closed = _reconcile_t212_ghosts(p, set(), now)   # T212 reports nothing
        assert len(closed) == 1
        assert closed[0].ticker == "CTRA"
        assert closed[0].exit_reason == "corporate_action"
        assert closed[0].exit_price == 110.0
        assert p.positions == []
        assert len(p.trades) == 1
        assert "CTRA" in p.recently_sold          # blocks immediate re-buy

    def test_uses_entry_price_when_no_cached_price(self):
        now = datetime.now(tz=timezone.utc)
        p = self._pf("XYZ")   # entry_price defaults to 100.0 in _make_position
        closed = _reconcile_t212_ghosts(p, set(), now)
        assert closed[0].exit_price == 100.0
        assert closed[0].pct_return == 0.0   # neutral placeholder, flagged by reason

    def test_settling_own_sell_is_not_reconciled(self):
        now = datetime.now(tz=timezone.utc)
        p = self._pf("AAPL")
        # We just sold AAPL ourselves 2 minutes ago — still settling, T212 may
        # still report it gone briefly; must NOT be treated as a corporate action.
        p.recently_sold["AAPL"] = (now - timedelta(minutes=2)).isoformat()
        closed = _reconcile_t212_ghosts(p, set(), now)
        assert closed == []
        assert len(p.positions) == 1

    def test_old_sell_past_settlement_is_reconciled(self):
        now = datetime.now(tz=timezone.utc)
        p = self._pf("AAPL")
        # Sold 30 min ago (past the 15-min settle window) yet still locally
        # tracked and absent from T212 -> genuine ghost, reconcile it.
        p.recently_sold["AAPL"] = (now - timedelta(minutes=30)).isoformat()
        closed = _reconcile_t212_ghosts(p, set(), now)
        assert len(closed) == 1 and p.positions == []

    def test_mixed_held_and_ghost(self):
        now = datetime.now(tz=timezone.utc)
        p = self._pf("KEEP", "GONE")
        closed = _reconcile_t212_ghosts(p, {"KEEP"}, now)
        assert [c.ticker for c in closed] == ["GONE"]
        assert [pos.ticker for pos in p.positions] == ["KEEP"]


class TestPositionWeight:
    def test_guidance_raise_sized_up(self):
        assert _position_weight("guidance_raise") == 7.0

    def test_solid_detectors_2x(self):
        assert _position_weight("dividend_surprise") == 2.0
        assert _position_weight("contract_win") == 2.0

    def test_unknown_and_weak_detectors_baseline(self):
        # Weak/fat-tail detectors are NOT sized down (would clip the tail).
        assert _position_weight("ai_pivot") == 1.0
        assert _position_weight("mna_target") == 1.0
        assert _position_weight("fda_decision") == 1.0
        assert _position_weight("some_new_detector") == 1.0


class TestConvictionSizing:
    def _sig(self, detector):
        from switching.signal import Signal
        return Signal(detector=detector, ticker="T", company="C",
                      event_dt=datetime.now(tz=timezone.utc), headline="h",
                      url="", evidence="e", severity=0.8)

    def test_weight_scales_allocation(self):
        # guidance_raise (7x) should buy ~7x the shares of a baseline detector
        # at the same price, all else equal.
        base = Portfolio(cash=100_000.0, max_position_pct=0.015)
        pos_base = open_position(base, self._sig("contract_win"), 50.0)  # 2x
        conv = Portfolio(cash=100_000.0, max_position_pct=0.015)
        pos_conv = open_position(conv, self._sig("guidance_raise"), 50.0)  # 7x
        ratio = pos_conv.shares / pos_base.shares
        assert abs(ratio - 3.5) < 0.01   # 7x / 2x

    def test_per_position_cap_enforced(self):
        # A huge weight can't exceed the per-position cap of the fund.
        p = Portfolio(cash=100_000.0, max_position_pct=0.015)
        pos = open_position(p, self._sig("guidance_raise"), 100.0)
        cost = pos.shares * pos.entry_price
        assert cost <= 100_000.0 * _MAX_SINGLE_POSITION_PCT + 1e-6

    def test_baseline_size(self):
        p = Portfolio(cash=100_000.0, max_position_pct=0.015)
        pos = open_position(p, self._sig("analyst_upgrade"), 50.0)
        cost = pos.shares * pos.entry_price
        assert abs(cost - 100_000.0 * 0.015) < 0.01   # 1.5% baseline, weight 1.0


class TestRideMode:
    """Momentum detectors flip into peak-tracking at first-green and ride
    toward the peak instead of taking the small first-green win."""

    def _mkt(self, **kw):
        # patch market open + trading days
        return patch.multiple(
            "switching.paper_trader",
            is_market_hours=lambda: True,
            **kw,
        )

    def test_ride_detector_does_not_exit_on_first_green(self):
        yesterday = datetime.now(tz=timezone.utc) - timedelta(days=1)
        p = Portfolio(cash=800.0, positions=[
            _make_position(yesterday, detector="mna_target", first_green=True,
                           first_green_pct=0.03)
        ])
        with patch("switching.paper_trader.get_intraday_data",
                   return_value={"open":100.0,"high":105.0,"low":99.5,"close":104.0}), \
             patch("switching.paper_trader.is_market_hours", return_value=True), \
             patch("switching.paper_trader.trading_days_since", return_value=1):
            closed = check_exits(p)
        # +4% on day 1 with first_green 3% -> would normally exit first_green,
        # but mna_target rides: no exit, now peak-tracking.
        assert closed == []
        assert len(p.positions) == 1
        assert p.positions[0].peak_tracking is True

    def test_ride_then_trailing_stop_exit(self):
        yesterday = datetime.now(tz=timezone.utc) - timedelta(days=1)
        pos = _make_position(yesterday, detector="mna_target", first_green=True,
                             first_green_pct=0.03, peak_tracking=True, peak_price=120.0)
        p = Portfolio(cash=800.0, positions=[pos])
        # price dropped >3% from the 120 peak (to 115 = -4.2%) -> peak_trailing exit
        with patch("switching.paper_trader.get_intraday_data",
                   return_value={"open":116.0,"high":116.0,"low":115.0,"close":115.0}), \
             patch("switching.paper_trader.is_market_hours", return_value=True), \
             patch("switching.paper_trader.trading_days_since", return_value=2):
            closed = check_exits(p)
        assert len(closed) == 1
        assert closed[0].exit_reason == "peak_trailing"

    def test_ride_holds_above_trail_band(self):
        yesterday = datetime.now(tz=timezone.utc) - timedelta(days=1)
        pos = _make_position(yesterday, detector="mna_target", first_green=True,
                             first_green_pct=0.03, peak_tracking=True, peak_price=120.0,
                             hold_days=8)
        p = Portfolio(cash=800.0, positions=[pos])
        # only -1% from peak (119), inside the 3% band, day 2 < 8 -> keep riding
        with patch("switching.paper_trader.get_intraday_data",
                   return_value={"open":119.0,"high":119.5,"low":118.5,"close":118.8}), \
             patch("switching.paper_trader.is_market_hours", return_value=True), \
             patch("switching.paper_trader.trading_days_since", return_value=2):
            closed = check_exits(p)
        assert closed == []
        assert len(p.positions) == 1


class TestPeakPollUKNormalisation:
    """`_check_peak_exits_only` must normalise UK (GBX/pence) prices to GBP
    before the trailing-stop maths. `get_current_price` returns raw yfinance
    pence for LSE tickers, while `peak_price`/`entry_price` are stored in GBP.
    Regression: without normalisation the pence price clobbered the GBP peak
    (~100x), recording a ~+9000% phantom trade or force-closing next cycle.
    """

    def test_uk_peak_exit_normalises_pence(self):
        # Entry £4.00, peak £4.30 (both GBP). yfinance returns 425.0 pence
        # (= £4.25), which is 1.16% below the £4.30 peak -> trailing exit.
        pos = _make_position(
            datetime.now(tz=timezone.utc) - timedelta(days=1),
            ticker="VOD.L", detector="mna_target", entry_price=4.0,
            shares=10.0, peak_tracking=True, peak_price=4.30,
        )
        p = Portfolio(cash=100.0, positions=[pos])
        with patch("switching.paper_trader.get_current_price", return_value=425.0):
            closed = _check_peak_exits_only(p, market="uk")
        assert len(closed) == 1
        t = closed[0]
        assert t.exit_reason == "peak_trailing"
        # Exit price is GBP (~£4.25), NOT raw pence (425) -> no 100x blow-up.
        assert t.exit_price == pytest.approx(4.25, abs=0.01)
        # Return is a sane single-digit %, not ~+10,000%.
        assert t.pct_return == pytest.approx(0.0625, abs=0.001)

    def test_uk_peak_updates_in_gbp_no_exit(self):
        # Price ticks UP to £4.40 (440 pence), a new high above the £4.30 peak,
        # so it keeps riding. Critically the peak must update to £4.40 (GBP),
        # NOT 440 (pence) — the pre-fix bug clobbered it to the raw pence value.
        pos = _make_position(
            datetime.now(tz=timezone.utc) - timedelta(days=1),
            ticker="VOD.L", detector="mna_target", entry_price=4.0,
            shares=10.0, peak_tracking=True, peak_price=4.30,
        )
        p = Portfolio(cash=100.0, positions=[pos])
        with patch("switching.paper_trader.get_current_price", return_value=440.0):
            closed = _check_peak_exits_only(p, market="uk")
        assert closed == []
        assert len(p.positions) == 1
        # New peak is in GBP units (£4.40), never clobbered to 440 pence.
        assert p.positions[0].peak_price == pytest.approx(4.40, abs=0.01)


class TestWeeklyReportSchedule:
    """The Saturday weekly report must fire exactly once per Saturday, at/after
    09:00 UTC, from the `us` paper loop only. Regression for the 'fires every
    hour' bug: the old dedup only stamped on Telegram success (so a flaky send
    re-fired every scan) and both us+uk loops sent it.
    """

    # 2026-05-30 is a Saturday (2026-05-29 was a Friday).
    SAT_0905 = datetime(2026, 5, 30, 9, 5, tzinfo=timezone.utc)
    SAT_1400 = datetime(2026, 5, 30, 14, 0, tzinfo=timezone.utc)
    SAT_0859 = datetime(2026, 5, 30, 8, 59, tzinfo=timezone.utc)
    FRI_0905 = datetime(2026, 5, 29, 9, 5, tzinfo=timezone.utc)

    def test_fires_saturday_morning_us_never_sent(self):
        assert _should_send_weekly_report(self.SAT_0905, "", "us") is True

    def test_not_before_0900(self):
        assert _should_send_weekly_report(self.SAT_0859, "", "us") is False

    def test_not_on_a_weekday(self):
        assert _should_send_weekly_report(self.FRI_0905, "", "us") is False

    def test_only_us_service_sends(self):
        # uk/t212 loops must NOT emit a duplicate of the global digest.
        for svc in ("uk", "t212", "t212_uk"):
            assert _should_send_weekly_report(self.SAT_0905, "", svc) is False

    def test_deduped_when_already_sent_today(self):
        already = "2026-05-30T09:05:00+00:00"
        # Same day, later tick -> must not re-fire (the 'every hour' bug).
        assert _should_send_weekly_report(self.SAT_1400, already, "us") is False

    def test_fires_again_next_saturday(self):
        last_week = "2026-05-23T09:05:00+00:00"
        assert _should_send_weekly_report(self.SAT_0905, last_week, "us") is True

    def test_downtime_catchup_same_saturday(self):
        # Bot was down at 09:00 and first runs at 14:00 — still sends once.
        assert _should_send_weekly_report(self.SAT_1400, "", "us") is True


class TestRichMarkupEscapedExceptions:
    """T212 error messages contain '[/equity/orders/market]', which Rich
    parses as a closing markup tag and raises MarkupError on. The loops now
    escape exception text before interpolating into Rich markup; this guards
    against a regression where 9 real BUY FAILEDs were lost as tracebacks
    because the error-printing line itself crashed.
    """

    def test_console_print_handles_path_brackets_in_exception(self):
        import io
        from rich.console import Console
        from rich.markup import escape

        msg = "T212 bad request [/equity/orders/market]: instrument not found"
        buf = io.StringIO()
        c = Console(file=buf, force_terminal=False, no_color=True, width=200)
        # This is the exact pattern used in run_loop_t212's BUY FAILED branch.
        c.print(f"[red]BUY FAILED NVDA: {escape(msg)}[/red]")
        out = buf.getvalue()
        assert "BUY FAILED NVDA" in out
        # The bracketed path appears literally (proves it wasn't parsed as markup).
        assert "[/equity/orders/market]" in out

    def test_paper_trader_imports_rich_markup_escape(self):
        # Locks the import so the codemod's premise (use escape()) stays true.
        import switching.paper_trader as pt
        assert hasattr(pt, "_esc")


# ---------------------------------------------------------------------------
# Out-of-hours signal queue
# ---------------------------------------------------------------------------

def _make_signal(ticker="ACME", url="https://example.com/acme", detector="guidance_raise"):
    from switching.signal import Signal
    return Signal(
        detector=detector,
        ticker=ticker,
        company=f"{ticker} Corp",
        event_dt=datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc),
        headline=f"{ticker} raises full-year guidance",
        url=url,
        evidence="raises full-year guidance",
        severity=0.8,
        extra={},
    )


class TestPendingOrderQueue:
    """The bot can classify news 24/7 but markets are open ~6.5h/day.
    Out-of-hours signals must be queued and drained at the next open
    rather than (a) firing as failed orders or (b) being lost forever."""

    def test_queue_for_open_is_idempotent_per_signal(self):
        from switching.paper_trader import _queue_for_open, _signal_key
        port = Portfolio()
        sig = _make_signal("ACME")
        now = datetime(2026, 1, 5, 22, 0, tzinfo=timezone.utc)
        _queue_for_open(port, sig, now)
        _queue_for_open(port, sig, now)   # duplicate enqueue
        assert len(port.pending_orders) == 1
        assert _signal_key(sig) in port.pending_orders

    def test_drain_returns_nothing_when_market_closed(self):
        from switching.paper_trader import _queue_for_open, _drain_pending_orders
        port = Portfolio()
        closed_now = datetime(2026, 1, 3, 22, 0, tzinfo=timezone.utc)
        _queue_for_open(port, _make_signal("ACME"), closed_now)
        with patch("switching.paper_trader._is_market_open", return_value=False):
            drained = _drain_pending_orders(port, "us", closed_now)
        assert drained == []
        assert len(port.pending_orders) == 1  # still queued

    def test_drain_returns_queued_signals_when_market_open(self):
        from switching.paper_trader import _queue_for_open, _drain_pending_orders
        port = Portfolio()
        queued_at = datetime(2026, 1, 4, 22, 0, tzinfo=timezone.utc)
        _queue_for_open(port, _make_signal("ACME"), queued_at)
        _queue_for_open(port, _make_signal("FOO", url="https://example.com/foo"), queued_at)
        open_now = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
        with patch("switching.paper_trader._is_market_open", return_value=True):
            drained = _drain_pending_orders(port, "us", open_now)
        assert {s.ticker for s in drained} == {"ACME", "FOO"}
        assert port.pending_orders == {}  # popped after drain

    def test_expire_drops_stale_entries(self):
        from switching.paper_trader import (
            _queue_for_open, _expire_pending_orders, _PENDING_ORDER_MAX_AGE_HOURS,
        )
        port = Portfolio()
        old_now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        _queue_for_open(port, _make_signal("OLD"), old_now)
        # Advance past the expiry window
        later = old_now + timedelta(hours=_PENDING_ORDER_MAX_AGE_HOURS + 1)
        expired = _expire_pending_orders(port.pending_orders, later)
        assert expired == 1
        assert port.pending_orders == {}

    def test_drain_also_expires_stale_entries(self):
        # Queue something stale, then call drain when market is open —
        # the stale entry must be dropped, not returned.
        from switching.paper_trader import (
            _queue_for_open, _drain_pending_orders, _PENDING_ORDER_MAX_AGE_HOURS,
        )
        port = Portfolio()
        stale_queued = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        _queue_for_open(port, _make_signal("STALE"), stale_queued)
        much_later = stale_queued + timedelta(hours=_PENDING_ORDER_MAX_AGE_HOURS + 5)
        with patch("switching.paper_trader._is_market_open", return_value=True):
            drained = _drain_pending_orders(port, "us", much_later)
        assert drained == []
        assert port.pending_orders == {}

    def test_queue_persists_through_save_and_load(self, tmp_path):
        # The queue must survive a container restart — otherwise we lose
        # signals queued overnight when the deploy script bounces services.
        from switching.paper_trader import _queue_for_open
        path = tmp_path / "paper_portfolio.json"
        port = Portfolio()
        queued_at = datetime(2026, 1, 4, 22, 0, tzinfo=timezone.utc)
        _queue_for_open(port, _make_signal("ACME"), queued_at)
        port.save(path)

        # Simulate restart by loading fresh
        loaded = Portfolio.load(path)
        assert len(loaded.pending_orders) == 1
        key = list(loaded.pending_orders.keys())[0]
        assert "ACME" in key

    def test_hydrate_pending_signal_round_trip(self):
        # The queue payload must round-trip back into a usable Signal so
        # the buy pipeline can re-evaluate it at market open.
        from switching.paper_trader import _hydrate_pending_signal
        sig = _make_signal("RNDX")
        payload = sig.to_dict()
        rebuilt = _hydrate_pending_signal(payload)
        assert rebuilt is not None
        assert rebuilt.ticker == "RNDX"
        assert rebuilt.detector == "guidance_raise"
        assert rebuilt.url == "https://example.com/acme"

    def test_hydrate_pending_signal_returns_none_on_bad_payload(self):
        # A corrupt persisted entry must not crash the loop.
        from switching.paper_trader import _hydrate_pending_signal
        assert _hydrate_pending_signal({"not": "a signal"}) is None
        assert _hydrate_pending_signal({}) is None


# ---------------------------------------------------------------------------
# Fast-scan window after open
# ---------------------------------------------------------------------------

class TestFastScanWindow:
    """The first 15 min after market open uses a tight 1-min scan cadence
    so queued buys and freshly-broken catalysts fire promptly. Outside
    that window the user's base interval is honored."""

    def test_fast_interval_used_at_open(self):
        from switching.paper_trader import (
            _effective_scan_interval_seconds, _FAST_SCAN_INTERVAL_SECONDS,
        )
        # 5 min after open (within fast window)
        with patch("switching.paper_trader.minutes_since_us_open", return_value=5.0):
            assert _effective_scan_interval_seconds("us", 10) == _FAST_SCAN_INTERVAL_SECONDS

    def test_base_interval_after_fast_window(self):
        from switching.paper_trader import _effective_scan_interval_seconds
        # 30 min after open (past fast window)
        with patch("switching.paper_trader.minutes_since_us_open", return_value=30.0):
            assert _effective_scan_interval_seconds("us", 10) == 600

    def test_base_interval_when_market_closed(self):
        from switching.paper_trader import _effective_scan_interval_seconds
        with patch("switching.paper_trader.minutes_since_us_open", return_value=None):
            assert _effective_scan_interval_seconds("us", 10) == 600

    def test_fast_interval_capped_at_user_base(self):
        # If the user passes --interval 0.5 (30s), fast window mustn't slow them down.
        from switching.paper_trader import _effective_scan_interval_seconds
        with patch("switching.paper_trader.minutes_since_us_open", return_value=5.0):
            # base = 30 seconds (0.5 min), fast cap = 60s — should return 30
            assert _effective_scan_interval_seconds("us", 0) == 0  # edge: 0 -> 0
            # We can't pass float to base_minutes via the signature, but real
            # usage in run_loop passes int. Just verify the min() logic:
            # base 1 minute (60s) -> min(60, 60) = 60
            assert _effective_scan_interval_seconds("us", 1) == 60

    def test_fast_window_uses_lse_helper_for_uk(self):
        from switching.paper_trader import _effective_scan_interval_seconds, _FAST_SCAN_INTERVAL_SECONDS
        with patch("switching.paper_trader.minutes_since_lse_open", return_value=10.0), \
             patch("switching.paper_trader.minutes_since_us_open", return_value=None):
            assert _effective_scan_interval_seconds("uk", 10) == _FAST_SCAN_INTERVAL_SECONDS


class TestRunLoopT212Market:
    """`run_loop_t212(market="uk")` must construct a UK-scoped Trading212Client,
    use LSE market hours, use the LSE trading-day calendar, and write state
    under the `t212_uk` service tag. The two T212 services (US + UK) share one
    T212 account but stay isolated via the broker bulkhead + distinct state
    files.
    """

    def test_invalid_market_returns_without_running(self, tmp_path, monkeypatch):
        """Bad --market must exit cleanly, NOT touch T212 or state files."""
        from switching.paper_trader import run_loop_t212
        monkeypatch.setenv("T212_API_KEY", "fake")
        monkeypatch.setenv("T212_API_SECRET", "fake")
        called = {"client_constructed": False}

        def boom(*a, **kw):
            called["client_constructed"] = True
            raise AssertionError("should not construct client for invalid market")

        monkeypatch.setattr("switching.broker_trading212.Trading212Client", boom)
        # Should print error and return None without raising
        run_loop_t212(
            state_path=tmp_path / "t212_portfolio.json",
            detectors=[],
            market="de",
            once=True,
        )
        assert called["client_constructed"] is False

    def test_market_us_constructs_us_client(self, tmp_path, monkeypatch):
        """market='us' must pass market='us' to Trading212Client."""
        from switching.paper_trader import run_loop_t212
        monkeypatch.setenv("T212_API_KEY", "fake")
        monkeypatch.setenv("T212_API_SECRET", "fake")

        captured = {}

        class StubClient:
            def __init__(self, market="us"):
                captured["market"] = market
                self.demo = True
                self.market = market
            def get_account(self):
                # Stop the loop after construction by raising — `once=True`
                # then exits the loop without running further cycles.
                raise RuntimeError("stop")

        monkeypatch.setattr("switching.broker_trading212.Trading212Client", StubClient)
        run_loop_t212(
            state_path=tmp_path / "t212_portfolio.json",
            detectors=[],
            market="us",
            once=True,
        )
        assert captured["market"] == "us"

    def test_market_uk_constructs_uk_client(self, tmp_path, monkeypatch):
        """market='uk' must pass market='uk' to Trading212Client."""
        from switching.paper_trader import run_loop_t212
        monkeypatch.setenv("T212_API_KEY", "fake")
        monkeypatch.setenv("T212_API_SECRET", "fake")

        captured = {}

        class StubClient:
            def __init__(self, market="us"):
                captured["market"] = market
                self.demo = True
                self.market = market
            def get_account(self):
                raise RuntimeError("stop")

        monkeypatch.setattr("switching.broker_trading212.Trading212Client", StubClient)
        run_loop_t212(
            state_path=tmp_path / "t212_uk_portfolio.json",
            detectors=[],
            market="uk",
            once=True,
        )
        assert captured["market"] == "uk"
