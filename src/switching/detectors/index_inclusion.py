"""S&P / Russell index-inclusion detector.

When a stock is added to the S&P 500 (or S&P 400 / S&P 600 / Russell 1000),
index funds are forced to buy it, producing a well-documented single-day
reprice on the announcement. Russell reconstitution (annual) produces a
batch of such events in June.

Source: S&P Dow Jones Indices and FTSE Russell press releases. Historical
coverage uses a hand-curated seed CSV; ``pull_live`` is a stub for v1
(S&P release HTML is brittle and an LLM or structured scrape is the
proper upgrade path).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Iterable

from switching.detectors.base import Detector
from switching.registry import register
from switching.signal import Signal
from switching.sources import rss
from switching.sources.sec_edgar import EdgarClient

log = logging.getLogger(__name__)


_INDEX_RX = re.compile(r"(?i)(S&P\s+(?:500|400|600)|Russell\s+(?:1000|2000)|FTSE\s+(?:100|250|All[\s\-]Share))")
_ACTION_RX = re.compile(
    r"(?i)(will\s+be\s+added|added\s+to|addition|includ(?:ed|ing)\s+in|join(?:ing)?\s+the"
    r"|will\s+replace|replac(?:es|ing)"
    r"|will\s+be\s+removed|removed\s+from|deletion|deleted\s+from|drop(?:ped|s)?\s+from)"
)
_TICKER_RX = re.compile(r"\b([A-Z]{1,5})\b(?:\s*\((?:NYSE|NASDAQ|NASDAQ\s*GS)\))?")


@register
class IndexInclusionDetector(Detector):
    name = "index_inclusion"
    description = (
        "Additions to S&P 500 / 400 / 600 and Russell 1000. Announcement-day "
        "signal; severity scales with the index tier (larger index = larger "
        "forced passive flow)."
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
            ticker = item.extract_ticker() or _guess_ticker(item.title)
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
                extra={"index": match["index"], "direction": match["direction"]},
            )
        log.info(
            "%s: %d items, %d classified, %d with ticker",
            self.name, len(items), classified, with_ticker,
        )


def classify(title: str, summary: str = "") -> dict | None:
    text = f"{title}\n{summary}"
    index_match = _INDEX_RX.search(text)
    action_match = _ACTION_RX.search(text)
    if not (index_match and action_match):
        return None
    index_label = _normalize_index(index_match.group(1))
    direction = _classify_direction(text)
    severity = _base_severity_for_index(index_label)
    if direction == "delete":
        severity = max(0.30, severity - 0.10)
    return {
        "index": index_label,
        "direction": direction,
        "severity": round(severity, 3),
        "evidence": _evidence_snippet(text, index_match, action_match),
    }


def _normalize_index(raw: str) -> str:
    s = re.sub(r"\s+", " ", raw).strip().upper()
    s = s.replace("S&P ", "S&P 500").replace("S&P 500500", "S&P 500")
    # Re-parse to get clean label.
    if "S&P" in s and "500" in raw:
        return "S&P 500"
    if "S&P" in s and "400" in raw:
        return "S&P 400"
    if "S&P" in s and "600" in raw:
        return "S&P 600"
    if "RUSSELL" in s and "1000" in raw.upper():
        return "Russell 1000"
    if "RUSSELL" in s and "2000" in raw.upper():
        return "Russell 2000"
    if "FTSE" in s and "100" in raw.upper() and "250" not in raw.upper():
        return "FTSE 100"
    if "FTSE" in s and "250" in raw.upper():
        return "FTSE 250"
    if "FTSE" in s and ("ALL" in s or "ALL-SHARE" in s.replace(" ", "")):
        return "FTSE All-Share"
    return s


def _base_severity_for_index(label: str) -> float:
    return {
        "S&P 500": 0.90,
        "S&P 400": 0.70,
        "S&P 600": 0.55,
        "Russell 1000": 0.60,
        "Russell 2000": 0.40,
        "FTSE 100": 0.85,
        "FTSE 250": 0.65,
        "FTSE All-Share": 0.50,
    }.get(label, 0.50)


def _classify_direction(text: str) -> str:
    if re.search(r"(?i)\b(removed|deletion|deleted|drop|drops|will\s+be\s+removed)\b", text):
        return "delete"
    return "add"


def _evidence_snippet(text: str, *matches: re.Match | None) -> str:
    spans = sorted(m.span() for m in matches if m is not None)
    if not spans:
        return text[:160].strip()
    start = max(0, spans[0][0] - 40)
    end = min(len(text), spans[-1][1] + 60)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _guess_ticker(title: str) -> str | None:
    # Titles often include "(TICKER)" alongside company name.
    match = re.search(r"\(([A-Z]{1,5})\)", title)
    if match:
        return match.group(1)
    return None


def _company_from_headline(title: str) -> str:
    return re.split(r"\s+(?:to\s+Join|Added|Joins|Will\s+Join)\b", title, maxsplit=1)[0].strip()


def pull_live(
    client: EdgarClient,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[Signal]:
    """Stub for v1. S&P press-release scraping is roadmap."""
    return []
