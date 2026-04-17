from datetime import date

from switching.detectors.insider_cluster import (
    InsiderPurchase,
    classify_role,
    detect_clusters,
)


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
