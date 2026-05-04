"""Tests for the dividend_surprise detector."""

from switching.detectors.dividend_surprise import classify


def test_special_dividend():
    m = classify("Costco Declares Special Cash Dividend of $15 Per Share", "")
    assert m is not None
    assert m["direction"] == "special"
    assert m["per_share"] == 15.0
    assert m["severity"] >= 0.85  # base 0.80 + large bonus


def test_small_special_dividend():
    m = classify("Ford Announces Special Dividend of $0.50 Per Share", "")
    assert m is not None
    assert m["direction"] == "special"
    assert m["per_share"] == 0.50
    assert m["severity"] >= 0.75


def test_dividend_initiation():
    m = classify("Meta Platforms Initiates Quarterly Dividend of $0.50 Per Share", "")
    assert m is not None
    assert m["direction"] == "initiation"
    assert m["per_share"] == 0.50
    assert m["severity"] >= 0.70


def test_first_dividend():
    m = classify("Alphabet Declares Its First-Ever Quarterly Dividend of $0.20 Per Share", "")
    assert m is not None
    assert m["direction"] == "initiation"


def test_dividend_increase():
    m = classify("Microsoft Increases Quarterly Dividend by 10%", "")
    assert m is not None
    assert m["direction"] == "increase"
    assert m["severity"] >= 0.55


def test_large_dividend_increase():
    m = classify("Apple Raises Dividend", "The 25% increase brings the quarterly payout to $0.25 per share.")
    assert m is not None
    assert m["direction"] == "increase"
    assert m["pct_increase"] == 25.0
    assert m["severity"] >= 0.65


def test_dividend_cut():
    m = classify("Intel Cuts Quarterly Dividend by 66%", "")
    assert m is not None
    assert m["direction"] == "cut"
    assert m["severity"] >= 0.60


def test_dividend_suspension():
    m = classify("Boeing Suspends Dividend Amid 737 MAX Crisis", "")
    assert m is not None
    assert m["direction"] == "cut"


def test_dividend_hike():
    m = classify("JPMorgan Hikes Dividend to $1.05 Per Share", "")
    assert m is not None
    assert m["direction"] == "increase"
    assert m["per_share"] == 1.05


def test_rejects_unrelated():
    assert classify("Apple launches new MacBook Pro lineup", "") is None


def test_rejects_earnings():
    assert classify("NVDA beats estimates on surging AI demand", "") is None


def test_rejects_stock_split():
    assert classify("Amazon announces 20-for-1 stock split", "") is None


def test_severity_capped():
    m = classify("Company Declares Special Cash Dividend of $100 Per Share", "")
    assert m is not None
    assert m["severity"] <= 0.95


def test_extraordinary_dividend():
    m = classify("MSFT Announces Extraordinary Dividend of $3.00 Per Share", "")
    assert m is not None
    assert m["direction"] == "special"
    assert m["per_share"] == 3.0
