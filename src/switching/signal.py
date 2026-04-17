from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class PriceReaction:
    baseline_close: float
    pct_change_1d: float | None
    pct_change_5d: float | None
    volume_ratio: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Signal:
    detector: str
    ticker: str
    company: str
    event_dt: datetime
    headline: str
    url: str
    evidence: str
    severity: float
    price_reaction: PriceReaction | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def with_reaction(self, reaction: PriceReaction | None) -> "Signal":
        return replace(self, price_reaction=reaction)

    def dedup_key(self) -> tuple[str, str, str]:
        normalized = " ".join(self.headline.lower().split())
        return (self.ticker.upper(), self.event_dt.date().isoformat(), normalized)

    def to_dict(self) -> dict[str, Any]:
        event_dt = self.event_dt
        if event_dt.tzinfo is None:
            event_dt = event_dt.replace(tzinfo=timezone.utc)
        return {
            "detector": self.detector,
            "ticker": self.ticker,
            "company": self.company,
            "event_dt": event_dt.isoformat(),
            "headline": self.headline,
            "url": self.url,
            "evidence": self.evidence,
            "severity": self.severity,
            "price_reaction": self.price_reaction.to_dict() if self.price_reaction else None,
            "extra": dict(self.extra),
        }
