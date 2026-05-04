"""Share-buyback authorization detector.

Hundreds of buyback authorizations are announced per year in US equities.
The thesis is well-documented: the stock typically reacts positively to
the announcement, with magnitude correlated to the size of the
authorization relative to market cap.

Source: PR Newswire / BusinessWire corp-fin feeds for live scanning, SEC
EDGAR 8-K filings (Items 7.01 / 8.01) for backtest live-seed.
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
from switching.sources.sec_edgar import EdgarClient, Filing

log = logging.getLogger(__name__)

_AUTHORIZE_RX = re.compile(
    r"(?i)(board|company|directors?)\s+(?:has\s+)?(?:approved|authoriz[a-z]*)"
)
_REPURCHASE_RX = re.compile(
    r"(?i)(?:share\s+)?(?:repurchase|buyback)(?:\s+(?:program|plan|authorization))?"
)
_AMOUNT_RX = re.compile(
    r"(?i)\$?([\d,]+(?:\.\d+)?)\s*(million|billion|mm|bn|m|b)\b"
)
_PROGRAM_RX = re.compile(
    r"(?i)(?:share\s+)?(?:repurchase|buyback)\s+(?:program|plan|authorization)"
)
_ACCELERATED_RX = re.compile(r"(?i)accelerated\s+share\s+repurchase|\bASR\b")


@register
class BuybackDetector(Detector):
    name = "buyback"
    description = (
        "Board-authorized share repurchase programs. Matches press releases "
        "combining an authorize/approve verb with a repurchase-program noun, "
        "plus a dollar size."
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
                extra={"authorization_usd": match.get("amount_usd")},
            )
        log.info(
            "%s: %d items, %d classified, %d with ticker",
            self.name, len(items), classified, with_ticker,
        )


def classify(title: str, summary: str = "") -> dict | None:
    """Return match metadata if the text looks like a buyback authorization."""
    text = f"{title}\n{summary}"
    authorize = _AUTHORIZE_RX.search(text)
    repurchase = _REPURCHASE_RX.search(text)
    if not (authorize and repurchase):
        return None
    amount_match = _AMOUNT_RX.search(text)
    amount_usd = _parse_amount(amount_match) if amount_match else None
    # Require either a dollar size or the tight "repurchase program" phrase.
    # This rejects dividend releases that mention "repurchase activity" in
    # passing without either a size or a program noun.
    if amount_usd is None and not _PROGRAM_RX.search(text):
        return None

    severity = 0.60
    if amount_usd is not None:
        if amount_usd >= 10_000_000_000:      # $10B+
            severity += 0.20
        elif amount_usd >= 1_000_000_000:     # $1B+
            severity += 0.10
        elif amount_usd >= 100_000_000:       # $100M+
            severity += 0.05
    if _ACCELERATED_RX.search(text):
        severity += 0.10
    if _AUTHORIZE_RX.search(title):
        severity += 0.05
    severity = min(severity, 0.95)  # regex alone never earns 1.0

    return {
        "evidence": _evidence_snippet(text, authorize, repurchase, amount_match),
        "severity": round(severity, 3),
        "amount_usd": amount_usd,
    }


def _parse_amount(m: re.Match) -> float | None:
    raw = m.group(1).replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    unit = m.group(2).lower()
    if unit in ("b", "bn", "billion"):
        return value * 1_000_000_000
    if unit in ("m", "mm", "million"):
        return value * 1_000_000
    return value


def _evidence_snippet(text: str, *matches: re.Match | None) -> str:
    spans = sorted(m.span() for m in matches if m is not None)
    if not spans:
        return text[:160].strip()
    start = max(0, spans[0][0] - 40)
    end = min(len(text), spans[-1][1] + 60)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _company_from_headline(title: str) -> str:
    return re.split(
        r"\s+(?:Announces|Authorizes|Approves|Declares|Launches|Reports)\b",
        title,
        maxsplit=1,
    )[0].strip()


def pull_live(
    client: EdgarClient,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[Signal]:
    """Fetch 8-K filings that reference a repurchase program in the date range."""
    start = _as_date(since) if since else date(2020, 1, 1)
    end = _as_date(until) if until else date.today()
    try:
        filings = client.search_filings(
            forms=["8-K"],
            since=start,
            until=end,
            query='"repurchase program"',
        )
    except Exception as exc:  # pragma: no cover
        log.warning("EDGAR 8-K search failed: %s", exc)
        return []
    out: list[Signal] = []
    for f in filings:
        sig = _filing_to_signal(f)
        if sig is not None:
            out.append(sig)
    return out


def _filing_to_signal(f: Filing) -> Signal | None:
    if not f.ticker:
        return None
    dt = datetime.combine(f.filed, datetime.min.time(), tzinfo=timezone.utc)
    return Signal(
        detector="buyback",
        ticker=f.ticker,
        company=f.company,
        event_dt=dt,
        headline=f"{f.company} — 8-K referencing repurchase program",
        url=f.url,
        evidence="",  # live seed: no parsed body
        severity=0.60,
    )


def _as_date(v):
    if hasattr(v, "date"):
        return v.date()
    return v
