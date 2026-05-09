"""Insider-cluster buying detector.

Thesis: when three or more distinct insiders at the same issuer report
open-market purchases within a 30-day rolling window, the buying is
meaningfully different from routine grants / exercises and is a
historically positive signal for forward returns.

Signal date = date of the third (or later) qualifying purchase in the
window. Severity scales with how many of the buyers are C-suite and with
total dollar size.

Source: EDGAR Form 4 filings (transaction code ``P`` — open-market
purchase). Full Form-4 XML parsing is beyond v1; ``pull_live`` returns
placeholder signals derived from filing metadata so callers can at least
backtest against seed events. For production use, replace ``pull_live``
with a full Form 4 parser (see the roadmap in README).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence

from switching.detectors.base import Detector
from switching.registry import register
from switching.signal import Signal
from switching.sources.sec_edgar import EdgarClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InsiderPurchase:
    ticker: str
    issuer: str
    insider_name: str
    insider_title: str
    transaction_date: date
    dollar_amount: float
    transaction_code: str = "P"   # P = open-market purchase
    url: str = ""


_CSUITE_RX = re.compile(r"(?i)\b(chief\s+\w+\s+officer|ceo|cfo|coo|cto|president)\b")
_DIRECTOR_RX = re.compile(r"(?i)\b(director|board)\b")


def classify_role(title: str) -> str:
    """Return 'csuite', 'director', or 'other'."""
    if _CSUITE_RX.search(title or ""):
        return "csuite"
    if _DIRECTOR_RX.search(title or ""):
        return "director"
    return "other"


@register
class InsiderClusterDetector(Detector):
    name = "insider_cluster"
    description = (
        "Three or more distinct insiders at the same issuer reporting "
        "open-market purchases within a 30-day rolling window."
    )

    def __init__(
        self,
        client: EdgarClient | None = None,
        *,
        min_insiders: int = 3,
        window_days: int = 30,
        min_aggregate_usd: float = 100_000.0,
    ) -> None:
        self._client = client
        self._min = min_insiders
        self._window = window_days
        self._floor = min_aggregate_usd

    def scan(self, since: datetime) -> Iterable[Signal]:
        if self._client is None:
            log.info("insider_cluster scan requires an EdgarClient; nothing to yield")
            return
        purchases = _pull_form4_purchases(self._client, since=since)
        yield from detect_clusters(
            purchases,
            min_insiders=self._min,
            window_days=self._window,
            min_aggregate_usd=self._floor,
        )


def detect_clusters(
    purchases: Sequence[InsiderPurchase],
    *,
    min_insiders: int = 3,
    window_days: int = 30,
    min_aggregate_usd: float = 100_000.0,
) -> list[Signal]:
    """Group purchases by ticker and emit one signal per qualifying cluster.

    A cluster is a maximal set of purchases in a ``window_days`` rolling
    window where at least ``min_insiders`` *distinct* insiders bought and
    the aggregate dollar amount clears ``min_aggregate_usd``. To avoid
    emitting overlapping clusters on the same issuer, after a cluster
    closes at date ``D`` we skip further cluster emissions until the next
    purchase dated > ``D + window_days``.
    """
    by_ticker: dict[str, list[InsiderPurchase]] = {}
    for p in purchases:
        if p.transaction_code.upper() != "P":
            continue
        by_ticker.setdefault(p.ticker.upper(), []).append(p)

    signals: list[Signal] = []
    for ticker, rows in by_ticker.items():
        rows_sorted = sorted(rows, key=lambda r: r.transaction_date)
        i = 0
        while i < len(rows_sorted):
            end_date = rows_sorted[i].transaction_date
            window_start = end_date - timedelta(days=window_days)
            in_window = [
                r for r in rows_sorted[: i + 1] if r.transaction_date >= window_start
            ]
            insiders = {r.insider_name for r in in_window}
            aggregate = sum(r.dollar_amount for r in in_window)
            if len(insiders) >= min_insiders and aggregate >= min_aggregate_usd:
                sig = _cluster_to_signal(ticker, in_window)
                signals.append(sig)
                # Skip ahead past this window to avoid overlap emissions.
                skip_after = end_date + timedelta(days=window_days)
                while i < len(rows_sorted) and rows_sorted[i].transaction_date <= skip_after:
                    i += 1
                continue
            i += 1
    return signals


def _cluster_to_signal(ticker: str, members: Sequence[InsiderPurchase]) -> Signal:
    roles = [classify_role(m.insider_title) for m in members]
    csuite_count = sum(1 for r in roles if r == "csuite")
    director_count = sum(1 for r in roles if r == "director")
    aggregate = sum(m.dollar_amount for m in members)

    severity = 0.60
    if csuite_count >= 2:
        severity += 0.15
    elif csuite_count == 1:
        severity += 0.05
    if aggregate >= 1_000_000:
        severity += 0.10
    if director_count == len(members) and director_count >= 3:
        severity += 0.05
    severity = min(severity, 0.95)

    end_date = max(m.transaction_date for m in members)
    issuer = members[0].issuer
    dt = datetime.combine(end_date, datetime.min.time(), tzinfo=timezone.utc)
    insider_names = ", ".join(sorted({m.insider_name for m in members}))
    headline = (
        f"Insider cluster buying at {issuer} — "
        f"{len({m.insider_name for m in members})} insiders, ${aggregate:,.0f}"
    )
    return Signal(
        detector="insider_cluster",
        ticker=ticker,
        company=issuer,
        event_dt=dt,
        headline=headline,
        url=members[-1].url,
        evidence=f"insiders: {insider_names}",
        severity=round(severity, 3),
        extra={
            "insiders": sorted({m.insider_name for m in members}),
            "csuite_count": csuite_count,
            "director_count": director_count,
            "aggregate_usd": aggregate,
            "window_end": end_date.isoformat(),
        },
    )


def parse_form4_xml(xml_bytes: bytes, url: str = "") -> list[InsiderPurchase]:
    """Parse a Form 4 XML document and return InsiderPurchase objects.

    Only returns transactions with transaction code ``P`` (open-market
    purchase).  Derivative transactions (stock options, SARs, etc.) are
    ignored — we only want voluntary cash-on-the-table stock purchases.

    Form 4 XML structure (abbreviated)::

        <ownershipDocument>
          <issuer>
            <issuerName>Tesla, Inc.</issuerName>
            <issuerTradingSymbol>TSLA</issuerTradingSymbol>
          </issuer>
          <reportingOwner>
            <reportingOwnerId>
              <rptOwnerName>Musk Elon</rptOwnerName>
            </reportingOwnerId>
            <reportingOwnerRelationship>
              <isOfficer>1</isOfficer>
              <officerTitle>CEO</officerTitle>
            </reportingOwnerRelationship>
          </reportingOwner>
          <nonDerivativeTable>
            <nonDerivativeTransaction>
              <transactionDate><value>2024-01-15</value></transactionDate>
              <transactionCoding>
                <transactionCode>P</transactionCode>
              </transactionCoding>
              <transactionAmounts>
                <transactionShares><value>1000</value></transactionShares>
                <transactionPricePerShare><value>250.50</value></transactionPricePerShare>
              </transactionAmounts>
            </nonDerivativeTransaction>
          </nonDerivativeTable>
        </ownershipDocument>
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        log.debug("Form 4 XML parse error at %s: %s", url, exc)
        return []

    # ---- issuer -----------------------------------------------------------
    issuer_el = root.find(".//issuer")
    if issuer_el is None:
        return []
    ticker_el = issuer_el.find("issuerTradingSymbol")
    issuer_name_el = issuer_el.find("issuerName")
    ticker = (ticker_el.text or "").strip().upper() if ticker_el is not None else ""
    issuer_name = (issuer_name_el.text or "").strip() if issuer_name_el is not None else ""

    if not ticker:
        return []

    # ---- reporting owner --------------------------------------------------
    owner_el = root.find(".//reportingOwner")
    if owner_el is None:
        return []
    owner_name_el = owner_el.find(".//rptOwnerName")
    insider_name = (
        (owner_name_el.text or "").strip() if owner_name_el is not None else ""
    )
    officer_title_el = owner_el.find(".//officerTitle")
    is_director_el = owner_el.find(".//isDirector")
    insider_title = ""
    if officer_title_el is not None and (officer_title_el.text or "").strip():
        insider_title = officer_title_el.text.strip()
    elif (
        is_director_el is not None
        and (is_director_el.text or "").strip() == "1"
    ):
        insider_title = "Director"

    # ---- non-derivative transactions (stock purchases) -------------------
    results: list[InsiderPurchase] = []
    for txn in root.findall(".//nonDerivativeTransaction"):
        # Transaction code must be "P" (open-market purchase)
        code_el = txn.find(".//transactionCoding/transactionCode")
        if code_el is None:
            continue
        if (code_el.text or "").strip().upper() != "P":
            continue

        date_el = txn.find(".//transactionDate/value")
        shares_el = txn.find(".//transactionAmounts/transactionShares/value")
        price_el = txn.find(".//transactionAmounts/transactionPricePerShare/value")

        if date_el is None or shares_el is None:
            continue

        try:
            txn_date = date.fromisoformat((date_el.text or "").strip())
        except ValueError:
            continue

        try:
            shares = float((shares_el.text or "0").replace(",", ""))
        except ValueError:
            continue

        try:
            price = float(
                (price_el.text or "0").replace(",", "").strip()
            ) if price_el is not None else 0.0
        except ValueError:
            price = 0.0

        dollar_amount = shares * price
        if dollar_amount <= 0:
            # Skip zero-price grants / transfers
            continue

        results.append(InsiderPurchase(
            ticker=ticker,
            issuer=issuer_name,
            insider_name=insider_name,
            insider_title=insider_title,
            transaction_date=txn_date,
            dollar_amount=dollar_amount,
            transaction_code="P",
            url=url,
        ))

    return results


def _pull_form4_purchases(
    client: EdgarClient, *, since: datetime
) -> list[InsiderPurchase]:
    """Fetch recent Form 4 filings from EDGAR and parse open-market purchases.

    Calls ``EdgarClient.fetch_recent_form4_filings()`` to get the ATOM feed
    of recent filings, then downloads and parses each Form 4 XML to extract
    code-P (open-market purchase) transactions.
    """
    filings = client.fetch_recent_form4_filings(since)
    purchases: list[InsiderPurchase] = []
    errors = 0

    for cik, _accession_dashed, xml_url in filings:
        try:
            xml_bytes = client._fetch(xml_url)
        except Exception as exc:
            log.debug("Form 4 XML fetch failed (%s): %s", xml_url, exc)
            errors += 1
            continue
        items = parse_form4_xml(xml_bytes, url=xml_url)
        purchases.extend(items)

    log.info(
        "insider_cluster: %d Form 4 filings fetched, %d purchases found, %d errors",
        len(filings),
        len(purchases),
        errors,
    )
    return purchases


def pull_live(
    client: EdgarClient,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[Signal]:
    """Hook used by ``--live-seed``.  Fetches and clusters real Form 4 purchases."""
    if since is None:
        from datetime import timedelta
        since = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    purchases = _pull_form4_purchases(client, since=since)
    return detect_clusters(purchases)


def _as_date(v):
    if hasattr(v, "date"):
        return v.date()
    return v


# `date` is imported for `InsiderPurchase.transaction_date` typing callers.
__all__ = [
    "InsiderClusterDetector",
    "InsiderPurchase",
    "classify_role",
    "detect_clusters",
    "pull_live",
    "date",
]
