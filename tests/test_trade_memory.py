"""Tests for trade memory analysis."""

from __future__ import annotations

from dataclasses import dataclass

from switching.trade_memory import TierStats, _price_tier, build_memory


@dataclass
class FakeTrade:
    detector: str
    ticker: str
    entry_price: float
    exit_price: float
    pnl: float
    pct_return: float
    exit_reason: str


def _trades():
    return [
        FakeTrade("ai_pivot", "AAPL", 150.0, 153.0, 3.0, 0.02, "first_green"),
        FakeTrade("ai_pivot", "MSFT", 300.0, 306.0, 6.0, 0.02, "first_green"),
        FakeTrade("ai_pivot", "QUBT", 4.0, 3.8, -0.2, -0.05, "stop_loss"),
        FakeTrade("buyback", "META", 350.0, 345.0, -5.0, -0.014, "hold_expiry"),
        FakeTrade("buyback", "GOOG", 140.0, 137.0, -3.0, -0.021, "stop_loss"),
        FakeTrade("earnings_surprise", "NVDA", 800.0, 820.0, 20.0, 0.025, "first_green"),
    ]


class TestPriceTier:
    def test_100_plus(self):
        assert _price_tier(150.0) == "$100+"

    def test_30_to_100(self):
        assert _price_tier(50.0) == "$30-100"

    def test_5_to_30(self):
        assert _price_tier(15.0) == "$5-30"

    def test_under_5(self):
        assert _price_tier(3.0) == "<$5"

    def test_boundary_100(self):
        assert _price_tier(100.0) == "$100+"

    def test_boundary_30(self):
        assert _price_tier(30.0) == "$30-100"

    def test_boundary_5(self):
        assert _price_tier(5.0) == "$5-30"


class TestBuildMemory:
    def test_empty_trades(self):
        m = build_memory([])
        assert m["total_trades"] == 0

    def test_overall_stats(self):
        m = build_memory(_trades())
        assert m["total_trades"] == 6
        assert m["overall"]["wins"] == 3
        assert m["overall"]["trades"] == 6

    def test_by_detector(self):
        m = build_memory(_trades())
        assert "ai_pivot" in m["by_detector"]
        assert m["by_detector"]["ai_pivot"]["trades"] == 3
        assert m["by_detector"]["ai_pivot"]["wins"] == 2
        assert m["by_detector"]["buyback"]["trades"] == 2

    def test_by_price_tier(self):
        m = build_memory(_trades())
        assert "$100+" in m["by_price_tier"]
        assert "<$5" in m["by_price_tier"]

    def test_by_exit_reason(self):
        m = build_memory(_trades())
        assert "first_green" in m["by_exit_reason"]
        assert "stop_loss" in m["by_exit_reason"]
        assert m["by_exit_reason"]["first_green"]["trades"] == 3

    def test_by_detector_and_tier(self):
        m = build_memory(_trades())
        assert "ai_pivot" in m["by_detector_and_tier"]
        assert "$100+" in m["by_detector_and_tier"]["ai_pivot"]
        assert "<$5" in m["by_detector_and_tier"]["ai_pivot"]

    def test_patterns_detected(self):
        m = build_memory(_trades())
        assert isinstance(m["patterns"], list)

    def test_patterns_with_enough_data(self):
        trades = _trades() + [
            FakeTrade("ai_pivot", "AMZN", 180.0, 183.6, 3.6, 0.02, "first_green"),
        ]
        m = build_memory(trades)
        ai_patterns = [p for p in m["patterns"] if "ai_pivot" in p]
        assert any("strong" in p for p in ai_patterns)


class TestTierStats:
    def test_win_rate_no_trades(self):
        s = TierStats()
        assert s.win_rate == 0.0

    def test_avg_return_no_trades(self):
        s = TierStats()
        assert s.avg_return == 0.0

    def test_to_dict(self):
        s = TierStats(trades=10, wins=6, total_pnl=50.0, total_return_pct=0.15)
        d = s.to_dict()
        assert d["trades"] == 10
        assert d["win_rate"] == 0.6
