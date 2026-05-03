"""Government / defense contract win detector.

Captures large government and defense contract awards. These are
strong bullish catalysts — a multi-billion-dollar DoD or federal
contract can move a stock 5-20% depending on relative size.

Source: DEFAULT_FEEDS + CORPORATE_FEEDS for live scanning. Major
contract awards are press-released via PR Newswire / BusinessWire
and also published in government contract databases (SAM.gov).
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

_CONTRACT_AWARD_RX = re.compile(
    r"(?i)(?:"
    r"(?:awarded?|receives?|wins?|secures?|selected\s+for)\s+(?:a\s+)?\$[\d,.]+"
    r"[\s\-]*(?:million|billion|M|B)?\b.*?(?:contract|deal|agreement|task\s+order|delivery\s+order)"
    r"|contract\s+(?:award|win)\s+(?:worth|valued\s+at)\s+\$"
    r")"
)

_GOV_CONTRACT_RX = re.compile(
    r"(?i)(?:"
    r"(?:Department\s+of\s+Defense|DoD|Pentagon|U\.?S\.?\s+Army|U\.?S\.?\s+Navy|U\.?S\.?\s+Air\s+Force|U\.?S\.?\s+Space\s+Force)"
    r"|(?:NASA|FAA|DARPA|DHS|Department\s+of\s+Homeland)"
    r"|(?:Department\s+of\s+(?:Energy|Veterans\s+Affairs|Health|State|Interior))"
    r"|(?:federal\s+(?:contract|award)|government\s+(?:contract|award))"
    r"|(?:GSA|General\s+Services\s+Admin)"
    r"|(?:IDIQ|indefinite[- ]delivery)"
    r")"
)

_DEFENSE_KEYWORDS_RX = re.compile(
    r"(?i)(?:"
    r"defense|military|missile|weapon|fighter\s+jet|submarine"
    r"|hypersonic|radar|cybersecurity\s+contract"
    r"|munitions|logistics\s+contract|support\s+services\s+contract"
    r"|bomber|B-\d+\s+\w+|F-\d+|stealth"
    r")"
)

# Dollar amounts
_DOLLAR_RX = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*(million|billion|M|B)?", re.IGNORECASE)

# Multi-year modifier
_MULTI_YEAR_RX = re.compile(
    r"(?i)(?:multi[- ]year|indefinite[- ]delivery|IDIQ|\d+[- ]year)"
)

# Sole-source / no-bid (higher certainty)
_SOLE_SOURCE_RX = re.compile(
    r"(?i)(?:sole[- ]source|single[- ]award|no[- ]bid)"
)


@register
class ContractWinDetector(Detector):
    name = "contract_win"
    description = (
        "Government and defense contract awards. Detects large-dollar "
        "contract wins from DoD, NASA, federal agencies, and other "
        "government entities."
    )

    def __init__(self, feeds: tuple[str, ...] | None = None) -> None:
        self._feeds = feeds

    def scan(self, since: datetime) -> Iterable[Signal]:
        feeds = self._feeds or (rss.DEFAULT_FEEDS + rss.CORPORATE_FEEDS)
        items = rss.fetch(feeds, since=since)
        log.info("contract_win: scanned %d RSS items", len(items))
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
                    "contract_value": match.get("contract_value"),
                    "government": match.get("government", False),
                    "defense": match.get("defense", False),
                    "multi_year": match.get("multi_year", False),
                },
            )


def classify(title: str, summary: str = "") -> dict | None:
    """Return match metadata if the text looks like a contract award.

    Returns a dict with keys: severity, evidence, contract_value,
    government, defense, multi_year — or None if no match.
    """
    text = f"{title}\n{summary}"

    award_m = _CONTRACT_AWARD_RX.search(text)
    if not award_m:
        return None

    gov_m = _GOV_CONTRACT_RX.search(text)
    defense_m = _DEFENSE_KEYWORDS_RX.search(text)
    government = bool(gov_m)
    defense = bool(defense_m)

    base_severity = 0.60

    # Government contract bonus
    if government:
        base_severity += 0.10
    if defense:
        base_severity += 0.05

    # Extract contract dollar value
    contract_value: float | None = None
    d_match = _DOLLAR_RX.search(text)
    if d_match:
        try:
            val = float(d_match.group(1).replace(",", ""))
            unit = (d_match.group(2) or "").lower()
            if unit in ("billion", "b"):
                val *= 1_000_000_000
            elif unit in ("million", "m"):
                val *= 1_000_000
            contract_value = val
        except ValueError:
            pass

    # Large contract bonus
    if contract_value:
        if contract_value >= 1_000_000_000:
            base_severity += 0.15
        elif contract_value >= 500_000_000:
            base_severity += 0.10
        elif contract_value >= 100_000_000:
            base_severity += 0.05

    # Multi-year bonus
    multi_year = bool(_MULTI_YEAR_RX.search(text))
    if multi_year:
        base_severity += 0.05

    severity = min(base_severity, 0.95)

    return {
        "severity": round(severity, 3),
        "evidence": _evidence_snippet(text, award_m, gov_m, defense_m),
        "contract_value": contract_value,
        "government": government,
        "defense": defense,
        "multi_year": multi_year,
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
        r"(?i)(?:awarded?|receives?|wins?|secures?|selected)\s",
        title,
    )
    if m and m.start() > 0:
        return title[: m.start()].strip().rstrip(",")
    return re.split(
        r"\s+(?:Awarded?|Receives?|Wins?|Secures?|Selected)\b",
        title,
        maxsplit=1,
    )[0].strip()
