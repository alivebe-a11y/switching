from datetime import datetime, timezone

import pandas as pd
import pytest

from switching import backtest
from switching.signal import Signal


def _synthetic_history(event_date, spike_pct: float) -> pd.DataFrame:
    # 40 business days starting 20 calendar days before the event.
    start = pd.Timestamp(event_date) - pd.Timedelta(days=20)
    idx = pd.bdate_range(start=start, periods=40)
    rows = len(idx)
    df = pd.DataFrame(
        {
            "Open": [10.0] * rows,
            "High": [10.2] * rows,
            "Low": [9.8] * rows,
            "Close": [10.0] * rows,
            "Volume": [1_000_000] * rows,
        },
        index=idx,
    )
    mask = df.index >= pd.Timestamp(event_date)
    if mask.any():
        first = df.index[mask][0]
        pos = df.index.get_loc(first)
        # Open stays at 10; close five trading days later reflects the spike.
        df.iloc[pos + 5, df.columns.get_loc("Close")] = 10.0 * (1.0 + spike_pct)
    return df


@pytest.fixture
def monkey_get_history(monkeypatch):
    """Patch backtest.get_history to yield deterministic fake data per ticker."""
    scenarios = {
        "WIN_A": (datetime(2023, 3, 1, tzinfo=timezone.utc), 0.08),   # +8%
        "WIN_B": (datetime(2023, 5, 15, tzinfo=timezone.utc), 0.03),  # +3%
        "LOSS":  (datetime(2023, 7, 10, tzinfo=timezone.utc), -0.04), # -4%
    }

    def fake(ticker, start, end, cache=None):
        event_dt, pct = scenarios[ticker]
        return _synthetic_history(event_dt.date(), pct)

    monkeypatch.setattr(backtest, "get_history", fake)
    return scenarios


def _sig(ticker: str, event_dt: datetime, severity: float = 0.8) -> Signal:
    return Signal(
        detector="ai_pivot",
        ticker=ticker,
        company=ticker,
        event_dt=event_dt,
        headline=f"{ticker} announces AI pivot",
        url="",
        evidence="",
        severity=severity,
    )


def test_simulate_and_summarize(monkey_get_history):
    signals = [
        _sig("WIN_A", monkey_get_history["WIN_A"][0]),
        _sig("WIN_B", monkey_get_history["WIN_B"][0]),
        _sig("LOSS", monkey_get_history["LOSS"][0]),
    ]
    trades = backtest.simulate(signals, hold_days=5, cost_bps=0.0)
    assert len(trades) == 3
    returns = sorted(t.net_return for t in trades)
    assert returns[0] == pytest.approx(-0.04, abs=1e-9)
    assert returns[-1] == pytest.approx(0.08, abs=1e-9)

    perf = backtest.summarize(trades)
    assert perf.trades == 3
    assert perf.wins == 2
    assert perf.win_rate == pytest.approx(2 / 3)
    assert perf.best == pytest.approx(0.08)
    assert perf.worst == pytest.approx(-0.04)


def test_simulate_applies_cost(monkey_get_history):
    signals = [_sig("WIN_B", monkey_get_history["WIN_B"][0])]
    trades = backtest.simulate(signals, hold_days=5, cost_bps=50.0)  # 50 bps
    assert len(trades) == 1
    assert trades[0].gross_return == pytest.approx(0.03, abs=1e-9)
    assert trades[0].net_return == pytest.approx(0.03 - 0.005, abs=1e-9)


def test_simulate_respects_min_severity(monkey_get_history):
    signals = [
        _sig("WIN_A", monkey_get_history["WIN_A"][0], severity=0.2),
        _sig("WIN_B", monkey_get_history["WIN_B"][0], severity=0.9),
    ]
    trades = backtest.simulate(signals, hold_days=5, min_severity=0.5)
    assert len(trades) == 1
    assert trades[0].ticker == "WIN_B"


def test_summarize_empty():
    perf = backtest.summarize([])
    assert perf.trades == 0
    assert perf.win_rate == 0.0
