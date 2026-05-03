"""Trade memory — learn from closed trades.

Analyzes the portfolio's trade history and writes a summary JSON file
that captures per-detector, per-price-tier, and per-exit-reason stats.
Updated after each scan cycle. Read-only analysis — does not change
trading behavior.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger(__name__)


@dataclass
class TierStats:
    trades: int = 0
    wins: int = 0
    total_pnl: float = 0.0
    total_return_pct: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def avg_return(self) -> float:
        return self.total_return_pct / self.trades if self.trades else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "trades": self.trades,
            "wins": self.wins,
            "win_rate": round(self.win_rate, 3),
            "avg_return": round(self.avg_return, 4),
            "total_pnl": round(self.total_pnl, 2),
        }


def _price_tier(price: float) -> str:
    if price >= 100:
        return "$100+"
    if price >= 30:
        return "$30-100"
    if price >= 5:
        return "$5-30"
    return "<$5"


def build_memory(trades: Sequence[Any]) -> dict[str, Any]:
    """Analyze closed trades and return a structured memory dict."""
    if not trades:
        return {"total_trades": 0}

    by_detector: dict[str, TierStats] = {}
    by_price_tier: dict[str, TierStats] = {}
    by_exit: dict[str, TierStats] = {}
    by_detector_tier: dict[str, dict[str, TierStats]] = {}

    overall = TierStats()

    for t in trades:
        overall.trades += 1
        overall.total_pnl += t.pnl
        overall.total_return_pct += t.pct_return
        if t.pnl > 0:
            overall.wins += 1

        det = t.detector
        if det not in by_detector:
            by_detector[det] = TierStats()
        s = by_detector[det]
        s.trades += 1
        s.total_pnl += t.pnl
        s.total_return_pct += t.pct_return
        if t.pnl > 0:
            s.wins += 1

        tier = _price_tier(t.entry_price)
        if tier not in by_price_tier:
            by_price_tier[tier] = TierStats()
        s = by_price_tier[tier]
        s.trades += 1
        s.total_pnl += t.pnl
        s.total_return_pct += t.pct_return
        if t.pnl > 0:
            s.wins += 1

        exit_r = t.exit_reason
        if exit_r not in by_exit:
            by_exit[exit_r] = TierStats()
        s = by_exit[exit_r]
        s.trades += 1
        s.total_pnl += t.pnl
        s.total_return_pct += t.pct_return
        if t.pnl > 0:
            s.wins += 1

        if det not in by_detector_tier:
            by_detector_tier[det] = {}
        if tier not in by_detector_tier[det]:
            by_detector_tier[det][tier] = TierStats()
        s = by_detector_tier[det][tier]
        s.trades += 1
        s.total_pnl += t.pnl
        s.total_return_pct += t.pct_return
        if t.pnl > 0:
            s.wins += 1

    losers = [t for t in trades if t.pnl < 0]
    winners = [t for t in trades if t.pnl > 0]

    patterns: list[str] = []
    for det, stats in by_detector.items():
        if stats.trades >= 3 and stats.win_rate < 0.40:
            patterns.append(f"{det}: low win rate ({stats.win_rate:.0%} over {stats.trades} trades)")
        if stats.trades >= 3 and stats.win_rate > 0.70:
            patterns.append(f"{det}: strong performer ({stats.win_rate:.0%} over {stats.trades} trades)")

    for tier, stats in by_price_tier.items():
        if stats.trades >= 3 and stats.win_rate < 0.40:
            patterns.append(f"{tier} stocks: low win rate ({stats.win_rate:.0%} over {stats.trades} trades)")
        if stats.trades >= 3 and stats.win_rate > 0.70:
            patterns.append(f"{tier} stocks: strong ({stats.win_rate:.0%} over {stats.trades} trades)")

    return {
        "total_trades": overall.trades,
        "overall": overall.to_dict(),
        "by_detector": {k: v.to_dict() for k, v in sorted(by_detector.items())},
        "by_price_tier": {k: v.to_dict() for k, v in sorted(by_price_tier.items())},
        "by_exit_reason": {k: v.to_dict() for k, v in sorted(by_exit.items())},
        "by_detector_and_tier": {
            det: {tier: s.to_dict() for tier, s in sorted(tiers.items())}
            for det, tiers in sorted(by_detector_tier.items())
        },
        "patterns": patterns,
    }


def update_memory(trades: Sequence[Any], path: Path) -> dict[str, Any]:
    """Build memory from trades and write to disk."""
    memory = build_memory(trades)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(memory, indent=2), encoding="utf-8")
    log.info("trade memory updated: %d trades, %d patterns", memory["total_trades"], len(memory.get("patterns", [])))
    return memory


def load_memory(path: Path) -> dict[str, Any]:
    """Load existing memory file, or return empty dict."""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))
