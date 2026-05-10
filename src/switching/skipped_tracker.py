"""Skipped-signal tracker.

When the paper trader can't open a position (max positions hit, cash too low,
already holding the ticker) the signal is otherwise lost. This module records
the price at skip time and runs the same exit logic on it as a real position
so we can answer: "how much P&L did we leave on the table by being capped?"

Tracked alongside the portfolio JSON in `skipped_signals.json`. Capped at the
most recent 500 entries to keep the file bounded.

Used purely for analysis — never affects cash or real positions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from switching.market_calendar import is_trading_day

log = logging.getLogger(__name__)

TRACK_DAYS = 30  # cap on how long we follow a skipped signal
MAX_ENTRIES = 500


@dataclass
class SkippedSignal:
    ticker: str
    detector: str
    severity: float
    headline: str
    skip_reason: str
    skipped_at: str
    would_be_entry_price: float
    hold_days: int
    first_green: bool
    first_green_pct: float
    stop_loss_pct: float
    snapshots: list[dict] = field(default_factory=list)
    tracking_complete: bool = False
    simulated_exit_price: float | None = None
    simulated_exit_reason: str | None = None
    simulated_pct_return: float | None = None
    simulated_exit_dt: str | None = None

    @property
    def days_tracked(self) -> int:
        return len(self.snapshots)

    @property
    def max_pct_return(self) -> float | None:
        if not self.snapshots:
            return None
        return max(s["pct_return"] for s in self.snapshots)

    @property
    def min_pct_return(self) -> float | None:
        if not self.snapshots:
            return None
        return min(s["pct_return"] for s in self.snapshots)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["days_tracked"] = self.days_tracked
        d["max_pct_return"] = self.max_pct_return
        d["min_pct_return"] = self.min_pct_return
        return d


@dataclass
class SkippedTracker:
    skipped: list[SkippedSignal] = field(default_factory=list)

    def add(
        self,
        *,
        ticker: str,
        detector: str,
        severity: float,
        headline: str,
        skip_reason: str,
        price: float,
        hold_days: int,
        first_green: bool,
        first_green_pct: float,
        stop_loss_pct: float,
    ) -> None:
        # Don't double-track the same ticker+detector on the same day
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        for s in self.skipped:
            if s.ticker == ticker and s.detector == detector and s.skipped_at.startswith(today):
                return
        self.skipped.append(SkippedSignal(
            ticker=ticker,
            detector=detector,
            severity=severity,
            headline=headline,
            skip_reason=skip_reason,
            skipped_at=datetime.now(tz=timezone.utc).isoformat(),
            would_be_entry_price=price,
            hold_days=hold_days,
            first_green=first_green,
            first_green_pct=first_green_pct,
            stop_loss_pct=stop_loss_pct,
        ))
        # Trim oldest if over cap
        if len(self.skipped) > MAX_ENTRIES:
            self.skipped = self.skipped[-MAX_ENTRIES:]

    def update(self, get_price_fn) -> int:
        """Fetch today's price for active skipped signals and apply exit rules.

        Skips entirely on weekends and bank holidays — yfinance would return
        stale data and we'd accumulate spurious "days tracked" counts that
        trigger hold_expiry early.
        """
        if not is_trading_day():
            return 0

        updated = 0
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        now_iso = datetime.now(tz=timezone.utc).isoformat()

        for s in self.skipped:
            if s.tracking_complete:
                continue
            if s.days_tracked >= TRACK_DAYS:
                self._finalize(s, reason="track_expiry", price=s.snapshots[-1]["price"] if s.snapshots else s.would_be_entry_price, when=now_iso)
                continue
            if s.snapshots and s.snapshots[-1]["date"] == today:
                continue

            price = get_price_fn(s.ticker)
            if price is None:
                continue

            pct = round(price / s.would_be_entry_price - 1.0, 4)
            s.snapshots.append({
                "date": today,
                "day": s.days_tracked + 1,
                "price": round(price, 4),
                "pct_return": pct,
            })
            updated += 1

            # Apply the same exit logic the real position would have used
            if pct <= -s.stop_loss_pct:
                self._finalize(s, reason="stop_loss", price=price, when=now_iso)
            elif s.first_green and pct >= s.first_green_pct and s.days_tracked >= 1:
                self._finalize(s, reason="first_green", price=price, when=now_iso)
            elif s.days_tracked >= s.hold_days:
                self._finalize(s, reason="hold_expiry", price=price, when=now_iso)

        return updated

    def _finalize(self, s: SkippedSignal, *, reason: str, price: float, when: str) -> None:
        s.simulated_exit_price = round(price, 4)
        s.simulated_exit_reason = reason
        s.simulated_pct_return = round(price / s.would_be_entry_price - 1.0, 4)
        s.simulated_exit_dt = when
        s.tracking_complete = True

    @property
    def active_count(self) -> int:
        return sum(1 for s in self.skipped if not s.tracking_complete)

    @property
    def completed_count(self) -> int:
        return sum(1 for s in self.skipped if s.tracking_complete)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "skipped": [s.to_dict() for s in self.skipped],
            "summary": self._build_summary(),
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> SkippedTracker:
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        tracker = cls()
        constructor_fields = {
            "ticker", "detector", "severity", "headline", "skip_reason",
            "skipped_at", "would_be_entry_price", "hold_days",
            "first_green", "first_green_pct", "stop_loss_pct",
            "snapshots", "tracking_complete",
            "simulated_exit_price", "simulated_exit_reason",
            "simulated_pct_return", "simulated_exit_dt",
        }
        for item in data.get("skipped", []):
            filtered = {k: v for k, v in item.items() if k in constructor_fields}
            tracker.skipped.append(SkippedSignal(**filtered))
        return tracker

    def _build_summary(self) -> dict[str, Any]:
        completed = [s for s in self.skipped if s.tracking_complete and s.simulated_pct_return is not None]
        if not completed:
            return {"completed_count": 0}

        wins = sum(1 for s in completed if s.simulated_pct_return > 0)
        avg_return = sum(s.simulated_pct_return for s in completed) / len(completed)

        by_detector: dict[str, list[SkippedSignal]] = {}
        by_reason: dict[str, list[SkippedSignal]] = {}
        for s in completed:
            by_detector.setdefault(s.detector, []).append(s)
            by_reason.setdefault(s.skip_reason, []).append(s)

        def _agg(group: list[SkippedSignal]) -> dict[str, Any]:
            rets = [g.simulated_pct_return for g in group if g.simulated_pct_return is not None]
            ws = sum(1 for r in rets if r > 0)
            return {
                "count": len(group),
                "win_rate": round(ws / len(rets), 3) if rets else 0,
                "avg_return": round(sum(rets) / len(rets), 4) if rets else 0,
                "best": round(max(rets), 4) if rets else 0,
                "worst": round(min(rets), 4) if rets else 0,
            }

        return {
            "completed_count": len(completed),
            "active_count": self.active_count,
            "would_be_win_rate": round(wins / len(completed), 3),
            "would_be_avg_return": round(avg_return, 4),
            "by_detector": {d: _agg(g) for d, g in sorted(by_detector.items())},
            "by_skip_reason": {r: _agg(g) for r, g in sorted(by_reason.items())},
        }
