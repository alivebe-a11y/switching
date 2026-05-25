"""Crypto treasury adoption detector.

Detects announcements where a company is adding Bitcoin (or other
cryptocurrency) to its corporate treasury. The MicroStrategy pattern:
a company announces it is buying Bitcoin as a reserve asset, which
typically triggers a sharp rally in the stock as crypto-aligned investors
pile in.

Historical examples: MicroStrategy (MSTR), Metaplanet (TYO:3350),
Semler Scientific (SMLR), GameStop (GME) — all spiked 10-40% on
initial treasury adoption announcements.

Source: PR Newswire / BusinessWire corporate and default feeds.
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
# Core regexes
# ---------------------------------------------------------------------------

_CRYPTO_TREASURY_RX = re.compile(
    r"(?i)"
    r"(?:"
    r"bitcoin\s+(?:treasury|reserve|strategy|adoption)"
    r"|adopts?\s+bitcoin\s+(?:as\s+)?(?:a\s+)?(?:treasury|reserve|primary)"
    r"|adds?\s+bitcoin\s+to\s+(?:(?:its|the)\s+)?(?:corporate\s+)?(?:treasury|balance\s+sheet|reserves?)"
    r"|bitcoin\s+as\s+(?:a\s+)?(?:primary\s+)?treasury\s+(?:reserve\s+)?asset"
    r"|corporate\s+bitcoin\s+(?:treasury|strategy|reserve)"
    r"|purchases?\s+bitcoin\s+for\s+(?:its\s+)?(?:treasury|balance\s+sheet)"
    r"|btc\s+treasury"
    r"|microstrategy[\s\-](?:style|like)\s+bitcoin"
    # "acquires/purchases [optional word] N,NNN bitcoin" — covers "Acquires Additional 5,050 Bitcoin"
    r"|(?:acquires?|purchases?)\s+(?:\w+\s+)?[\d,]+\s+bitcoin"
    r")"
)

# Additional crypto assets (lower severity)
_OTHER_CRYPTO_RX = re.compile(
    r"(?i)"
    r"(?:"
    r"ethereum\s+treasury"
    r"|crypto\s+treasury\s+(?:reserve|strategy|adoption)"
    r"|adds?\s+(?:cryptocurrency|crypto)\s+to\s+(?:its\s+)?(?:treasury|balance\s+sheet)"
    r")"
)

# Board approval — confirms institutional commitment
_BOARD_RX = re.compile(r"(?i)\bboard(?:\s+of\s+directors?)?\s+(?:approves?|authorizes?|declares?)")

# Amount extractor — matches "$X million/billion" anywhere in the text
_AMOUNT_RX = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*(billion|million|B|M)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Detector class
# ---------------------------------------------------------------------------

@register
class CryptoTreasuryDetector(Detector):
    name = "crypto_treasury"
    description = (
        "Bitcoin / crypto treasury adoption announcements — companies adding "
        "BTC to their balance sheet (MicroStrategy pattern). Severity highest "
        "for first-time Bitcoin treasury adopters."
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
                company=item.title.split(" ")[0],
                event_dt=item.published,
                headline=item.title,
                url=item.url,
                evidence=match["evidence"],
                severity=match["severity"],
                extra={"asset": match.get("asset", "bitcoin")},
            )
        log.info(
            "%s: %d items, %d classified, %d with ticker",
            self.name, len(items), classified, with_ticker,
        )


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------

def classify(title: str, summary: str = "") -> dict | None:
    """Return match metadata if the text describes a crypto treasury adoption."""
    text = f"{title}\n{summary}"

    btc_match = _CRYPTO_TREASURY_RX.search(text)
    other_match = _OTHER_CRYPTO_RX.search(text) if not btc_match else None

    if not (btc_match or other_match):
        return None

    # Bitcoin treasury = highest conviction signal
    if btc_match:
        severity = 0.75
        asset = "bitcoin"
        key_match = btc_match
    else:
        severity = 0.60
        asset = "crypto"
        key_match = other_match

    # Board approval adds conviction
    if _BOARD_RX.search(text):
        severity += 0.05

    # Boost based on purchase amount — bigger commitment = higher conviction
    amount_match = _AMOUNT_RX.search(text)
    purchase_size: float | None = None
    if amount_match:
        raw = float(amount_match.group(1).replace(",", ""))
        unit = amount_match.group(2).lower()
        purchase_size = raw * 1000.0 if unit in ("billion", "b") else raw  # in millions
        if purchase_size >= 500.0:
            severity += 0.10   # $500M+ — major institutional commitment
        elif purchase_size >= 100.0:
            severity += 0.05   # $100M+ — significant but smaller
        else:
            severity += 0.02   # any stated amount adds minor conviction

    severity = min(severity, 0.95)

    evidence = _evidence_snippet(text, key_match, amount_match)

    return {
        "severity": round(severity, 3),
        "evidence": evidence,
        "asset": asset,
        "purchase_size": purchase_size,
    }


def _evidence_snippet(text: str, *matches: re.Match | None) -> str:
    spans = sorted(m.span() for m in matches if m is not None)
    if not spans:
        return text[:160].strip()
    start = max(0, spans[0][0] - 40)
    end = min(len(text), spans[-1][1] + 60)
    return re.sub(r"\s+", " ", text[start:end]).strip()
