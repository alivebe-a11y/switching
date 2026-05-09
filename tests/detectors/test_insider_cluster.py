from datetime import date

import pytest

from switching.detectors.insider_cluster import (
    InsiderPurchase,
    classify_role,
    detect_clusters,
    parse_form4_xml,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _form4_xml(
    ticker: str = "ACME",
    issuer_name: str = "ACME Corp",
    owner_name: str = "John Smith",
    officer_title: str = "Chief Executive Officer",
    is_director: str = "0",
    txn_code: str = "P",
    txn_date: str = "2024-03-15",
    shares: str = "1000",
    price: str = "50.00",
) -> bytes:
    """Build a minimal Form 4 XML payload for testing."""
    return f"""<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>{txn_date}</periodOfReport>
  <issuer>
    <issuerCik>0000123456</issuerCik>
    <issuerName>{issuer_name}</issuerName>
    <issuerTradingSymbol>{ticker}</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0000654321</rptOwnerCik>
      <rptOwnerName>{owner_name}</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>{is_director}</isDirector>
      <isOfficer>{'1' if officer_title else '0'}</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>{officer_title}</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>{txn_date}</value></transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>{txn_code}</transactionCode>
        <equitySwapInvolved>0</equitySwapInvolved>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{shares}</value></transactionShares>
        <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
""".encode()


def _p(
    ticker: str = "ACME",
    insider: str = "Alice",
    title: str = "Director",
    d: date = date(2024, 3, 1),
    amount: float = 100_000.0,
    code: str = "P",
) -> InsiderPurchase:
    return InsiderPurchase(
        ticker=ticker,
        issuer="ACME Corp",
        insider_name=insider,
        insider_title=title,
        transaction_date=d,
        dollar_amount=amount,
        transaction_code=code,
    )


def test_classify_role():
    assert classify_role("Chief Executive Officer") == "csuite"
    assert classify_role("CFO") == "csuite"
    assert classify_role("Director") == "director"
    assert classify_role("EVP, Marketing") == "other"
    assert classify_role("") == "other"


def test_three_distinct_insiders_in_window_emit_cluster():
    purchases = [
        _p(insider="Alice", d=date(2024, 3, 1), amount=200_000),
        _p(insider="Bob",   d=date(2024, 3, 10), amount=300_000),
        _p(insider="Carol", d=date(2024, 3, 15), amount=500_000),
    ]
    sigs = detect_clusters(purchases)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.ticker == "ACME"
    assert sig.event_dt.date() == date(2024, 3, 15)
    assert sig.extra["aggregate_usd"] == 1_000_000
    # $1M aggregate bumps severity.
    assert sig.severity >= 0.70


def test_same_insider_thrice_does_not_count():
    purchases = [
        _p(insider="Alice", d=date(2024, 3, 1), amount=200_000),
        _p(insider="Alice", d=date(2024, 3, 10), amount=300_000),
        _p(insider="Alice", d=date(2024, 3, 15), amount=500_000),
    ]
    sigs = detect_clusters(purchases)
    assert sigs == []


def test_buys_outside_window_do_not_cluster():
    purchases = [
        _p(insider="Alice", d=date(2024, 1, 1), amount=200_000),
        _p(insider="Bob",   d=date(2024, 3, 1), amount=200_000),
        _p(insider="Carol", d=date(2024, 5, 1), amount=200_000),
    ]
    sigs = detect_clusters(purchases)
    assert sigs == []


def test_csuite_bump():
    baseline = [
        _p(insider="Alice", title="Director", d=date(2024, 3, 1)),
        _p(insider="Bob",   title="Director", d=date(2024, 3, 10)),
        _p(insider="Carol", title="Director", d=date(2024, 3, 15)),
    ]
    bumped = [
        _p(insider="Alice", title="Chief Executive Officer", d=date(2024, 3, 1)),
        _p(insider="Bob",   title="Chief Financial Officer", d=date(2024, 3, 10)),
        _p(insider="Carol", title="Director",                d=date(2024, 3, 15)),
    ]
    s_base = detect_clusters(baseline)[0]
    s_bump = detect_clusters(bumped)[0]
    assert s_bump.severity > s_base.severity


def test_non_purchase_codes_ignored():
    purchases = [
        _p(insider="Alice", d=date(2024, 3, 1), code="S"),   # sale
        _p(insider="Bob",   d=date(2024, 3, 10), code="G"),  # gift
        _p(insider="Carol", d=date(2024, 3, 15), code="P"),
    ]
    # Only Carol's purchase is valid → not a cluster.
    assert detect_clusters(purchases) == []


def test_aggregate_floor_excludes_small_clusters():
    purchases = [
        _p(insider="Alice", d=date(2024, 3, 1), amount=10_000),
        _p(insider="Bob",   d=date(2024, 3, 5), amount=10_000),
        _p(insider="Carol", d=date(2024, 3, 10), amount=10_000),
    ]
    # Aggregate only $30k, below the default $100k floor.
    assert detect_clusters(purchases) == []


# ---------------------------------------------------------------------------
# Form 4 XML parser tests
# ---------------------------------------------------------------------------


def test_parse_form4_xml_basic_purchase():
    xml = _form4_xml(ticker="TSLA", shares="500", price="200.00")
    purchases = parse_form4_xml(xml, url="https://www.sec.gov/test/doc.xml")
    assert len(purchases) == 1
    p = purchases[0]
    assert p.ticker == "TSLA"
    assert p.issuer == "ACME Corp"
    assert p.insider_name == "John Smith"
    assert p.insider_title == "Chief Executive Officer"
    assert p.transaction_date == date(2024, 3, 15)
    assert p.dollar_amount == pytest.approx(100_000.0)
    assert p.transaction_code == "P"
    assert "sec.gov" in p.url


def test_parse_form4_xml_non_purchase_code_filtered():
    """Sales (code S) and grants (code A) must not appear in results."""
    for code in ("S", "A", "G", "M", "F"):
        xml = _form4_xml(txn_code=code)
        assert parse_form4_xml(xml) == [], f"code {code!r} should be filtered out"


def test_parse_form4_xml_zero_price_filtered():
    """Transactions with zero price (grants, option exercises) must be skipped."""
    xml = _form4_xml(txn_code="P", price="0")
    assert parse_form4_xml(xml) == []


def test_parse_form4_xml_director_title_fallback():
    """When officerTitle is absent but isDirector=1, title should be 'Director'."""
    xml = _form4_xml(officer_title="", is_director="1", txn_code="P", price="10.00", shares="100")
    purchases = parse_form4_xml(xml)
    assert len(purchases) == 1
    assert purchases[0].insider_title == "Director"


def test_parse_form4_xml_missing_ticker_returns_empty():
    """A Form 4 with no issuerTradingSymbol (private company) should yield nothing."""
    xml = _form4_xml(ticker="")
    assert parse_form4_xml(xml) == []


def test_parse_form4_xml_malformed_xml_returns_empty():
    assert parse_form4_xml(b"<not valid xml at all") == []


def test_parse_form4_xml_dollar_amount_computed():
    xml = _form4_xml(shares="2500", price="40.00")
    p = parse_form4_xml(xml)[0]
    assert p.dollar_amount == pytest.approx(100_000.0)


def test_parse_form4_xml_comma_in_shares():
    """Some EDGAR filings write shares as '1,000' — must parse correctly."""
    xml = _form4_xml(shares="1,000", price="50.00")
    purchases = parse_form4_xml(xml)
    assert len(purchases) == 1
    assert purchases[0].dollar_amount == pytest.approx(50_000.0)


def test_parse_form4_xml_ticker_uppercased():
    """Tickers from XML must be returned uppercase regardless of filing casing."""
    xml = _form4_xml(ticker="aapl")
    purchases = parse_form4_xml(xml)
    assert purchases[0].ticker == "AAPL"


def test_parse_form4_integrates_with_detect_clusters():
    """End-to-end: parse three Form 4s for the same ticker and cluster them."""
    purchases = []
    for name, d in [("Alice", "2024-03-01"), ("Bob", "2024-03-10"), ("Carol", "2024-03-20")]:
        xml = _form4_xml(owner_name=name, txn_date=d, shares="1000", price="100.00")
        purchases.extend(parse_form4_xml(xml))
    signals = detect_clusters(purchases)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.ticker == "ACME"
    assert sig.extra["aggregate_usd"] == pytest.approx(300_000.0)
