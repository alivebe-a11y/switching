"""FDA decision detector.

Captures FDA approvals, rejections, complete response letters (CRLs),
breakthrough therapy designations, fast track designations, priority reviews,
and advisory committee (AdCom) votes from RSS press-release headlines.

The thesis: FDA action dates are a primary binary catalyst for biotech and
pharma stocks. Full approvals cause multi-day rallies; CRLs and rejections
cause sharp sell-offs; designations (breakthrough, fast track) signal
accelerated development paths and attract institutional accumulation.

Source: PR Newswire / BusinessWire / GlobeNewswire general and corporate
feeds for live scanning.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Iterable

from switching.detectors.base import Detector
from switching.registry import register
from switching import detection_funnel
from switching.signal import Signal
from switching.sources import rss

log = logging.getLogger(__name__)

# Pharma-specific RSS feeds (empty for now; general feeds pick up FDA news).
PHARMA_FEEDS: tuple[str, ...] = ()

# ---------------------------------------------------------------------------
# Regex patterns — ordered from most specific to least specific so the first
# matching category wins when categories overlap.
# ---------------------------------------------------------------------------

_APPROVAL_RX = re.compile(
    r"(?i)(?:FDA\s+approves?|receives?\s+FDA\s+approval|FDA\s+grants?\s+approval"
    r"|approved\s+by\s+(?:the\s+)?FDA|FDA\s+approved"
    r"|NDA\s+approved|BLA\s+approved|sNDA\s+approved"
    r"|approved\s+for\s+(?:the\s+)?treatment)"
)

_REJECTION_RX = re.compile(
    r"(?i)(?:FDA\s+rejects?|receives?\s+(?:a\s+)?complete\s+response\s+letter"
    r"|\bCRL\b|complete\s+response\s+letter|FDA\s+issues?\s+(?:a\s+)?CRL"
    r"|not\s+approvable|refuses?\s+to\s+file|RTF\b)"
)

_BREAKTHROUGH_RX = re.compile(
    r"(?i)(?:FDA\s+grants?\s+breakthrough|breakthrough\s+therapy\s+designation"
    r"|receives?\s+breakthrough\s+therapy|granted\s+breakthrough"
    r"|breakthrough\s+designation)"
)

_FAST_TRACK_RX = re.compile(
    r"(?i)(?:fast\s+track\s+designation|receives?\s+fast\s+track"
    r"|FDA\s+grants?\s+fast\s+track|fast-track\s+designation)"
)

_PRIORITY_REVIEW_RX = re.compile(
    r"(?i)(?:priority\s+review\s+designation|receives?\s+priority\s+review"
    r"|granted\s+priority\s+review|FDA\s+grants?\s+priority\s+review"
    r"|\bPDUFA\b)"
)

_ADCOM_RX = re.compile(
    r"(?i)(?:advisory\s+committee|AdCom\s+votes?|FDA\s+panel"
    r"|FDA\s+advisory\s+panel|advisory\s+panel\s+votes?"
    r"|adcom\s+recommends?)"
)

_NDA_BLA_RX = re.compile(
    r"(?i)(?:\bNDA\s+accepted\b|\bBLA\s+accepted\b|\bsNDA\b"
    r"|submits?\s+NDA|submits?\s+BLA|FDA\s+accepts?\s+(?:NDA|BLA|sNDA))"
)

# Bonus modifiers
_FIRST_IN_CLASS_RX = re.compile(
    r"(?i)(?:\bfirst[\s-]in[\s-]class\b|\bnovel\b|\bfirst\s+(?:ever\s+)?approved\b"
    r"|\bfirst\s+(?:and\s+only|treatment)\b)"
)

_BLOCKBUSTER_RX = re.compile(
    r"(?i)(?:blockbuster|peak\s+(?:annual\s+)?sales?|billion[\s-]dollar\s+(?:drug|market)"
    r"|\$\d+\s*[Bb](?:illion)?\s+(?:peak|annual|market)|multi[\s-]billion)"
)

# Ordered list of (pattern, direction) for the classification waterfall
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (_APPROVAL_RX, "approval"),
    (_REJECTION_RX, "rejection"),
    (_BREAKTHROUGH_RX, "breakthrough"),
    (_FAST_TRACK_RX, "fast_track"),
    (_PRIORITY_REVIEW_RX, "priority_review"),
    (_ADCOM_RX, "adcom"),
    (_NDA_BLA_RX, "priority_review"),  # NDA/BLA accepted → treated as priority review tier
]

_BASE_SEVERITY: dict[str, float] = {
    "approval": 0.85,
    "rejection": 0.80,
    "breakthrough": 0.70,
    "adcom": 0.65,
    "fast_track": 0.60,
    "priority_review": 0.55,
}


@register
class FdaDecisionDetector(Detector):
    name = "fda_decision"
    description = (
        "FDA approvals, rejections, complete response letters, breakthrough "
        "therapy designations, fast track designations, priority reviews, and "
        "advisory committee votes from press-release headlines."
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
                extra={"direction": match["direction"]},
            )
        log.info(
            "%s: %d items, %d classified, %d with ticker",
            self.name, len(items), classified, with_ticker,
        )


def classify(title: str, summary: str = "") -> dict | None:
    """Return match metadata if the text looks like an FDA decision/designation.

    Returns a dict with keys ``direction``, ``severity``, and ``evidence``,
    or ``None`` if no FDA-related pattern is detected.
    """
    text = f"{title}\n{summary}"

    matched_pattern: re.Pattern | None = None
    direction: str | None = None
    for pattern, dir_name in _PATTERNS:
        m = pattern.search(text)
        if m:
            matched_pattern = pattern
            direction = dir_name
            break

    if direction is None:
        return None

    severity = _BASE_SEVERITY[direction]

    if _FIRST_IN_CLASS_RX.search(text):
        severity += 0.10
    if _BLOCKBUSTER_RX.search(text):
        severity += 0.05

    severity = min(severity, 0.95)

    key_match = matched_pattern.search(text) if matched_pattern else None
    return {
        "direction": direction,
        "severity": round(severity, 3),
        "evidence": _evidence_snippet(text, key_match),
    }


def _evidence_snippet(text: str, *matches: re.Match | None) -> str:
    spans = sorted(m.span() for m in matches if m is not None)
    if not spans:
        return text[:160].strip()
    start = max(0, spans[0][0] - 40)
    end = min(len(text), spans[-1][1] + 60)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _company_from_headline(title: str) -> str:
    return re.split(
        r"\s+(?:Receives?|Announces?|Reports?|Gets?|Granted|Achieves?|Submits?|Earns?)\b",
        title,
        maxsplit=1,
    )[0].strip()
