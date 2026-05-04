"""Analyst upgrade/downgrade/initiation and price-target detector.

Captures analyst rating changes (upgrades, downgrades, initiations) and
price-target adjustments from financial wire RSS headlines. The thesis is
well-documented: stocks move meaningfully on changes from bulge-bracket
firms, with outsized moves on double upgrades (e.g. Sell → Buy).

Source: PR Newswire / BusinessWire financial feeds + earnings feeds for
live scanning. Analyst notes hit the wires via Benzinga, Briefing.com, and
other newswire distributors.
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
# Analyst firm tiers
# ---------------------------------------------------------------------------

_TOP_TIER_FIRMS = frozenset(
    {
        "Goldman Sachs",
        "Goldman",
        "Morgan Stanley",
        "JP Morgan",
        "JPMorgan",
        "J.P. Morgan",
    }
)

_KNOWN_FIRMS_RX = re.compile(
    r"(?i)\b(?:"
    r"Goldman\s+Sachs|Goldman"
    r"|Morgan\s+Stanley"
    r"|JP\s+Morgan|JPMorgan|J\.P\.\s*Morgan"
    r"|Barclays"
    r"|UBS"
    r"|Citi(?:group|bank)?"
    r"|BofA|Bank\s+of\s+America"
    r"|Wells\s+Fargo"
    r"|Deutsche\s+Bank"
    r"|Jefferies"
    r"|Piper\s+Sandler"
    r"|Raymond\s+James"
    r"|Needham"
    r"|Wedbush"
    r"|Cowen"
    r"|Baird"
    r"|Stifel"
    r"|Mizuho"
    r"|BTIG"
    r"|Oppenheimer"
    r"|Canaccord"
    r"|Truist"
    r"|KeyBanc"
    r"|RBC\s+Capital|Royal\s+Bank"
    r"|Credit\s+Suisse"
    r"|Bernstein"
    r"|Evercore"
    r"|Guggenheim"
    r"|DA\s+Davidson"
    r"|Daiwa"
    r"|HSBC"
    r"|Nomura"
    r")\b"
)

# ---------------------------------------------------------------------------
# Rating vocabularies
# ---------------------------------------------------------------------------

_BUY_RATINGS = r"(?:Buy|Outperform|Overweight|Strong\s+Buy|Positive|Bullish|Market\s+Outperform|Top\s+Pick)"
_SELL_RATINGS = r"(?:Sell|Underperform|Underweight|Strong\s+Sell|Negative|Bearish)"
_NEUTRAL_RATINGS = r"(?:Hold|Neutral|Equal[- ]Weight|Market\s+Perform|In[- ]Line|Sector\s+Perform)"

# double-upgrade: moving from sell → buy (extreme move)
_FROM_SELL_RX = re.compile(
    r"(?i)\bfrom\s+" + _SELL_RATINGS,
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Event-type regexes
# ---------------------------------------------------------------------------

_UPGRADE_RX = re.compile(
    r"(?i)upgrades?\s+\S.*?\bto\s+" + _BUY_RATINGS,
    re.IGNORECASE,
)

_DOWNGRADE_RX = re.compile(
    r"(?i)downgrades?\s+\S.*?\bto\s+" + _SELL_RATINGS,
    re.IGNORECASE,
)

# Also catch: "upgrades X to Neutral / Hold"
_UPGRADE_NEUTRAL_RX = re.compile(
    r"(?i)upgrades?\s+\S.*?\bto\s+" + _NEUTRAL_RATINGS,
    re.IGNORECASE,
)

_INITIATION_RX = re.compile(
    r"(?i)(?:initiates?\s+coverage|starts?\s+coverage|begins?\s+coverage|commences?\s+coverage)"
    r".*?\b(?:with|at|rating\s*[:\-])?\s*" + _BUY_RATINGS,
    re.IGNORECASE,
)

_INITIATION_ANY_RX = re.compile(
    r"(?i)(?:initiates?\s+coverage|starts?\s+coverage|begins?\s+coverage|commences?\s+coverage)",
    re.IGNORECASE,
)

_PT_RAISE_RX = re.compile(
    r"(?i)(?:raises?\s+price\s+target|increases?\s+(?:price\s+)?target|boosts?\s+(?:price\s+)?target|lifts?\s+(?:price\s+)?target)",
    re.IGNORECASE,
)

_PT_LOWER_RX = re.compile(
    r"(?i)(?:lowers?\s+price\s+target|decreases?\s+(?:price\s+)?target|cuts?\s+(?:price\s+)?target|reduces?\s+(?:price\s+)?target|trims?\s+(?:price\s+)?target)",
    re.IGNORECASE,
)

_REITERATE_BUY_RX = re.compile(
    r"(?i)(?:reiterates?|maintains?|keeps?)\s+(?:Buy|Outperform|Overweight).*?"
    r"(?:raises?\s+(?:price\s+)?target|increases?\s+(?:price\s+)?target|boosts?\s+(?:price\s+)?target)",
    re.IGNORECASE,
)

_PT_VALUE_RX = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)",
)


@register
class AnalystUpgradeDetector(Detector):
    name = "analyst_upgrade"
    description = (
        "Analyst rating upgrades, downgrades, initiations, and price-target "
        "changes from financial wire headlines. Weighted by direction, firm "
        "tier, and whether a double upgrade is detected."
    )

    def __init__(self, feeds: tuple[str, ...] | None = None) -> None:
        self._feeds = feeds

    def scan(self, since: datetime) -> Iterable[Signal]:
        feeds = self._feeds or (rss.DEFAULT_FEEDS + rss.EARNINGS_FEEDS)
        items = rss.fetch(feeds, since=since)
        log.info("analyst_upgrade: scanned %d RSS items", len(items))
        for item in items:
            match = classify(item.title, item.summary)
            if match is None:
                continue
            ticker = item.extract_ticker()
            if not ticker:
                continue
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
                    "firm": match.get("firm"),
                    "top_tier": match.get("top_tier", False),
                    "double_upgrade": match.get("double_upgrade", False),
                    "price_target": match.get("price_target"),
                },
            )


def classify(title: str, summary: str = "") -> dict | None:
    """Return match metadata if the text looks like an analyst rating event.

    Returns a dict with keys: direction, severity, evidence, firm,
    top_tier, double_upgrade, price_target — or None if no match.
    """
    text = f"{title}\n{summary}"

    upgrade = _UPGRADE_RX.search(text)
    downgrade = _DOWNGRADE_RX.search(text)
    initiation = _INITIATION_RX.search(text)
    initiation_any = _INITIATION_ANY_RX.search(text)
    pt_raise = _PT_RAISE_RX.search(text)
    pt_lower = _PT_LOWER_RX.search(text)
    reiterate_buy = _REITERATE_BUY_RX.search(text)

    # Determine direction — priority: explicit rating change > initiation > PT
    if upgrade:
        direction = "upgrade"
        base_severity = 0.60
        key_match = upgrade
    elif downgrade:
        direction = "downgrade"
        base_severity = 0.55
        key_match = downgrade
    elif initiation:
        direction = "initiation"
        base_severity = 0.65
        key_match = initiation
    elif initiation_any:
        # Initiation without a clear buy rating — still worth tracking
        direction = "initiation"
        base_severity = 0.55
        key_match = initiation_any
    elif reiterate_buy:
        # "Reiterates Buy, raises target" → treat as pt_raise with slight bump
        direction = "pt_raise"
        base_severity = 0.50
        key_match = reiterate_buy
    elif pt_raise:
        direction = "pt_raise"
        base_severity = 0.50
        key_match = pt_raise
    elif pt_lower:
        direction = "pt_lower"
        base_severity = 0.45
        key_match = pt_lower
    else:
        return None

    # Firm detection
    firm_match = _KNOWN_FIRMS_RX.search(text)
    firm = firm_match.group(0).strip() if firm_match else None

    # Top-tier bonus
    top_tier = False
    if firm:
        for name in _TOP_TIER_FIRMS:
            if name.lower() in firm.lower():
                top_tier = True
                break

    # Double-upgrade bonus (Sell → Buy)
    double_upgrade = bool(_FROM_SELL_RX.search(text)) and direction == "upgrade"

    severity = base_severity
    if top_tier:
        severity += 0.10
    if double_upgrade:
        severity += 0.10
    severity = min(severity, 0.95)

    # Extract price target value if present
    price_target: float | None = None
    pt_matches = _PT_VALUE_RX.findall(text)
    if pt_matches:
        try:
            price_target = float(pt_matches[-1].replace(",", ""))
        except ValueError:
            pass

    return {
        "direction": direction,
        "severity": round(severity, 3),
        "evidence": _evidence_snippet(text, key_match, firm_match),
        "firm": firm,
        "top_tier": top_tier,
        "double_upgrade": double_upgrade,
        "price_target": price_target,
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
    # Many analyst headlines start with the firm name: "Goldman Sachs upgrades Apple to Buy"
    # Try to extract the subject being rated.
    m = re.search(
        r"(?i)(?:upgrades?|downgrades?|initiates?\s+coverage\s+on|raises?\s+price\s+target\s+on|lowers?\s+price\s+target\s+on)\s+([A-Za-z][A-Za-z0-9 &,.']+?)(?:\s+to\b|\s+with\b|\s+at\b)",
        title,
    )
    if m:
        return m.group(1).strip()
    # Fall back: first token sequence before a verb
    return re.split(
        r"\s+(?:Upgrades?|Downgrades?|Initiates?|Raises?|Lowers?|Cuts?|Lifts?|Reiterates?|Maintains?)\b",
        title,
        maxsplit=1,
    )[0].strip()
