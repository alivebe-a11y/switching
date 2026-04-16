"""Insider-cluster buying detector.

Thesis: when three or more distinct insiders at the same issuer report
open-market purchases within a 30-day rolling window, the buying is
meaningfully different from routine grants / exercises and is a
historically positive signal for forward returns.

Signal date = date of the third (or later) qualifying purchase in the
window. Severity scales with how many of the buyers are C-suite and with
total dollar size.

Source: EDGAR Form 4 filings (transaction code ``P`` — open-market
purchase). Full Form-4 XML parsing is beyond v1; ``pull_live`` returns
placeholder signals derived from filing metadata so callers can at least
backtest against seed events. For production use, replace ``pull_live``
with a full Form 4 parser (see the roadmap in README).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence

from switching.detectors.base import Detector
from switching.registry import register
from switching.signal import Signal
from switching.sources.sec_edgar import EdgarClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InsiderPurchase:
    ticker: str
    issuer: str
    insider_name: str
    insider_title: str
    transaction_date: date
    dollar_amount: float
    transaction_code: str = "P"   # P = open-market purchase
    url: str = ""


_CSUITE_RX = re.compile(r"(?i)\b(chief\s+\w+\s+officer|ceo|cfo|coo|cto|president)\b")
_DIRECTOR_RX = re.compile(r"(?i)\b(director|board)\b")


def classify_role(title: str) -> str:
    """Return 'csuite', 'director', or 'other'."""
    if _CSUITE_RX.search(title or ""):
        return "csuite"
    if _DIRECTOR_RX.search(title or ""):
        return "director"
    return "other"


@register
class InsiderClusterDetector(Detector):
    name = "insider_cluster"
    description = (
        "Three or more distinct insiders at the same issuer reporting "
        "open-market purchases within a 30-day rolling window."
    )

    def __init__(
        self,
        client: EdgarClient | None = None,
        *,
        min_insiders: int = 3,
        window_days: int = 30,
        min_aggregate_usd: float = 100_000.0,
    ) -> None:
        self._client = client
        self._min = min_insiders
        self._window = window_days
        self._floor = min_aggregate_usd

    def scan(self, since: datetime) -> Iterable[Signal]:
        if self._client is None:
            log.info("insider_cluster scan requires an EdgarClient; nothing to yield")
            return
        purchases = _pull_form4_purchases(self._client, since=since)
        yield from detect_clusters(
            purchases,
            min_insiders=self._min,
            window_days=self._window,
            min_aggregate_usd=self._floor,
        )


def detect_clusters(
    purchases: Sequence[InsiderPurchase],
    *,
    min_insiders: int = 3,
    window_days: int = 30,
    min_aggregate_usd: float = 100_000.0,
) -> list[Signal]:
    """Group purchases by ticker and emit one signal per qualifying cluster.

    A cluster is a maximal set of purchases in a ``window_days`` rolling
    window where at least ``min_insiders`` *distinct* insiders bought and
    the aggregate dollar amount clears ``min_aggregate_usd``. To avoid
    emitting overlapping clusters on the same issuer, after a cluster
    closes at date ``D`` we skip further cluster emissions until the next
    purchase dated > ``D + window_days``.
    """
    by_ticker: dict[str, list[InsiderPurchase]] = {}
    for p in purchases:
        if p.transaction_code.upper() != "P":
            continue
        by_ticker.setdefault(p.ticker.upper(), []).append(p)

    signals: list[Signal] = []
    for ticker, rows in by_ticker.items():
        rows_sorted = sorted(rows, key=lambda r: r.transaction_date)
        i = 0
        while i < len(rows_sorted):
            end_date = rows_sorted[i].transaction_date
            window_start = end_date - timedelta(days=window_days)
            in_window = [
                r for r in rows_sorted[: i + 1] if r.transaction_date >= window_start
            ]
            insiders = {r.insider_name for r in in_window}
            aggregate = sum(r.dollar_amount for r in in_window)
            if len(insiders) >= min_insiders and aggregate >= min_aggregate_usd:
                sig = _cluster_to_signal(ticker, in_window)
                signals.append(sig)
                # Skip ahead past this window to avoid overlap emissions.
                skip_after = end_date + timedelta(days=window_days)
                while i < len(rows_sorted) and rows_sorted[i].transaction_date <= skip_after:
                    i += 1
                continue
            i += 1
    return signals


def _cluster_to_signal(ticker: str, members: Sequence[InsiderPurchase]) -> Signal:
    roles = [classify_role(m.insider_title) for m in members]
    csuite_count = sum(1 for r in roles if r == "csuite")
    director_count = sum(1 for r in roles if r == "director")
    aggregate = sum(m.dollar_amount for m in members)

    severity = 0.60
    if csuite_count >= 2:
        severity += 0.15
    elif csuite_count == 1:
        severity += 0.05
    if aggregate >= 1_000_000:
        severity += 0.10
    if director_count == len(members) and director_count >= 3:
        severity += 0.05
    severity = min(severity, 0.95)

    end_date = max(m.transaction_date for m in members)
    issuer = members[0].issuer
    dt = datetime.combine(end_date, datetime.min.time(), tzinfo=timezone.utc)
    insider_names = ", ".join(sorted({m.insider_name for m in members}))
    headline = (
        f"Insider cluster buying at {issuer} — "
        f"{len({m.insider_name for m in members})} insiders, ${aggregate:,.0f}"
    )
    return Signal(
        detector="insider_cluster",
        ticker=ticker,
        company=issuer,
        event_dt=dt,
        headline=headline,
        url=members[-1].url,
        evidence=f"insiders: {insider_names}",
        severity=round(severity, 3),
        extra={
            "insiders": sorted({m.insider_name for m in members}),
            "csuite_count": csuite_count,
            "director_count": director_count,
            "aggregate_usd": aggregate,
            "window_end": end_date.isoformat(),
        },
    )


def _pull_form4_purchases(
    client: EdgarClient, *, since: datetime
) -> list[InsiderPurchase]:
    """Placeholder: emit nothing from Form 4 metadata alone.

    Real implementation requires fetching each Form 4 XML and parsing
    transaction lines. That's out of scope for v1 — the detector's value
    is in the clustering logic, which is reached via the seed CSV.
    """
    log.info("pull_live for insider_cluster is a stub; use the seed CSV for backtests")
    return []


def pull_live(
    client: EdgarClient,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[Signal]:
    """Hook used by ``--live-seed``. Returns empty; real Form 4 parsing is roadmap."""
    return []


def _as_date(v):
    if hasattr(v, "date"):
        return v.date()
    return v


# `date` is imported for `InsiderPurchase.transaction_date` typing callers.
__all__ = [
    "InsiderClusterDetector",
    "InsiderPurchase",
    "classify_role",
    "detect_clusters",
    "pull_live",
    "date",
]
