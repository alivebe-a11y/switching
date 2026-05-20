"""Stock split announcement detector.

Detects forward stock split announcements. Splits are bullish signals —
companies typically split when the share price has risen substantially,
and the announcement often triggers additional buying from retail investors
who perceive the lower post-split price as more accessible.

Historical pattern: +5-15% in the 10-30 days following a split announcement.

Source: PR Newswire / BusinessWire corporate feeds.
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
# Core regexes
# ---------------------------------------------------------------------------

_SPLIT_RX = re.compile(
    r"(?i)"
    r"(?:"
    # Action verb (required for bare "stock/share split" — avoids historical references)
    r"(?:declares?|announces?|approves?|authorizes?)\s+(?:a\s+)?(?:\d+[\s\-]for[\s\-]\d+\s+)?(?:forward\s+)?(?:stock|share)\s+split"
    # Bare ratio + "stock/share split" — unambiguously a new announcement
    r"|\b\d+[\s\-]for[\s\-]\d+\s+(?:forward\s+)?(?:stock|share)\s+split\b"
    # "splits/split its common shares"
    r"|\bsplit\s+its\s+(?:common\s+)?shares?\b"
    r")"
)

# Reverse split — bearish, exclude
_REVERSE_RX = re.compile(r"(?i)\breverse\s+(?:stock\s+)?split\b")

# Ratio extractor e.g. "3-for-1", "2 for 1"
_RATIO_RX = re.compile(r"(\d+)[\s\-]for[\s\-](\d+)", re.IGNORECASE)

# Board approval — adds conviction
_BOARD_RX = re.compile(r"(?i)\bboard(?:\s+of\s+directors?)?\s+(?:approves?|authorizes?|declares?)")


# ---------------------------------------------------------------------------
# Detector class
# ---------------------------------------------------------------------------

@register
class StockSplitDetector(Detector):
    name = "stock_split"
    description = (
        "Forward stock split announcements — companies splitting shares "
        "typically have strong price momentum. Severity boosted for high "
        "split ratios (3-for-1+). Reverse splits are excluded."
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
                continue
            with_ticker += 1
            yield Signal(
                detector=self.name,
                ticker=ticker,
                company=item.title.split(" ")[0],
                event_dt=item.published,
                headline=item.title,
                url=item.url,
                evidence=match["evidence"],
                severity=match["severity"],
                extra={"split_ratio": match.get("split_ratio")},
            )
        log.info(
            "%s: %d items, %d classified, %d with ticker",
            self.name, len(items), classified, with_ticker,
        )


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------

def classify(title: str, summary: str = "") -> dict | None:
    """Return match metadata if the text describes a forward stock split."""
    text = f"{title}\n{summary}"

    # Exclude reverse splits immediately
    if _REVERSE_RX.search(text):
        return None

    m = _SPLIT_RX.search(text)
    if not m:
        return None

    severity = 0.65

    # Boost for higher split ratios (bigger ratio = more bullish momentum)
    split_ratio: float | None = None
    ratio_str: str | None = None
    ratio_m = _RATIO_RX.search(text)
    if ratio_m:
        numerator = int(ratio_m.group(1))
        denominator = int(ratio_m.group(2))
        if denominator > 0:
            split_ratio = round(numerator / denominator, 2)
            ratio_str = f"{numerator}-for-{denominator}"
            if split_ratio >= 10.0:
                severity += 0.15   # e.g. 10-for-1, 20-for-1 — very high conviction
            elif split_ratio >= 3.0:
                severity += 0.10
            elif split_ratio >= 2.0:
                severity += 0.05

    # Board-of-directors approval adds further conviction
    if _BOARD_RX.search(text):
        severity += 0.10

    severity = min(severity, 0.95)

    evidence = _evidence_snippet(text, m, ratio_m)

    return {
        "severity": round(severity, 3),
        "evidence": evidence,
        "split_ratio": split_ratio,
        "ratio": ratio_str,
    }


def _evidence_snippet(text: str, *matches: re.Match | None) -> str:
    spans = sorted(m.span() for m in matches if m is not None)
    if not spans:
        return text[:160].strip()
    start = max(0, spans[0][0] - 40)
    end = min(len(text), spans[-1][1] + 60)
    return re.sub(r"\s+", " ", text[start:end]).strip()
