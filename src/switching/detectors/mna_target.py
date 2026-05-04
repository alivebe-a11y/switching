"""M&A / acquisition announcement detector.

Detects merger and acquisition announcements where a company is being
acquired (the target) or is the acquirer. Target stocks typically pop
20-40% on announcement day as the offer price becomes the de-facto fair
value floor. Acquirer stocks often drift down on dilution concerns.

Source: PR Newswire / BusinessWire general corporate feeds for live
scanning. Definitive agreements and tender-offer docs hit the wires via
press release before SEC filings arrive.
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
# Core event-type regexes
# ---------------------------------------------------------------------------

# Target-side patterns: the subject is being acquired
_TARGET_RX = re.compile(
    r"(?i)"
    r"(?:"
    r"to\s+be\s+acquired\s+by"
    r"|agrees?\s+to\s+be\s+acquired"
    r"|acquired\s+by\b"
    r"|acquisition\s+of\b"
    r"|to\s+acquire\b"
    r"|to\s+buy\b.*?\bfor\s+\$"
    r"|merger\s+agreement"
    r"|definitive\s+agreement"
    r"|tender\s+offer"
    r"|takeover\s+bid"
    r")"
)

# Acquirer-side patterns: subject is doing the buying
_ACQUIRER_RX = re.compile(
    r"(?i)"
    r"(?:"
    r"\bacquires?\b"
    r"|acquiring\b"
    r"|to\s+acquire\b"
    r"|acquisition\s+of\b"
    r"|purchase\s+of\b"
    r"|buys?\b.*?\bfor\s+\$"
    r")"
)

# Premium / price-per-share signals
_PER_SHARE_RX = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s+per\s+share", re.IGNORECASE)
_PREMIUM_RX = re.compile(
    r"represents?\s+a\s+([\d]+(?:\.\d+)?)\s*%\s+premium", re.IGNORECASE
)
_PRICE_GENERAL_RX = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")

# Certainty modifiers
_ALL_CASH_RX = re.compile(r"(?i)\ball[\s\-]cash\b")
_CASH_STOCK_RX = re.compile(r"(?i)\bcash[\s\-]and[\s\-]stock\b")
_DEFINITIVE_RX = re.compile(r"(?i)\bdefinitive\s+agreement\b")
_UNCERTAIN_RX = re.compile(
    r"(?i)\b(?:exploring|in\s+talks?|considering|rumored?|could\s+bid|potential(?:ly)?|may\s+acquire)\b"
)


@register
class MnaTargetDetector(Detector):
    name = "mna_target"
    description = (
        "M&A acquisition announcements — detects when a company is an "
        "acquisition target or acquirer. Severity is highest for all-cash "
        "definitive agreements and lowest for speculative 'in talks' reports."
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
                company=_company_from_headline(item.title),
                event_dt=item.published,
                headline=item.title,
                url=item.url,
                evidence=match["evidence"],
                severity=match["severity"],
                extra={
                    "direction": match["direction"],
                    "all_cash": match.get("all_cash", False),
                    "definitive": match.get("definitive", False),
                    "uncertain": match.get("uncertain", False),
                    "price_per_share": match.get("price_per_share"),
                    "premium_pct": match.get("premium_pct"),
                },
            )
        log.info(
            "%s: %d items, %d classified, %d with ticker",
            self.name, len(items), classified, with_ticker,
        )


def classify(title: str, summary: str = "") -> dict | None:
    """Return match metadata if the text looks like an M&A announcement.

    Returns a dict with keys: direction, severity, evidence, all_cash,
    definitive, uncertain, price_per_share, premium_pct — or None if no match.
    """
    text = f"{title}\n{summary}"

    target_match = _TARGET_RX.search(text)
    acquirer_match = _ACQUIRER_RX.search(text)

    if not (target_match or acquirer_match):
        return None

    # Determine direction: "to be acquired by" and similar target patterns
    # take priority over generic acquirer-side patterns.
    _TARGET_SIDE_RX = re.compile(
        r"(?i)"
        r"(?:"
        r"to\s+be\s+acquired\s+by"
        r"|agrees?\s+to\s+be\s+acquired"
        r"|acquired\s+by\b"
        r"|tender\s+offer"
        r"|takeover\s+bid"
        r"|merger\s+agreement"
        r"|definitive\s+agreement"
        r")"
    )
    if _TARGET_SIDE_RX.search(text) or (
        target_match and not acquirer_match
    ):
        direction = "target"
        base_severity = 0.85
        key_match = target_match
    else:
        direction = "acquirer"
        base_severity = 0.55
        key_match = acquirer_match

    # Certainty modifiers
    all_cash = bool(_ALL_CASH_RX.search(text))
    cash_stock = bool(_CASH_STOCK_RX.search(text))
    definitive = bool(_DEFINITIVE_RX.search(text))
    uncertain = bool(_UNCERTAIN_RX.search(text))

    severity = base_severity
    if all_cash:
        severity += 0.10
    if definitive:
        severity += 0.05
    if uncertain:
        severity -= 0.20
    severity = min(severity, 0.95)
    severity = max(severity, 0.10)

    # Extract price-per-share
    price_per_share: float | None = None
    ps_match = _PER_SHARE_RX.search(text)
    if ps_match:
        try:
            price_per_share = float(ps_match.group(1).replace(",", ""))
        except ValueError:
            pass

    # Extract premium percentage
    premium_pct: float | None = None
    prem_match = _PREMIUM_RX.search(text)
    if prem_match:
        try:
            premium_pct = float(prem_match.group(1))
        except ValueError:
            pass

    evidence = _evidence_snippet(text, key_match, ps_match, prem_match)

    return {
        "direction": direction,
        "severity": round(severity, 3),
        "evidence": evidence,
        "all_cash": all_cash,
        "cash_stock": cash_stock,
        "definitive": definitive,
        "uncertain": uncertain,
        "price_per_share": price_per_share,
        "premium_pct": premium_pct,
    }


def _evidence_snippet(text: str, *matches: re.Match | None) -> str:
    spans = sorted(m.span() for m in matches if m is not None)
    if not spans:
        return text[:160].strip()
    start = max(0, spans[0][0] - 40)
    end = min(len(text), spans[-1][1] + 60)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _company_from_headline(title: str) -> str:
    """Best-effort extraction of the company name from an M&A headline."""
    # "Microsoft to Acquire Activision Blizzard for $95 Per Share"
    m = re.search(
        r"^([A-Za-z][A-Za-z0-9 &,.']+?)\s+(?:to\s+(?:acquire|buy)|announces?|agrees?|signs?)\b",
        title,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    # "Activision Blizzard to Be Acquired by Microsoft"
    m = re.search(
        r"^([A-Za-z][A-Za-z0-9 &,.']+?)\s+(?:to\s+be\s+acquired|agrees?\s+to\b)\b",
        title,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    # Fall back: everything before the first verb-looking word
    return re.split(
        r"\s+(?:Acquires?|Agrees?|Signs?|Announces?|Completes?|Enters?)\b",
        title,
        maxsplit=1,
    )[0].strip()
