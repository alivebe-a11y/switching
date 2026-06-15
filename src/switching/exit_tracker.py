"""Post-exit price tracker.

After a trade is closed, this module tracks the stock's daily OHLC for up to
N days. This answers the key refinement questions:

- Did we exit too early? (stock kept running after first-green exit)
- Was the stop-loss too tight? (stock recovered after stop-loss hit)
- Was hold period right? (stock dropped after hold-expiry, or kept climbing)
- Which day post-exit had the highest intraday price? (optimal exit day)
- What does "average return by day held" look like across all trades?

The tracker runs as part of each scan cycle in the paper-trade loop.
Data is stored in a JSON file alongside the portfolio state.

Accepts either a plain float price function (legacy) or an OHLC dict function
{open, high, low, close}. OHLC is preferred — gives richer per-day data.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

TRACK_DAYS = 20


def _finite(x) -> bool:
    """True only for a real, finite number — filters out None and NaN/inf."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# A post-exit return beyond this band is not signal — it's bad data (classically a
# GBp/GBP price-unit mismatch making close/entry ~100x). Excluded from aggregates +
# insights so a units bug can never again surface as a "+10,000% left on table"
# headline. Defence-in-depth alongside the normalised price feed in paper_trader.
_MAX_PLAUSIBLE_RETURN = 5.0   # +500%


def _plausible(x) -> bool:
    """Finite AND within a believable return band (-100% .. +500%)."""
    return _finite(x) and -1.0 <= x <= _MAX_PLAUSIBLE_RETURN


def _parse_ohlc(raw) -> tuple[float, float, float, float] | None:
    """Accept either a float or {open,high,low,close} dict. Returns (o,h,l,c).

    Rejects non-finite values (NaN/inf). yfinance returns NaN OHLC for thin/AIM
    names on illiquid days; a NaN here would poison the snapshot, never complete
    the 20-day window (NaN math propagates), and — once serialized — produce
    invalid JSON (`NaN` literal) that browsers refuse to parse. Drop the day."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        f = float(raw)
        return None if not math.isfinite(f) else (f, f, f, f)
    try:
        o, h, l, c = (
            float(raw["open"]),
            float(raw["high"]),
            float(raw["low"]),
            float(raw["close"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (o, h, l, c)):
        return None
    return (o, h, l, c)


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
    peak_price: float = 0.0
    snapshots: list[dict] = field(default_factory=list)
    tracking_complete: bool = False

    @property
    def days_tracked(self) -> int:
        return len(self.snapshots)

    @property
    def max_post_exit_return(self) -> float | None:
        """Highest daily close return from entry price, post-exit."""
        if not self.snapshots:
            return None
        return max(s["pct_from_entry"] for s in self.snapshots)

    @property
    def max_intraday_high(self) -> float | None:
        """Highest intraday HIGH reached post-exit (from entry price).
        Answers: 'how high did it actually go, even if it didn't close there?'
        """
        if not self.snapshots:
            return None
        highs = [s["high_pct"] for s in self.snapshots if "high_pct" in s]
        return max(highs) if highs else self.max_post_exit_return

    @property
    def day_of_peak(self) -> int | None:
        """Which day number (1-20) had the highest intraday high post-exit."""
        if not self.snapshots:
            return None
        highs = [(s.get("high_pct", s["pct_from_entry"]), s["day"]) for s in self.snapshots]
        if not highs:
            return None
        return max(highs, key=lambda x: x[0])[1]

    @property
    def min_post_exit_return(self) -> float | None:
        """Lowest daily close return from entry price, post-exit."""
        if not self.snapshots:
            return None
        return min(s["pct_from_entry"] for s in self.snapshots)

    @property
    def final_return(self) -> float | None:
        """Return from entry at end of tracking window."""
        if not self.snapshots:
            return None
        return self.snapshots[-1]["pct_from_entry"]

    @property
    def left_on_table(self) -> float | None:
        """How much more the stock moved (close) after we exited.
        Positive = left money on table. Negative = we got out at the right time.
        """
        max_r = self.max_post_exit_return
        if max_r is None:
            return None
        return max_r - self.pct_return

    @property
    def left_on_table_intraday(self) -> float | None:
        """Left on table using intraday highs — even more conservative view."""
        max_r = self.max_intraday_high
        if max_r is None:
            return None
        return max_r - self.pct_return

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["days_tracked"] = self.days_tracked
        d["max_post_exit_return"] = self.max_post_exit_return
        d["max_intraday_high"] = self.max_intraday_high
        d["day_of_peak"] = self.day_of_peak
        d["min_post_exit_return"] = self.min_post_exit_return
        d["final_return"] = self.final_return
        d["left_on_table"] = self.left_on_table
        d["left_on_table_intraday"] = self.left_on_table_intraday
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
            peak_price=getattr(trade, "peak_price", 0.0),
        ))

    def update(self, get_price_fn: Callable) -> int:
        """Fetch today's OHLC for all active tracked exits. Returns count updated.

        Accepts either a float price function or an OHLC dict function
        {open, high, low, close}. OHLC gives richer per-day snapshot data.
        """
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

            raw = get_price_fn(t.ticker)
            parsed = _parse_ohlc(raw)
            if parsed is None:
                continue

            o, h, l, c = parsed
            pct_from_entry = round(c / t.entry_price - 1.0, 4)
            pct_from_exit  = round(c / t.exit_price  - 1.0, 4)
            high_pct       = round(h / t.entry_price - 1.0, 4)
            low_pct        = round(l / t.entry_price - 1.0, 4)

            t.snapshots.append({
                "date":            today,
                "day":             t.days_tracked + 1,
                "open":            round(o, 4),
                "high":            round(h, 4),
                "low":             round(l, 4),
                "close":           round(c, 4),
                "pct_from_entry":  pct_from_entry,
                "pct_from_exit":   pct_from_exit,
                "high_pct":        high_pct,
                "low_pct":         low_pct,
            })
            updated += 1

            if t.days_tracked >= TRACK_DAYS:
                t.tracking_complete = True

        return updated

    @property
    def active_count(self) -> int:
        return sum(1 for t in self.tracked if not t.tracking_complete)

    def save(self, path: Path, service: str = "us") -> None:
        from switching import storage
        items = [asdict(t) for t in self.tracked]
        storage.save_tracker(path, service, "exit", items)

    @classmethod
    def load(cls, path: Path, service: str = "us") -> ExitTracker:
        from switching import storage
        rows = storage.load_tracker(path, service, "exit")
        tracker = cls()
        constructor_fields = {
            "ticker", "detector", "entry_price", "exit_price",
            "exit_dt", "exit_reason", "pct_return", "headline",
            "peak_price", "snapshots", "tracking_complete",
        }
        for item in rows:
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
            # `_finite` drops both None and NaN/inf (legacy thin-bar snapshots can
            # carry NaN; NaN != None so a plain `is not None` would poison the mean).
            # return-based metrics: drop implausible (bad-data / unit-mismatch) values
            left       = [t.left_on_table          for t in group if _plausible(t.left_on_table)]
            left_intra = [t.left_on_table_intraday  for t in group if _plausible(t.left_on_table_intraday)]
            max_after  = [t.max_post_exit_return    for t in group if _plausible(t.max_post_exit_return)]
            max_high   = [t.max_intraday_high       for t in group if _plausible(t.max_intraday_high)]
            final      = [t.final_return            for t in group if _plausible(t.final_return)]
            day_peaks  = [t.day_of_peak             for t in group if _finite(t.day_of_peak)]  # day number, not a return
            return {
                "count":                  len(group),
                "avg_exit_return":        round(sum(t.pct_return for t in group) / len(group), 4),
                "avg_left_on_table":      round(sum(left)       / len(left),       4) if left       else None,
                "avg_left_on_table_intraday": round(sum(left_intra) / len(left_intra), 4) if left_intra else None,
                "avg_max_post_exit":      round(sum(max_after)  / len(max_after),  4) if max_after  else None,
                "avg_max_intraday_high":  round(sum(max_high)   / len(max_high),   4) if max_high   else None,
                "avg_final_return_day20": round(sum(final)      / len(final),      4) if final      else None,
                "avg_day_of_peak":        round(sum(day_peaks)  / len(day_peaks),  1) if day_peaks  else None,
                "exit_too_early_pct":     round(sum(1 for l in left if l > 0.02)  / len(left), 3) if left else None,
            }

        # By-day averages across all completed tracks — answers "day N = avg X% return"
        by_day: dict[int, dict] = {}
        for t in completed:
            for s in t.snapshots:
                day = s["day"]
                if day not in by_day:
                    by_day[day] = {"close": [], "high": [], "low": [], "n": 0}
                if _plausible(s.get("pct_from_entry")):
                    by_day[day]["close"].append(s["pct_from_entry"])
                if _plausible(s.get("high_pct")):
                    by_day[day]["high"].append(s["high_pct"])
                if _plausible(s.get("low_pct")):
                    by_day[day]["low"].append(s["low_pct"])
                by_day[day]["n"] += 1

        by_day_summary = {}
        for day in sorted(by_day.keys()):
            d = by_day[day]
            by_day_summary[str(day)] = {
                "n":         d["n"],
                "avg_close": round(sum(d["close"]) / len(d["close"]), 4) if d["close"] else None,
                "avg_high":  round(sum(d["high"])  / len(d["high"]),  4) if d["high"]  else None,
                "avg_low":   round(sum(d["low"])   / len(d["low"]),   4) if d["low"]   else None,
            }

        return {
            "completed_tracks": len(completed),
            "by_detector":      {d: _summarize_group(g) for d, g in sorted(by_detector.items())},
            "by_exit_reason":   {r: _summarize_group(g) for r, g in sorted(by_exit_reason.items())},
            "by_day":           by_day_summary,
            "insights":         self._generate_insights(completed),
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
            left = [t.left_on_table for t in trades if _plausible(t.left_on_table)]
            peaks = [t.day_of_peak  for t in trades if _finite(t.day_of_peak)]
            if left:
                avg_left = sum(left) / len(left)
                if avg_left > 0.03:
                    avg_peak_day = round(sum(peaks) / len(peaks), 1) if peaks else "?"
                    insights.append(
                        f"{det}: avg {avg_left:.1%} left on table — "
                        f"peak typically on day {avg_peak_day}; "
                        f"consider raising first_green_pct or extending hold_days"
                    )
                elif avg_left < -0.02:
                    insights.append(
                        f"{det}: stocks drop avg {abs(avg_left):.1%} after exit — "
                        f"exit timing is good, possibly tighten further"
                    )

        for reason, trades in by_exit.items():
            if len(trades) < 3:
                continue
            if reason == "stop_loss":
                recovered = [t for t in trades if _plausible(t.max_post_exit_return) and t.max_post_exit_return > 0]
                if len(recovered) > len(trades) * 0.5:
                    insights.append(
                        f"stop_loss: {len(recovered)}/{len(trades)} trades recovered after stop — "
                        f"consider widening stop-loss"
                    )
            if reason == "first_green":
                kept_running = [t for t in trades if _plausible(t.left_on_table) and t.left_on_table > 0.03]
                if len(kept_running) > len(trades) * 0.4:
                    insights.append(
                        f"first_green: {len(kept_running)}/{len(trades)} trades ran 3%+ further — "
                        f"consider raising first_green threshold"
                    )

        return insights
