"""Tests for the post-exit price tracker."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from switching.exit_tracker import ExitTracker, TrackedExit, TRACK_DAYS


@dataclass
class FakeTrade:
    ticker: str = "AAPL"
    detector: str = "earnings_surprise"
    entry_price: float = 100.0
    exit_price: float = 103.0
    exit_dt: str = "2024-06-01T12:00:00+00:00"
    exit_reason: str = "first_green"
    pct_return: float = 0.03
    headline: str = "Apple Beats Earnings"


def test_add_trade():
    tracker = ExitTracker()
    trade = FakeTrade()
    tracker.add_trade(trade)
    assert len(tracker.tracked) == 1
    assert tracker.tracked[0].ticker == "AAPL"
    assert tracker.tracked[0].exit_reason == "first_green"


def test_no_duplicate_trades():
    tracker = ExitTracker()
    trade = FakeTrade()
    tracker.add_trade(trade)
    tracker.add_trade(trade)
    assert len(tracker.tracked) == 1


def test_update_records_snapshot():
    tracker = ExitTracker()
    tracker.add_trade(FakeTrade())

    prices = {"AAPL": 105.0}
    count = tracker.update(lambda t: prices.get(t))
    assert count == 1
    assert len(tracker.tracked[0].snapshots) == 1
    snap = tracker.tracked[0].snapshots[0]
    assert snap["close"] == 105.0   # scalar price stored as close/high/low/open
    assert snap["high"]  == 105.0
    assert snap["low"]   == 105.0
    assert snap["pct_from_entry"] == 0.05
    assert snap["day"] == 1


def test_update_skips_same_day():
    tracker = ExitTracker()
    tracker.add_trade(FakeTrade())

    prices = {"AAPL": 105.0}
    tracker.update(lambda t: prices.get(t))
    # Second call same day should skip
    count = tracker.update(lambda t: prices.get(t))
    assert count == 0
    assert len(tracker.tracked[0].snapshots) == 1


def test_update_skips_if_no_price():
    tracker = ExitTracker()
    tracker.add_trade(FakeTrade())
    count = tracker.update(lambda t: None)
    assert count == 0
    assert len(tracker.tracked[0].snapshots) == 0


def test_tracking_completes_after_n_days():
    tracker = ExitTracker()
    tracker.add_trade(FakeTrade())

    # Simulate 20 days of snapshots
    t = tracker.tracked[0]
    for i in range(TRACK_DAYS):
        t.snapshots.append({
            "date": f"2024-06-{i+2:02d}",
            "day": i + 1,
            "price": 100.0 + i,
            "pct_from_entry": round((100.0 + i) / 100.0 - 1.0, 4),
            "pct_from_exit": round((100.0 + i) / 103.0 - 1.0, 4),
        })

    tracker.update(lambda t: 120.0)
    assert t.tracking_complete is True
    assert tracker.active_count == 0


def test_left_on_table():
    t = TrackedExit(
        ticker="AAPL", detector="earnings_surprise",
        entry_price=100.0, exit_price=103.0,
        exit_dt="2024-06-01", exit_reason="first_green",
        pct_return=0.03, headline="Apple Beats",
    )
    t.snapshots = [
        {"date": "2024-06-02", "day": 1, "price": 105.0, "pct_from_entry": 0.05, "pct_from_exit": 0.019},
        {"date": "2024-06-03", "day": 2, "price": 110.0, "pct_from_entry": 0.10, "pct_from_exit": 0.068},
        {"date": "2024-06-04", "day": 3, "price": 108.0, "pct_from_entry": 0.08, "pct_from_exit": 0.049},
    ]
    assert t.max_post_exit_return == 0.10
    assert t.left_on_table == pytest.approx(0.07)
    assert t.final_return == 0.08


def test_save_and_load(tmp_path):
    tracker = ExitTracker()
    tracker.add_trade(FakeTrade())
    tracker.tracked[0].snapshots.append({
        "date": "2024-06-02", "day": 1, "price": 105.0,
        "pct_from_entry": 0.05, "pct_from_exit": 0.019,
    })

    path = tmp_path / "exit_tracker.json"
    tracker.save(path)

    loaded = ExitTracker.load(path)
    assert len(loaded.tracked) == 1
    assert loaded.tracked[0].ticker == "AAPL"
    assert len(loaded.tracked[0].snapshots) == 1
    assert loaded.tracked[0].snapshots[0]["price"] == 105.0


def test_load_missing_file(tmp_path):
    path = tmp_path / "nonexistent.json"
    tracker = ExitTracker.load(path)
    assert len(tracker.tracked) == 0


def test_active_count():
    tracker = ExitTracker()
    tracker.add_trade(FakeTrade())
    tracker.add_trade(FakeTrade(ticker="MSFT", exit_dt="2024-06-01T13:00:00+00:00"))
    assert tracker.active_count == 2
    tracker.tracked[0].tracking_complete = True
    assert tracker.active_count == 1


def test_to_dict_includes_computed_fields():
    t = TrackedExit(
        ticker="AAPL", detector="test",
        entry_price=100.0, exit_price=103.0,
        exit_dt="2024-06-01", exit_reason="first_green",
        pct_return=0.03, headline="Test",
    )
    t.snapshots = [
        {"date": "2024-06-02", "day": 1, "price": 108.0, "pct_from_entry": 0.08, "pct_from_exit": 0.048},
    ]
    d = t.to_dict()
    assert d["days_tracked"] == 1
    assert d["max_post_exit_return"] == 0.08
    assert d["left_on_table"] == pytest.approx(0.05)


def test_multiple_trades_different_detectors():
    tracker = ExitTracker()
    tracker.add_trade(FakeTrade(ticker="AAPL", detector="earnings_surprise"))
    tracker.add_trade(FakeTrade(ticker="MSFT", detector="analyst_upgrade", exit_dt="2024-06-01T13:00:00+00:00"))
    tracker.add_trade(FakeTrade(ticker="NVDA", detector="contract_win", exit_dt="2024-06-01T14:00:00+00:00"))

    prices = {"AAPL": 105.0, "MSFT": 302.0, "NVDA": 900.0}
    count = tracker.update(lambda t: prices.get(t))
    assert count == 3


def test_stop_loss_recovery_insight():
    """Trades that hit stop-loss but later recovered should generate an insight."""
    tracker = ExitTracker()
    for i in range(4):
        trade = FakeTrade(
            ticker=f"TICK{i}",
            exit_dt=f"2024-06-0{i+1}T12:00:00+00:00",
            exit_reason="stop_loss",
            pct_return=-0.026,
            exit_price=97.4,
        )
        tracker.add_trade(trade)
        t = tracker.tracked[-1]
        # Simulate recovery
        for d in range(TRACK_DAYS):
            t.snapshots.append({
                "date": f"2024-07-{d+1:02d}",
                "day": d + 1,
                "price": 100.0 + d,
                "pct_from_entry": round(d / 100.0, 4),
                "pct_from_exit": round((100.0 + d) / 97.4 - 1.0, 4),
            })
        t.tracking_complete = True

    path = Path("/dev/null")
    summary = tracker._build_summary()
    assert summary["completed_tracks"] == 4
    assert any("recovered" in i for i in summary.get("insights", []))
