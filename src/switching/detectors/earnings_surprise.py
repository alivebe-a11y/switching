"""Earnings-surprise detector.

Captures earnings beats and misses from press-release headlines. The thesis
is straightforward: stocks gap up on beats and down on misses, with
magnitude loosely correlated to the size of the surprise.

Source: PR Newswire / BusinessWire earnings headlines for live scanning,
SEC EDGAR 8-K (Item 2.02) for backtest live-seed.
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
from switching.sources.sec_edgar import EdgarClient

log = logging.getLogger(__name__)

_BEAT_RX = re.compile(
    r"(?i)(?:beats?\s+estimates|earnings?\s+beat|tops?\s+expectations"
    r"|exceeds?\s+expectations|blows?\s+past|crushes?\s+estimates"
    r"|smashes?\s+estimates)"
)
_MISS_RX = re.compile(
    r"(?i)(?:earnings?\s+miss|misses?\s+estimates|falls?\s+short"
    r"|disappoints?|below\s+expectations)"
)
_BIG_BEAT_RX = re.compile(
    r"(?i)(?:crushes?|blows?\s+past|smashes?)"
)
_REVENUE_BEAT_RX = re.compile(
    r"(?i)revenue\s+(?:also\s+)?(?:beats?|tops?|exceeds?)"
)
_GUIDES_LOWER_RX = re.compile(
    r"(?i)(?:warns?|guides?\s+lower|lowers?\s+guidance|cuts?\s+(?:outlook|guidance|forecast))"
)
_EPS_VS_RX = re.compile(
    r"(?i)(?:EPS|earnings)\s+(?:of\s+)?\$(\d+(?:\.\d+)?)\s+vs\.?\s+(?:consensus\s+|expected\s+)?\$(\d+(?:\.\d+)?)"
)
_RESULTS_RX = re.compile(
    r"(?i)(?:reports?\s+(?:first|second|third|fourth|1st|2nd|3rd|4th|q[1-4])\s+"
    r"(?:quarter|fiscal)\s+\d{4}\s+(?:results|earnings|financial))"
)
_RECORD_RX = re.compile(
    r"(?i)(?:record\s+(?:revenue|earnings|results|quarter|net\s+income|EPS)"
    r"|all[- ]time\s+high\s+(?:revenue|earnings))"
)
_RAISES_GUIDANCE_RX = re.compile(
    r"(?i)(?:raises?\s+(?:full[- ]year|annual|FY|guidance|outlook)"
    r"|increases?\s+(?:guidance|outlook)|upward\s+revision)"
)


@register
class EarningsSurpriseDetector(Detector):
    name = "earnings_surprise"
    description = (
        "Earnings beats and misses from press-release headlines. Matches "
        "standard beat/miss language and EPS-vs-consensus figures."
    )

    def __init__(self, feeds: tuple[str, ...] | None = None) -> None:
        self._feeds = feeds

    def scan(self, since: datetime) -> Iterable[Signal]:
        feeds = self._feeds or (rss.DEFAULT_FEEDS + rss.EARNINGS_FEEDS)
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
                    "magnitude": match.get("magnitude"),
                },
            )
        log.info(
            "%s: %d items, %d classified, %d with ticker",
            self.name, len(items), classified, with_ticker,
        )


def classify(title: str, summary: str = "") -> dict | None:
    """Return match metadata if the text looks like an earnings surprise."""
    text = f"{title}\n{summary}"

    beat = _BEAT_RX.search(text)
    miss = _MISS_RX.search(text)
    eps_vs = _EPS_VS_RX.search(text)
    results = _RESULTS_RX.search(text)
    record = _RECORD_RX.search(text)
    raises = _RAISES_GUIDANCE_RX.search(text)

    if not (beat or miss or eps_vs or record or (results and raises)):
        return None

    if eps_vs and not beat and not miss:
        actual = float(eps_vs.group(1))
        expected = float(eps_vs.group(2))
        if actual > expected:
            direction = "beat"
        elif actual < expected:
            direction = "miss"
        else:
            return None
    elif beat and not miss:
        direction = "beat"
    elif miss and not beat:
        direction = "miss"
    elif record:
        direction = "beat"
    elif results and raises:
        direction = "beat"
    elif beat and miss:
        direction = "beat" if beat.start() < miss.start() else "miss"
    else:
        return None

    magnitude = None
    if eps_vs:
        actual = float(eps_vs.group(1))
        expected = float(eps_vs.group(2))
        if expected != 0:
            magnitude = round((actual - expected) / expected, 4)

    if direction == "beat":
        severity = 0.65
        if _BIG_BEAT_RX.search(text):
            severity += 0.15
        if _REVENUE_BEAT_RX.search(text):
            severity += 0.10
        if record:
            severity += 0.10
        if raises:
            severity += 0.05
    else:
        severity = 0.55
        if _GUIDES_LOWER_RX.search(text):
            severity += 0.10

    severity = min(severity, 0.95)

    key_match = beat or miss or eps_vs or record or results
    return {
        "direction": direction,
        "severity": round(severity, 3),
        "evidence": _evidence_snippet(text, key_match, eps_vs),
        "magnitude": magnitude,
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
        r"\s+(?:Reports|Beats|Tops|Exceeds|Misses|Crushes|Blows|Posts|Announces)\b",
        title,
        maxsplit=1,
    )[0].strip()


def pull_live(
    client: EdgarClient,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[Signal]:
    """Stub for v1. 8-K Item 2.02 parsing is roadmap."""
    return []
