"""Mid-quarter guidance change detector.

Captures when companies raise or lower their forward guidance OUTSIDE of
regular earnings announcements. Mid-quarter guidance raises are strong
bullish signals; pre-announcement cuts are bearish.

The key exclusion: if the headline also contains language matching a
regular quarterly earnings report ("reports Q1", "quarterly results", etc.)
the item is left for the earnings_surprise detector instead.

Source: DEFAULT_FEEDS + EARNINGS_FEEDS + CORPORATE_FEEDS for live scanning.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Iterable

from switching.detectors.base import Detector
from switching.registry import register
from switching.signal import Signal
from switching.sources import rss

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exclusion guard — regular quarterly earnings reports
# ---------------------------------------------------------------------------

_EARNINGS_REPORT_RX = re.compile(
    r"(?i)(?:"
    r"reports?\s+Q[1-4]"
    r"|reports?\s+(?:first|second|third|fourth)\s+quarter"
    r"|reports?\s+(?:1st|2nd|3rd|4th)\s+quarter"
    r"|quarterly\s+results"
    r"|quarterly\s+earnings"
    r"|quarterly\s+financial"
    r")"
)

# ---------------------------------------------------------------------------
# Raise / positive guidance patterns
# ---------------------------------------------------------------------------

_RAISE_RX = re.compile(
    r"(?i)(?:"
    r"(?:raises?|increases?|boosts?|lifts?)\s+(?:[\w\-]+\s+){0,3}(?:guidance|outlook|forecast)"
    r"|upward\s+revision"
    r"|revises?\s+(?:[\w\-]+\s+){0,2}guidance\s+upward"
    r"|above\s+previously\s+issued\s+guidance"
    r")"
)

# ---------------------------------------------------------------------------
# Lower / negative guidance patterns
# ---------------------------------------------------------------------------

_LOWER_RX = re.compile(
    r"(?i)(?:"
    r"(?:lowers?|cuts?|reduces?|slashes?)\s+(?:[\w\-]+\s+){0,3}(?:guidance|outlook|forecast)"
    r"|revises?\s+(?:[\w\-]+\s+){0,2}guidance\s+downward"
    r")"
)

# ---------------------------------------------------------------------------
# Pre-announcement patterns
# ---------------------------------------------------------------------------

_PRE_ANNOUNCE_BEAT_RX = re.compile(
    r"(?i)(?:"
    r"pre[- ]announces?\s+(?:results?\s+)?above"
    r"|preliminary\s+results?\s+above"
    r"|pre[- ]announces?\s+beat"
    r")"
)

_PRE_ANNOUNCE_MISS_RX = re.compile(
    r"(?i)(?:"
    r"pre[- ]announces?\s+(?:results?\s+)?below"
    r"|preliminary\s+results?\s+below"
    r"|pre[- ]announces?\s+miss"
    r")"
)

# Generic pre-announcement (needs additional classification context)
_PRE_ANNOUNCE_RX = re.compile(
    r"(?i)pre[- ]announces?"
)

# ---------------------------------------------------------------------------
# Upward-narrowing guidance range
# ---------------------------------------------------------------------------

_NARROW_UPWARD_RX = re.compile(
    r"(?i)narrows?\s+guidance\s+range"
)

# ---------------------------------------------------------------------------
# UK trading update patterns (Investegate / RNS style)
# ---------------------------------------------------------------------------

_UK_POSITIVE_UPDATE_RX = re.compile(
    r"(?i)(?:trading\s+(?:update|statement).*ahead\s+of|ahead\s+of\s+(?:board|full[\s\-]year|current\s+year)\s+(?:expectations|guidance))"
)

# ---------------------------------------------------------------------------
# Modifiers for severity bonuses
# ---------------------------------------------------------------------------

# Large raise: "from $X to $Y" where Y > X by >10%
_FROM_TO_RX = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:million|billion|M|B)?\s+to\s+\$\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Full-year / annual scope bonus
_FULL_YEAR_RX = re.compile(
    r"(?i)(?:full[- ]year|annual|FY\s*\d{2,4})"
)


@register
class GuidanceRaiseDetector(Detector):
    name = "guidance_raise"
    description = (
        "Mid-quarter guidance changes: raises, cuts, and pre-announcements "
        "that occur outside of regular earnings reports. Bullish on raises; "
        "bearish on cuts and pre-announce misses."
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
                    "full_year": match.get("full_year", False),
                    "large_raise": match.get("large_raise", False),
                },
            )
        log.info(
            "%s: %d items, %d classified, %d with ticker",
            self.name, len(items), classified, with_ticker,
        )


def classify(title: str, summary: str = "") -> dict | None:
    """Return match metadata if the text looks like a mid-quarter guidance change.

    Returns a dict with keys: direction, severity, evidence, full_year,
    large_raise — or None if no match.

    Explicitly rejects headlines that also match a regular quarterly earnings
    report, since those belong to the earnings_surprise detector.
    """
    text = f"{title}\n{summary}"

    # Must not be a regular earnings report
    if _EARNINGS_REPORT_RX.search(text):
        return None

    raise_m = _RAISE_RX.search(text)
    lower_m = _LOWER_RX.search(text)
    pa_beat_m = _PRE_ANNOUNCE_BEAT_RX.search(text)
    pa_miss_m = _PRE_ANNOUNCE_MISS_RX.search(text)
    pa_m = _PRE_ANNOUNCE_RX.search(text)
    narrow_m = _NARROW_UPWARD_RX.search(text)
    uk_update_m = _UK_POSITIVE_UPDATE_RX.search(text)

    # Determine direction — priority ordering
    if pa_beat_m:
        direction = "pre_announce_beat"
        base_severity = 0.75
        key_match = pa_beat_m
    elif pa_miss_m:
        direction = "pre_announce_miss"
        base_severity = 0.70
        key_match = pa_miss_m
    elif raise_m:
        direction = "raise"
        base_severity = 0.70
        key_match = raise_m
    elif lower_m:
        direction = "lower"
        base_severity = 0.65
        key_match = lower_m
    elif narrow_m:
        # "Narrows guidance range" — treat as a mild raise
        direction = "raise"
        base_severity = 0.70
        key_match = narrow_m
    elif pa_m:
        # Generic pre-announce without explicit beat/miss — lower confidence
        direction = "pre_announce_beat"
        base_severity = 0.65
        key_match = pa_m
    elif uk_update_m:
        # UK trading update: "trading update... ahead of expectations" (RNS pattern)
        direction = "raise"
        base_severity = 0.70
        key_match = uk_update_m
    else:
        return None

    severity = base_severity

    # Full-year / annual bonus +0.05
    full_year = bool(_FULL_YEAR_RX.search(text))
    if full_year:
        severity += 0.05

    # Large raise bonus +0.10 (raise by >10% via "from $X to $Y")
    large_raise = False
    if direction in ("raise", "pre_announce_beat"):
        ft_match = _FROM_TO_RX.search(text)
        if ft_match:
            try:
                from_val = float(ft_match.group(1).replace(",", ""))
                to_val = float(ft_match.group(2).replace(",", ""))
                if from_val > 0 and (to_val - from_val) / from_val > 0.10:
                    large_raise = True
                    severity += 0.10
            except ValueError:
                pass

    severity = min(severity, 0.95)

    return {
        "direction": direction,
        "severity": round(severity, 3),
        "evidence": _evidence_snippet(text, key_match),
        "full_year": full_year,
        "large_raise": large_raise,
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
        r"(?i)(?:raises?|increases?|lowers?|cuts?|reduces?|revises?|pre[- ]announces?|narrows?)\s",
        title,
    )
    if m and m.start() > 0:
        return title[: m.start()].strip().rstrip(",")
    return re.split(
        r"\s+(?:Raises?|Increases?|Lowers?|Cuts?|Reduces?|Revises?|Pre-[Aa]nnounces?|Narrows?)\b",
        title,
        maxsplit=1,
    )[0].strip()
