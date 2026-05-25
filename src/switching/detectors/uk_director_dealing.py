"""UK Director / PDMR dealing detector.

Detects RNS announcements where a director or PDMR (Person Discharging
Managerial Responsibilities) has purchased shares. Director buys are
a high-conviction signal on the LSE, particularly when multiple directors
buy in the same week (cluster). Sells are excluded — insider selling has
weaker predictive value.

Historical examples: countless small-cap recoveries telegraphed by
director purchases at or near bottoms.

Source: UK_FEEDS (Google News RSS — the old Investegate/Reuters RNS feeds died;
see rss.py and the roadmap "primary-source ingestion" item).
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

_DEALING_RX = re.compile(
    r"(?i)(?:"
    r"director[s]?\s+(?:dealing|shareholding|purchase|dealing\s+notification)"
    r"|pdmr\s+(?:dealing|shareholding|notification)"
    r"|notification\s+of\s+(?:director|pdmr|major\s+shareholder)"
    r"|director[s]?\s+(?:and|&)\s+pdmr\s+(?:dealing|notification)"
    r")"
)

_BUY_RX = re.compile(
    r"(?i)(?:purchase[sd]?|acquisition|bought|buying|subscribed)"
)

_SELL_RX = re.compile(
    r"(?i)(?:sale|sold|disposal|disposed|selling)"
)

_CLUSTER_RX = re.compile(
    r"(?i)(?:multiple|several|number\s+of)\s+(?:directors?|pdmrs?)"
)


@register
class UKDirectorDealingDetector(Detector):
    name = "uk_director_dealing"
    description = (
        "UK Director / PDMR share purchases (RNS notifications). "
        "Director buys are a high-conviction bullish signal on the LSE, "
        "especially cluster buys. Sells are excluded."
    )

    def __init__(self, feeds: tuple[str, ...] | None = None) -> None:
        self._feeds = feeds

    def scan(self, since: datetime) -> Iterable[Signal]:
        feeds = self._feeds or rss.UK_FEEDS
        items = rss.fetch(feeds, since=since, market="uk")
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
                extra={"direction": match["direction"]},
            )
        log.info(
            "%s: %d items, %d classified, %d with ticker",
            self.name, len(items), classified, with_ticker,
        )


def classify(title: str, summary: str = "") -> dict | None:
    """Return match metadata if the text looks like a director/PDMR purchase.

    Returns a dict with keys: severity, evidence, direction — or None if no match.

    Rules:
    - Must match _DEALING_RX (director/PDMR announcement pattern)
    - Must match _BUY_RX OR have no direction language at all (neutral = flag)
    - Must NOT be sell-only (sells have weak predictive value)
    """
    text = f"{title}\n{summary}"

    dealing_m = _DEALING_RX.search(text)
    if not dealing_m:
        return None

    buy_m = _BUY_RX.search(text)
    sell_m = _SELL_RX.search(text)

    # Sell-only: skip
    if sell_m and not buy_m:
        return None

    severity = 0.65  # Base severity for director purchase

    # Cluster buy bonus
    if _CLUSTER_RX.search(text):
        severity += 0.10

    severity = min(severity, 0.95)

    evidence = _evidence_snippet(text, dealing_m, buy_m)
    direction = "buy" if buy_m else "neutral"

    return {
        "severity": round(severity, 3),
        "evidence": evidence,
        "direction": direction,
    }


def _evidence_snippet(text: str, *matches: re.Match | None) -> str:
    spans = sorted(m.span() for m in matches if m is not None)
    if not spans:
        return text[:160].strip()
    start = max(0, spans[0][0] - 40)
    end = min(len(text), spans[-1][1] + 60)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _company_from_headline(title: str) -> str:
    """Best-effort company name extraction from an RNS dealing headline."""
    # "Barclays PLC (BARC) - Director/PDMR Shareholding"
    m = re.match(r"^([A-Za-z][A-Za-z0-9 &,.']+?)\s*[\(\-]", title)
    if m:
        return m.group(1).strip()
    return title.split(" ")[0]
