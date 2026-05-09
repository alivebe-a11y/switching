"""Tests for the Black-Scholes options model and trade comparison."""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from switching.options_model import (
    OptionsCompareResult,
    _norm_cdf,
    black_scholes_call,
    compare_options_vs_stock,
    model_call_trade,
)


# ---------------------------------------------------------------------------
# Stub ClosedTrade for comparison tests (avoids importing paper_trader)
# ---------------------------------------------------------------------------

@dataclass
class _FakeTrade:
    ticker: str
    detector: str
    exit_reason: str
    entry_price: float
    exit_price: float
    shares: float
    entry_dt: str = "2024-03-01T10:00:00+00:00"
    exit_dt: str  = "2024-03-05T16:00:00+00:00"
    pnl: float = 0.0
    pct_return: float = 0.0
    headline: str = ""
    peak_price: float = 0.0
    severity: float = 0.0


# ---------------------------------------------------------------------------
# _norm_cdf
# ---------------------------------------------------------------------------

def test_norm_cdf_at_zero():
    assert _norm_cdf(0) == pytest.approx(0.5)

def test_norm_cdf_large_positive():
    assert _norm_cdf(10) == pytest.approx(1.0, abs=1e-6)

def test_norm_cdf_large_negative():
    assert _norm_cdf(-10) == pytest.approx(0.0, abs=1e-6)

def test_norm_cdf_symmetry():
    assert _norm_cdf(1.96) + _norm_cdf(-1.96) == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------------------------
# black_scholes_call
# ---------------------------------------------------------------------------

def test_bs_call_known_value():
    """ATM call: S=100, K=100, T=0.25y, r=0.05, sigma=0.20 → ~4.61 (textbook)."""
    price = black_scholes_call(S=100, K=100, T=0.25, r=0.05, sigma=0.20)
    assert 4.0 < price < 5.5   # rough range check

def test_bs_call_zero_time():
    """At expiry, call = intrinsic value only."""
    assert black_scholes_call(110, 100, T=0, r=0.05, sigma=0.30) == pytest.approx(10.0)
    assert black_scholes_call(90,  100, T=0, r=0.05, sigma=0.30) == pytest.approx(0.0)

def test_bs_call_otm_small():
    """Deep OTM call with little time is nearly worthless."""
    price = black_scholes_call(S=50, K=100, T=0.05, r=0.05, sigma=0.30)
    assert price < 0.01

def test_bs_call_itm_deep():
    """Deep ITM call approaches intrinsic (S - K*e^(-rT))."""
    S, K = 200.0, 100.0
    T, r = 0.25, 0.05
    price = black_scholes_call(S, K, T, r, sigma=0.30)
    intrinsic = S - K * math.exp(-r * T)
    assert price > intrinsic * 0.95   # call >= intrinsic (no-arb)

def test_bs_call_increases_with_vol():
    """Higher IV → higher option premium (vega is always positive for calls)."""
    p_lo = black_scholes_call(100, 100, 0.25, 0.05, sigma=0.20)
    p_hi = black_scholes_call(100, 100, 0.25, 0.05, sigma=0.50)
    assert p_hi > p_lo

def test_bs_call_increases_with_time():
    """Longer expiry → higher option premium (theta decay works the other way)."""
    p_short = black_scholes_call(100, 100, 0.1, 0.05, sigma=0.30)
    p_long  = black_scholes_call(100, 100, 0.5, 0.05, sigma=0.30)
    assert p_long > p_short

def test_bs_call_zero_vol():
    """Zero vol: call = max(0, S - K*e^(-rT))."""
    S, K, T, r = 110.0, 100.0, 0.25, 0.05
    price = black_scholes_call(S, K, T, r, sigma=0.0)
    expected = max(0.0, S - K * math.exp(-r * T))
    assert price == pytest.approx(expected)


# ---------------------------------------------------------------------------
# model_call_trade
# ---------------------------------------------------------------------------

def test_model_call_trade_profit_on_move():
    """A 10% stock move should produce a positive options P&L on a 2-week ATM call."""
    result = model_call_trade(
        ticker="AAPL", detector="guidance_raise", exit_reason="first_green",
        entry_price=150.0, exit_price=165.0,  # +10%
        cost_basis=1500.0,
        entry_dt="2024-03-01T10:00:00+00:00",
        exit_dt="2024-03-04T16:00:00+00:00",  # 3 days held
        assumed_iv=0.30, dte=14,
    )
    assert result.option_pnl > 0
    assert result.stock_pnl > 0
    assert result.option_pct_return > result.stock_pct_return  # leverage amplifies

def test_model_call_trade_loss_on_drop():
    """A -5% stock move → big option loss (time decay + move against)."""
    result = model_call_trade(
        ticker="XYZ", detector="earnings_surprise", exit_reason="stop_loss",
        entry_price=100.0, exit_price=95.0,  # -5%
        cost_basis=1000.0,
        entry_dt="2024-03-01T10:00:00+00:00",
        exit_dt="2024-03-03T16:00:00+00:00",
        assumed_iv=0.30, dte=14,
    )
    assert result.option_pnl < 0
    assert result.stock_pnl < 0
    # Option loses more in percentage terms due to theta + delta
    assert result.option_pct_return < result.stock_pct_return

def test_model_call_trade_hold_days_inferred():
    """hold_days should be inferred from entry_dt/exit_dt."""
    result = model_call_trade(
        ticker="T", detector="test", exit_reason="hold_expiry",
        entry_price=100.0, exit_price=102.0,
        cost_basis=500.0,
        entry_dt="2024-01-01T10:00:00+00:00",
        exit_dt="2024-01-05T16:00:00+00:00",  # 4 calendar days
        assumed_iv=0.30, dte=14,
    )
    assert result.hold_days == 4

def test_model_call_trade_premium_positive():
    """Premium should always be positive for a valid ATM call."""
    result = model_call_trade(
        ticker="T", detector="test", exit_reason="hold_expiry",
        entry_price=50.0, exit_price=52.0,
        cost_basis=500.0,
        entry_dt="2024-01-01T10:00:00+00:00",
        exit_dt="2024-01-08T16:00:00+00:00",
        assumed_iv=0.30, dte=14,
    )
    assert result.premium_per_share > 0


# ---------------------------------------------------------------------------
# compare_options_vs_stock
# ---------------------------------------------------------------------------

def _make_trades(outcomes: list[tuple[str, float, float]]) -> list[_FakeTrade]:
    """Build fake trades: (detector, entry, exit) list."""
    trades = []
    for det, entry, exit_ in outcomes:
        pnl = (exit_ - entry) * 10  # 10 shares
        pct = exit_ / entry - 1
        trades.append(_FakeTrade(
            ticker="TST", detector=det,
            exit_reason="first_green" if pct > 0 else "stop_loss",
            entry_price=entry, exit_price=exit_,
            shares=10.0, pnl=round(pnl, 2), pct_return=round(pct, 4),
        ))
    return trades


def test_compare_returns_correct_trade_count():
    trades = _make_trades([
        ("guidance_raise", 100.0, 105.0),
        ("analyst_upgrade", 50.0, 48.0),
    ])
    result = compare_options_vs_stock(trades, assumed_iv=0.30, dte=14)
    assert len(result.trades) == 2


def test_compare_detector_filter():
    trades = _make_trades([
        ("guidance_raise", 100.0, 105.0),
        ("analyst_upgrade", 50.0, 55.0),
    ])
    result = compare_options_vs_stock(
        trades, assumed_iv=0.30, dte=14,
        detectors={"guidance_raise"},
    )
    assert len(result.trades) == 1
    assert result.trades[0].detector == "guidance_raise"


def test_compare_win_rate():
    trades = _make_trades([
        ("det_a", 100.0, 110.0),  # win
        ("det_a", 100.0, 110.0),  # win
        ("det_a", 100.0, 90.0),   # loss
    ])
    result = compare_options_vs_stock(trades, assumed_iv=0.30, dte=14)
    assert result.stock_wins == 2
    assert result.stock_win_rate == pytest.approx(2 / 3)


def test_compare_by_detector_aggregation():
    trades = _make_trades([
        ("det_a", 100.0, 110.0),
        ("det_a", 100.0, 108.0),
        ("det_b", 50.0, 52.0),
    ])
    result = compare_options_vs_stock(trades, assumed_iv=0.30, dte=14)
    by_det = result.by_detector()
    assert "det_a" in by_det
    assert "det_b" in by_det
    assert by_det["det_a"]["trades"] == 2
    assert by_det["det_b"]["trades"] == 1


def test_compare_empty_trades():
    result = compare_options_vs_stock([], assumed_iv=0.30, dte=14)
    assert len(result.trades) == 0
    assert result.total_stock_pnl == 0.0
    assert result.total_options_pnl == 0.0
    assert result.stock_win_rate == 0.0


def test_compare_big_win_options_beat_stock():
    """On a large upward move, leveraged options should outperform stock."""
    trades = _make_trades([("det", 100.0, 130.0)])  # +30% — big gap
    result = compare_options_vs_stock(trades, assumed_iv=0.30, dte=14)
    assert result.total_options_pnl > result.total_stock_pnl
