from datetime import datetime, timezone

import pandas as pd

from switching.pricing import PriceCache, compute_reaction


def _history() -> pd.DataFrame:
    # 25 trading days ending Mar 1, 2024; pre-event flat, then +10% spike.
    idx = pd.bdate_range("2024-01-22", periods=30)
    data = {
        "Open":   [10.0] * 30,
        "High":   [10.2] * 30,
        "Low":    [9.8] * 30,
        "Close":  [10.0] * 30,
        "Volume": [1_000_000] * 30,
    }
    df = pd.DataFrame(data, index=idx)
    # Event happens at index 20 (Feb 19) — simulate surge over next 5 days.
    event_idx = 20
    df.iloc[event_idx:event_idx + 1, df.columns.get_loc("Close")] = 11.0
    df.iloc[event_idx:event_idx + 1, df.columns.get_loc("Volume")] = 5_000_000
    df.iloc[event_idx + 5, df.columns.get_loc("Close")] = 12.0
    return df


def test_compute_reaction_happy_path():
    import pytest

    hist = _history()
    event_dt = datetime(2024, 2, 19, tzinfo=timezone.utc)
    r = compute_reaction(hist, event_dt, hold_days=5, baseline_days=20)
    assert r is not None
    assert r.baseline_close == 10.0
    assert r.pct_change_1d == pytest.approx(0.10, abs=1e-9)
    assert r.pct_change_5d == pytest.approx(0.20, abs=1e-9)
    assert r.volume_ratio == pytest.approx(5.0, abs=1e-9)


def test_compute_reaction_empty():
    assert compute_reaction(pd.DataFrame(), datetime(2024, 1, 1, tzinfo=timezone.utc)) is None


def test_price_cache_roundtrip(tmp_path):
    cache = PriceCache(tmp_path / "p.sqlite")
    df = _history()
    cache.put("TEST", df)
    loaded = cache.get("TEST", df.index.min().date(), df.index.max().date())
    assert len(loaded) == len(df)
    assert float(loaded["Close"].iloc[0]) == 10.0
