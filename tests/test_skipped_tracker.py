"""Tests for the skipped-signal tracker."""

from __future__ import annotations

from pathlib import Path

import pytest

from switching.skipped_tracker import SkippedTracker, TRACK_DAYS


def _add(tracker: SkippedTracker, *, ticker="AAPL", detector="ai_pivot", price=100.0, hold_days=5, first_green=True, first_green_pct=0.0, stop_loss_pct=0.05) -> None:
    tracker.add(
        ticker=ticker,
        detector=detector,
        severity=0.7,
        headline=f"{ticker} headline",
        skip_reason="max_positions",
        price=price,
        hold_days=hold_days,
        first_green=first_green,
        first_green_pct=first_green_pct,
        stop_loss_pct=stop_loss_pct,
    )


def test_add_creates_entry():
    t = SkippedTracker()
    _add(t)
    assert len(t.skipped) == 1
    assert t.skipped[0].ticker == "AAPL"
    assert t.skipped[0].tracking_complete is False


def test_dedup_same_ticker_same_day():
    t = SkippedTracker()
    _add(t, ticker="AAPL")
    _add(t, ticker="AAPL")
    assert len(t.skipped) == 1


def test_first_green_finalizes_when_above_threshold():
    t = SkippedTracker()
    _add(t, ticker="AAPL", price=100.0, first_green_pct=0.02)
    # Day 1: under threshold (no exit since days_tracked must be >= 1 AFTER snapshot)
    t.update(lambda _: 100.5)
    assert not t.skipped[0].tracking_complete
    # Day 2: above threshold, days_tracked == 1 before this snapshot → triggers
    # But same-day dedup blocks it. Simulate next day by clearing date guard
    t.skipped[0].snapshots[-1]["date"] = "1999-01-01"
    t.update(lambda _: 103.0)
    assert t.skipped[0].tracking_complete
    assert t.skipped[0].simulated_exit_reason == "first_green"
    assert t.skipped[0].simulated_pct_return == 0.03


def test_stop_loss_triggers_immediately():
    t = SkippedTracker()
    _add(t, ticker="AAPL", price=100.0, stop_loss_pct=0.05)
    t.update(lambda _: 94.0)  # -6%
    assert t.skipped[0].tracking_complete
    assert t.skipped[0].simulated_exit_reason == "stop_loss"


def test_hold_expiry_triggers_after_hold_days():
    t = SkippedTracker()
    _add(t, ticker="AAPL", price=100.0, hold_days=2, first_green=False, stop_loss_pct=0.99)
    t.update(lambda _: 100.5)
    t.skipped[0].snapshots[-1]["date"] = "1999-01-01"
    t.update(lambda _: 101.0)
    t.skipped[0].snapshots[-1]["date"] = "1999-01-02"
    t.update(lambda _: 102.0)
    assert t.skipped[0].tracking_complete
    assert t.skipped[0].simulated_exit_reason == "hold_expiry"


def test_save_and_load_roundtrip(tmp_path: Path):
    t = SkippedTracker()
    _add(t, ticker="AAPL")
    _add(t, ticker="MSFT")
    t.update(lambda _: 95.0)  # stop-loss for both

    path = tmp_path / "skipped.json"
    t.save(path)

    t2 = SkippedTracker.load(path)
    assert len(t2.skipped) == 2
    assert all(s.tracking_complete for s in t2.skipped)


def test_active_and_completed_counts():
    t = SkippedTracker()
    _add(t, ticker="AAPL", first_green_pct=0.05)  # high threshold so it stays active
    _add(t, ticker="MSFT", price=200.0, first_green_pct=0.05)
    # Stop-loss AAPL only; MSFT only +0.25% (under 5% first-green and above stop-loss)
    t.update(lambda tk: 90.0 if tk == "AAPL" else 200.5)
    assert t.completed_count == 1
    assert t.active_count == 1


def test_summary_aggregates_completed_only():
    t = SkippedTracker()
    _add(t, ticker="AAPL", price=100.0)
    _add(t, ticker="MSFT", price=200.0)
    t.update(lambda tk: 95.0 if tk == "AAPL" else 210.0)
    # AAPL stop-loss completes; MSFT not yet (no first_green threshold met after 1 day)
    summary = t._build_summary()
    assert summary["completed_count"] >= 1


def test_load_missing_file_returns_empty(tmp_path: Path):
    t = SkippedTracker.load(tmp_path / "nonexistent.json")
    assert t.skipped == []


def test_max_entries_cap():
    t = SkippedTracker()
    for i in range(550):
        # Different tickers to bypass dedup
        _add(t, ticker=f"T{i:04d}")
    assert len(t.skipped) <= 500
