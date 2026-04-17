"""Activist 13D filing detector.

Schedule 13D is filed when a party crosses the 5% beneficial-ownership
threshold and has an intent to influence the issuer. Activist 13Ds are
well-documented single-day catalysts; the market typically reprices the
target on the filing.

Source: EDGAR Schedule 13D / 13D/A filings. This detector is live-seed
first — unlike the press-release-based detectors, 13Ds are only discoverable
via EDGAR. ``scan()`` therefore pulls from EDGAR when configured with a
client; without one it yields nothing.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Iterable

from switching.detectors._activist_filers import is_top_tier, match
from switching.detectors.base import Detector
from switching.registry import register
from switching.signal import Signal
from switching.sources.sec_edgar import EdgarClient, Filing

log = logging.getLogger(__name__)


@register
class Activist13DDetector(Detector):
    name = "activist_13d"
    description = (
        "Schedule 13D / 13D-A filings by a curated allowlist of activist "
        "investors (Icahn, Elliott, Starboard, Pershing, Trian, ...). Fresh "
        "13Ds score higher than amendments."
    )

    def __init__(self, client: EdgarClient | None = None) -> None:
        self._client = client

    def scan(self, since: datetime) -> Iterable[Signal]:
        if self._client is None:
            log.info("activist_13d scan requires an EdgarClient; nothing to yield")
            return
        yield from pull_live(self._client, since=since, until=None)


def pull_live(
    client: EdgarClient,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[Signal]:
    start = _as_date(since) if since else date(2020, 1, 1)
    end = _as_date(until) if until else date.today()
    try:
        filings = client.search_filings(
            forms=["SC 13D", "SC 13D/A"], since=start, until=end
        )
    except Exception as exc:  # pragma: no cover
        log.warning("EDGAR 13D search failed: %s", exc)
        return []
    out: list[Signal] = []
    for f in filings:
        sig = filing_to_signal(f)
        if sig is not None:
            out.append(sig)
    return out


def filing_to_signal(filing: Filing) -> Signal | None:
    # The filer name sits in `filer`; fall back to the raw EDGAR
    # display_names list (last entry is the filer when the subject is first).
    filer = filing.filer
    if not filer:
        raw_names = filing.extra.get("raw", {}).get("display_names") or []
        # Typical shape for 13D: [subject, filer]; fall back to last entry.
        if len(raw_names) >= 2:
            filer = raw_names[-1].split(" (CIK")[0].strip()
    matched = match(filer)
    if not matched:
        return None
    if not filing.ticker:
        return None
    is_amendment = filing.form.upper().endswith("/A")

    severity = 0.70
    if is_top_tier(filer):
        severity += 0.15
    if not is_amendment:
        severity += 0.10
    # Bonus when the filing reports a small/mid-cap stake target.
    if filing.reported_pct is not None and filing.reported_pct >= 5.0:
        severity += 0.02
    severity = min(severity, 0.95)

    dt = datetime.combine(filing.filed, datetime.min.time(), tzinfo=timezone.utc)
    return Signal(
        detector="activist_13d",
        ticker=filing.ticker,
        company=filing.company,
        event_dt=dt,
        headline=f"{filer} files {filing.form} on {filing.company}",
        url=filing.url,
        evidence=f"{filer} — {filing.form} filed {filing.filed.isoformat()}",
        severity=round(severity, 3),
        extra={
            "filer": filer,
            "is_amendment": is_amendment,
            "reported_pct": filing.reported_pct,
        },
    )


def _as_date(v):
    if hasattr(v, "date"):
        return v.date()
    return v
