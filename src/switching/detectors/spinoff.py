"""Corporate spinoff / split-off / carve-out detector.

Spinoffs create a new publicly traded entity from a parent company.
The thesis: spinoff stocks frequently outperform in the first 12-24
months as dedicated management, simplified capital allocation, and
index-inclusion mechanics drive re-rating.

Source: PR Newswire / BusinessWire corp-fin feeds for live scanning, SEC
EDGAR 8-K filings for backtest live-seed.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Iterable

from switching.detectors.base import Detector
from switching.registry import register
from switching import detection_funnel
from switching.signal import Signal
from switching.sources import rss
from switching.sources.sec_edgar import EdgarClient, Filing

log = logging.getLogger(__name__)

_SPINOFF_RX = re.compile(
    r"(?i)(?:spin[\s\-]?off|spins\s+off|spinning\s+off|split[\s\-]off|carve[\s\-]?out)"
)
_SEPARATION_RX = re.compile(
    r"(?i)(?:plans?\s+to\s+separate|plans?\s+to\s+split|tax[\s\-]free\s+distribution"
    r"|independent\s+company|standalone\s+company)"
)
_ACTION_RX = re.compile(
    r"(?i)(?:announces|plans|completes?|completed|board\s+approves?|board\s+approved"
    r"|will\s+separate|to\s+create)"
)
_COMPLETED_RX = re.compile(r"(?i)\b(?:completes?|completed)\b")
_TAX_FREE_RX = re.compile(r"(?i)tax[\s\-]free")
_BOARD_APPROVED_RX = re.compile(r"(?i)board\s+approved?")


@register
class SpinoffDetector(Detector):
    name = "spinoff"
    description = (
        "Corporate spinoff, split-off, and carve-out announcements. Matches "
        "press releases combining a spinoff/separation noun with a corporate "
        "action verb (announces, completes, board approves, etc.)."
    )

    def __init__(self, feeds: tuple[str, ...] | None = None) -> None:
        self._feeds = feeds

    def scan(self, since: datetime) -> Iterable[Signal]:
        feeds = self._feeds or (rss.DEFAULT_FEEDS + rss.CORPORATE_FEEDS)
        items = rss.fetch(feeds, since=since)
        classified = 0
        with_ticker = 0
        for item in items:
            match = classify(item.title, item.summary)
            if match is None:
                continue
            classified += 1
            ticker = item.extract_ticker()
            if not ticker:
                detection_funnel.record_drop(self.name, item)
                continue
            with_ticker += 1
            yield Signal(
                detector=self.name,
                ticker=ticker,
                company=_company_from_headline(item.title),
                event_dt=item.published,
                headline=item.title,
                url=item.url,
                evidence=match["evidence"],
                severity=match["severity"],
                extra={"spinoff_type": match["type"]},
            )
        log.info(
            "%s: %d items, %d classified, %d with ticker",
            self.name, len(items), classified, with_ticker,
        )


def classify(title: str, summary: str = "") -> dict | None:
    """Return match metadata if the text looks like a spinoff announcement."""
    text = f"{title}\n{summary}"
    spinoff = _SPINOFF_RX.search(text) or _SEPARATION_RX.search(text)
    action = _ACTION_RX.search(text)
    if not (spinoff and action):
        return None

    spinoff_type = _detect_type(text)

    severity = 0.70
    if _COMPLETED_RX.search(text):
        severity += 0.10
    if _TAX_FREE_RX.search(text):
        severity += 0.10
    if _BOARD_APPROVED_RX.search(text):
        severity += 0.05
    severity = min(severity, 0.95)

    return {
        "type": spinoff_type,
        "evidence": _evidence_snippet(text, spinoff, action),
        "severity": round(severity, 3),
    }


def _detect_type(text: str) -> str:
    if re.search(r"(?i)split[\s\-]off", text):
        return "split-off"
    if re.search(r"(?i)carve[\s\-]?out", text):
        return "carve-out"
    return "spinoff"


def _evidence_snippet(text: str, *matches: re.Match | None) -> str:
    spans = sorted(m.span() for m in matches if m is not None)
    if not spans:
        return text[:160].strip()
    start = max(0, spans[0][0] - 40)
    end = min(len(text), spans[-1][1] + 60)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _company_from_headline(title: str) -> str:
    return re.split(
        r"\s+(?:Announces|Completes|Plans|Approves|Launches|Reports|Will)\b",
        title,
        maxsplit=1,
    )[0].strip()


def pull_live(
    client: EdgarClient,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[Signal]:
    """Fetch 8-K filings that reference a spinoff in the date range."""
    start = _as_date(since) if since else date(2020, 1, 1)
    end = _as_date(until) if until else date.today()
    try:
        filings = client.search_filings(
            forms=["8-K"],
            since=start,
            until=end,
            query='"spin off" OR "spinoff"',
        )
    except Exception as exc:  # pragma: no cover
        log.warning("EDGAR 8-K search failed: %s", exc)
        return []
    out: list[Signal] = []
    for f in filings:
        sig = _filing_to_signal(f)
        if sig is not None:
            out.append(sig)
    return out


def _filing_to_signal(f: Filing) -> Signal | None:
    if not f.ticker:
        return None
    dt = datetime.combine(f.filed, datetime.min.time(), tzinfo=timezone.utc)
    return Signal(
        detector="spinoff",
        ticker=f.ticker,
        company=f.company,
        event_dt=dt,
        headline=f"{f.company} — 8-K referencing spinoff",
        url=f.url,
        evidence="",
        severity=0.70,
    )


def _as_date(v):
    if hasattr(v, "date"):
        return v.date()
    return v
