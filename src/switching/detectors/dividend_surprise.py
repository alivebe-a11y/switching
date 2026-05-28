"""Unexpected dividend change detector.

Captures special dividends, surprise dividend initiations, and significant
dividend increases or cuts. Special dividends are one-time cash events that
can move stocks 5-15%. Dividend initiations and large increases signal
management confidence; cuts are bearish.

Source: PR Newswire / BusinessWire corporate feeds + earnings feeds for
live scanning. Most dividend announcements hit newswires before the
ex-dividend date appears in pricing databases.
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

# ---------------------------------------------------------------------------
# Core event-type regexes
# ---------------------------------------------------------------------------

_SPECIAL_DIV_RX = re.compile(
    r"(?i)(?:"
    r"special\s+(?:cash\s+)?dividend"
    r"|one[- ]time\s+dividend"
    r"|extraordinary\s+dividend"
    r"|supplemental\s+dividend"
    r"|enhanced\s+capital\s+return"
    r"|special\s+cash\s+distribution"
    r")"
)

_DIV_INITIATION_RX = re.compile(
    r"(?i)(?:"
    r"initiates?\s+(?:quarterly\s+)?dividend"
    r"|declares?\s+(?:its?\s+)?(?:first(?:[- ]ever)?|inaugural|maiden)\s+(?:[\w\-]+\s+){0,2}dividend"
    r"|begins?\s+paying\s+(?:a\s+)?dividend"
    r"|starts?\s+(?:paying\s+)?(?:a\s+)?dividend"
    r")"
)

_DIV_INCREASE_RX = re.compile(
    r"(?i)(?:"
    r"(?:increases?|raises?|hikes?|boosts?|lifts?)\s+(?:quarterly\s+)?dividend"
    r"|dividend\s+(?:increase|raise|hike)"
    r")"
)

_DIV_CUT_RX = re.compile(
    r"(?i)(?:"
    r"(?:cuts?|reduces?|lowers?|slashes?|suspends?|eliminates?)\s+(?:quarterly\s+)?dividend"
    r"|dividend\s+(?:cut|reduction|suspension|elimination)"
    r")"
)

# Dollar value extraction
_DOLLAR_RX = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")

# "per share"
_PER_SHARE_RX = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s+per\s+share", re.IGNORECASE
)

# Percentage increase
_PCT_INCREASE_RX = re.compile(
    r"([\d]+(?:\.\d+)?)\s*%\s+(?:increase|raise|hike)", re.IGNORECASE
)

# Generic "declares dividend" fallback
_DECLARES_DIV_RX = re.compile(
    r"(?i)declares?\s+(?:\$[\d,.]+\s+)?(?:quarterly\s+)?(?:cash\s+)?dividend"
)

# ---------------------------------------------------------------------------
# Title-only no-op-declaration exclusion
# ---------------------------------------------------------------------------
# Live post-mortem (2026-05-28) showed "declares regular quarterly dividend"
# headlines consistently lose: -4.6% stop-outs for TRU, SII, LFVN, CCAP.
# Pattern: vanilla "Declares <Nth/Q-N/Regular> Dividend" with NO change-direction
# language in the TITLE. The classifier was firing on these because the summary
# happened to mention "dividend" — but the actual title is a no-op recurring
# declaration, not a surprise.
_TITLE_VANILLA_DECLARE_RX = re.compile(
    r"(?i)\bdeclares?\s+"
    r"(?:(?:its?|the|a)\s+)?"
    r"(?:first|second|third|fourth|1st|2nd|3rd|4th|q[1-4]|monthly|quarterly|annual|"
    r"semi[-\s]?annual|regular|next|new|the)\s+"
    # Allow up to 4 intermediate tokens like "Quarter 2026 Cash" so real
    # headlines like "Declares First Quarter 2026 Dividend" match.
    r"(?:\w+\s+){0,4}"
    r"(?:cash\s+)?dividend\b"
)

# Title-only POSITIVE indicator: if title has any of these, the announcement is
# a real surprise (increase / special / initiation) — let it through even if
# the vanilla-declare pattern also matches.
_TITLE_HAS_POSITIVE_SIGNAL_RX = re.compile(
    r"(?i)(?:"
    r"\b(?:increases?|raises?|hikes?|boosts?|lifts?)\b"
    r"|\bspecial\s+(?:cash\s+)?dividend\b"
    r"|\bsupplemental\s+dividend\b"
    r"|\bextraordinary\s+dividend\b"
    r"|\bone[- ]time\s+dividend\b"
    r"|\d+%?\s+dividend\s+(?:increase|hike|raise|boost)\b"
    r"|\bdividend\s+(?:increase|hike|raise|boost)\b"
    r"|\binaugural\s+dividend\b"
    r"|\bfirst[- ]ever\s+dividend\b"
    r")"
)


@register
class DividendSurpriseDetector(Detector):
    name = "dividend_surprise"
    description = (
        "Special dividends, dividend initiations, significant increases, "
        "and dividend cuts/suspensions from corporate press releases."
    )

    def __init__(self, feeds: tuple[str, ...] | None = None) -> None:
        self._feeds = feeds

    def scan(self, since: datetime) -> Iterable[Signal]:
        feeds = self._feeds or (
            rss.DEFAULT_FEEDS + rss.EARNINGS_FEEDS + rss.CORPORATE_FEEDS
        )
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
                extra={
                    "direction": match["direction"],
                    "per_share": match.get("per_share"),
                    "pct_increase": match.get("pct_increase"),
                },
            )
        log.info(
            "%s: %d items, %d classified, %d with ticker",
            self.name, len(items), classified, with_ticker,
        )


def classify(title: str, summary: str = "") -> dict | None:
    """Return match metadata if the text looks like a dividend surprise.

    Returns a dict with keys: direction, severity, evidence, per_share,
    pct_increase — or None if no match.
    """
    text = f"{title}\n{summary}"

    # Title-only check: vanilla "Declares Quarterly Dividend" with no
    # change-direction language in the title is a routine no-op announcement —
    # consistently loses in live data. Reject before classification.
    if (_TITLE_VANILLA_DECLARE_RX.search(title)
            and not _TITLE_HAS_POSITIVE_SIGNAL_RX.search(title)):
        return None

    special_m = _SPECIAL_DIV_RX.search(text)
    init_m = _DIV_INITIATION_RX.search(text)
    increase_m = _DIV_INCREASE_RX.search(text)
    cut_m = _DIV_CUT_RX.search(text)

    if special_m:
        direction = "special"
        base_severity = 0.80
        key_match = special_m
    elif init_m:
        direction = "initiation"
        base_severity = 0.70
        key_match = init_m
    elif cut_m:
        direction = "cut"
        base_severity = 0.65
        key_match = cut_m
    elif increase_m:
        direction = "increase"
        base_severity = 0.60
        key_match = increase_m
    else:
        return None

    severity = base_severity

    # Extract per-share amount
    per_share: float | None = None
    ps_match = _PER_SHARE_RX.search(text)
    if ps_match:
        try:
            per_share = float(ps_match.group(1).replace(",", ""))
        except ValueError:
            pass

    # Extract percentage increase
    pct_increase: float | None = None
    pct_match = _PCT_INCREASE_RX.search(text)
    if pct_match:
        try:
            pct_increase = float(pct_match.group(1))
        except ValueError:
            pass

    # Large special dividend bonus
    if direction == "special" and per_share and per_share >= 5.0:
        severity += 0.10

    # Large percentage increase bonus
    if direction == "increase" and pct_increase and pct_increase >= 20.0:
        severity += 0.10

    severity = min(severity, 0.95)

    return {
        "direction": direction,
        "severity": round(severity, 3),
        "evidence": _evidence_snippet(text, key_match),
        "per_share": per_share,
        "pct_increase": pct_increase,
    }


def _evidence_snippet(text: str, *matches: re.Match | None) -> str:
    spans = sorted(m.span() for m in matches if m is not None)
    if not spans:
        return text[:160].strip()
    start = max(0, spans[0][0] - 40)
    end = min(len(text), spans[-1][1] + 60)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _company_from_headline(title: str) -> str:
    """Best-effort extraction of the company name from a headline."""
    m = re.search(
        r"(?i)(?:declares?|announces?|initiates?|increases?|raises?|hikes?|cuts?|reduces?|suspends?|eliminates?)\s",
        title,
    )
    if m and m.start() > 0:
        return title[: m.start()].strip().rstrip(",")
    return re.split(
        r"\s+(?:Declares?|Announces?|Initiates?|Increases?|Raises?|Hikes?|Cuts?|Reduces?|Suspends?|Eliminates?)\b",
        title,
        maxsplit=1,
    )[0].strip()
