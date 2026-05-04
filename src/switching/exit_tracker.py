"""Post-exit price tracker.

After a trade is closed, this module tracks the stock's daily closing price
for up to N days. This answers the key refinement questions:

- Did we exit too early? (stock kept running after first-green exit)
- Was the stop-loss too tight? (stock recovered after stop-loss hit)
- Was hold period right? (stock dropped after hold-expiry, or kept climbing)

The tracker runs as part of each scan cycle in the paper-trade loop — it
doesn't hold positions, just records prices. Data is stored in a JSON file
alongside the portfolio state.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TRACK_DAYS = 20


@dataclass
class TrackedExit:
    ticker: str
    detector: str
    entry_price: float
    exit_price: float
    exit_dt: str
    exit_reason: str
    pct_return: float
    headline: str
    snapshots: list[dict] = field(default_factory=list)
    tracking_complete: bool = False

    @property
    def days_tracked(self) -> int:
        return len(self.snapshots)

    @property
    def max_post_exit_return(self) -> float | None:
        if not self.snapshots:
            return None
        return max(s["pct_from_entry"] for s in self.snapshots)

    @property
    def min_post_exit_return(self) -> float | None:
        if not self.snapshots:
            return None
        return min(s["pct_from_entry"] for s in self.snapshots)

    @property
    def final_return(self) -> float | None:
        if not self.snapshots:
            return None
        return self.snapshots[-1]["pct_from_entry"]

    @property
    def left_on_table(self) -> float | None:
        """How much more the stock moved after we exited. Positive = left money."""
        max_r = self.max_post_exit_return
        if max_r is None:
            return None
        return max_r - self.pct_return

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["days_tracked"] = self.days_tracked
        d["max_post_exit_return"] = self.max_post_exit_return
        d["min_post_exit_return"] = self.min_post_exit_return
        d["final_return"] = self.final_return
        d["left_on_table"] = self.left_on_table
        return d


@dataclass
class ExitTracker:
    tracked: list[TrackedExit] = field(default_factory=list)

    def add_trade(self, trade: Any) -> None:
        """Add a newly closed trade to be tracked."""
        for t in self.tracked:
            if t.ticker == trade.ticker and t.exit_dt == trade.exit_dt:
                return
        self.tracked.append(TrackedExit(
            ticker=trade.ticker,
            detector=trade.detector,
            entry_price=trade.entry_price,
            exit_price=trade.exit_price,
            exit_dt=trade.exit_dt,
            exit_reason=trade.exit_reason,
            pct_return=trade.pct_return,
            headline=trade.headline,
        ))

    def update(self, get_price_fn) -> int:
        """Fetch today's price for all active tracked exits. Returns count updated."""
        updated = 0
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

        for t in self.tracked:
            if t.tracking_complete:
                continue
            if t.days_tracked >= TRACK_DAYS:
                t.tracking_complete = True
                continue

            # Don't snapshot the same day twice
            if t.snapshots and t.snapshots[-1]["date"] == today:
                continue

            price = get_price_fn(t.ticker)
            if price is None:
                continue

            pct_from_entry = round(price / t.entry_price - 1.0, 4)
            pct_from_exit = round(price / t.exit_price - 1.0, 4)

            t.snapshots.append({
                "date": today,
                "day": t.days_tracked + 1,
                "price": round(price, 4),
                "pct_from_entry": pct_from_entry,
                "pct_from_exit": pct_from_exit,
            })
            updated += 1

            if t.days_tracked >= TRACK_DAYS:
                t.tracking_complete = True

        return updated

    @property
    def active_count(self) -> int:
        return sum(1 for t in self.tracked if not t.tracking_complete)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "tracked": [t.to_dict() for t in self.tracked],
            "summary": self._build_summary(),
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> ExitTracker:
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        tracker = cls()
        for item in data.get("tracked", []):
            # Filter out computed fields that aren't in the constructor
            constructor_fields = {
                "ticker", "detector", "entry_price", "exit_price",
                "exit_dt", "exit_reason", "pct_return", "headline",
                "snapshots", "tracking_complete",
            }
            filtered = {k: v for k, v in item.items() if k in constructor_fields}
            tracker.tracked.append(TrackedExit(**filtered))
        return tracker

    def _build_summary(self) -> dict[str, Any]:
        """Aggregate insights from completed tracks."""
        completed = [t for t in self.tracked if t.tracking_complete]
        if not completed:
            return {"completed_tracks": 0}

        by_detector: dict[str, list[TrackedExit]] = {}
        by_exit_reason: dict[str, list[TrackedExit]] = {}

        for t in completed:
            by_detector.setdefault(t.detector, []).append(t)
            by_exit_reason.setdefault(t.exit_reason, []).append(t)

        def _summarize_group(group: list[TrackedExit]) -> dict[str, Any]:
            left = [t.left_on_table for t in group if t.left_on_table is not None]
            max_after = [t.max_post_exit_return for t in group if t.max_post_exit_return is not None]
            final = [t.final_return for t in group if t.final_return is not None]
            return {
                "count": len(group),
                "avg_exit_return": round(sum(t.pct_return for t in group) / len(group), 4) if group else 0,
                "avg_left_on_table": round(sum(left) / len(left), 4) if left else None,
                "avg_max_post_exit": round(sum(max_after) / len(max_after), 4) if max_after else None,
                "avg_final_return_day20": round(sum(final) / len(final), 4) if final else None,
                "exit_too_early_pct": round(
                    sum(1 for l in left if l > 0.02) / len(left), 3
                ) if left else None,
            }

        return {
            "completed_tracks": len(completed),
            "by_detector": {d: _summarize_group(g) for d, g in sorted(by_detector.items())},
            "by_exit_reason": {r: _summarize_group(g) for r, g in sorted(by_exit_reason.items())},
            "insights": self._generate_insights(completed),
        }

    def _generate_insights(self, completed: list[TrackedExit]) -> list[str]:
        """Human-readable insights from post-exit tracking data."""
        insights: list[str] = []

        by_detector: dict[str, list[TrackedExit]] = {}
        by_exit: dict[str, list[TrackedExit]] = {}
        for t in completed:
            by_detector.setdefault(t.detector, []).append(t)
            by_exit.setdefault(t.exit_reason, []).append(t)

        for det, trades in by_detector.items():
            if len(trades) < 3:
                continue
            left = [t.left_on_table for t in trades if t.left_on_table is not None]
            if left:
                avg_left = sum(left) / len(left)
                if avg_left > 0.03:
                    insights.append(
                        f"{det}: avg {avg_left:.1%} left on table after exit — "
                        f"consider raising first_green_pct or extending hold_days"
                    )
                if avg_left < -0.02:
                    insights.append(
                        f"{det}: stocks drop avg {abs(avg_left):.1%} after exit — "
                        f"exit timing is good, possibly tighten further"
                    )

        for reason, trades in by_exit.items():
            if len(trades) < 3:
                continue
            if reason == "stop_loss":
                recovered = [t for t in trades if t.max_post_exit_return and t.max_post_exit_return > 0]
                if len(recovered) > len(trades) * 0.5:
                    insights.append(
                        f"stop_loss: {len(recovered)}/{len(trades)} trades recovered after stop — "
                        f"consider widening stop-loss"
                    )
            if reason == "first_green":
                kept_running = [t for t in trades if t.left_on_table and t.left_on_table > 0.03]
                if len(kept_running) > len(trades) * 0.4:
                    insights.append(
                        f"first_green: {len(kept_running)}/{len(trades)} trades ran 3%+ further — "
                        f"consider raising first_green threshold"
                    )

        return insights
