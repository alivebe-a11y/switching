"""Tests for the UK director dealing detector."""

from switching.detectors.uk_director_dealing import classify


def test_director_purchase_detected():
    m = classify("Barclays PLC (BARC) - Director/PDMR Shareholding", "Director purchased 50000 shares at 145p")
    assert m is not None
    assert m["direction"] == "buy"
    assert m["severity"] >= 0.65


def test_pdmr_shareholding_notification_detected():
    m = classify("Lloyds Banking Group (LLOY) - PDMR Shareholding Notification", "Chief Executive Officer purchased 200000 shares")
    assert m is not None
    assert m["direction"] == "buy"


def test_notification_of_director_dealing():
    m = classify("AstraZeneca PLC (AZN) - Notification of Director Dealing", "Non-Executive Director purchased 500 shares at 10200p")
    assert m is not None
    assert m["severity"] >= 0.65


def test_sell_only_notification_rejected():
    m = classify("BP PLC (BP.) - Director/PDMR Shareholding", "Director sold 100000 shares at 470p")
    assert m is None


def test_sell_only_disposal_rejected():
    m = classify("Vodafone Group (VOD) - PDMR Dealing Notification", "Disposal of 500000 shares at 75p")
    assert m is None


def test_cluster_buy_gives_severity_boost():
    m = classify(
        "GSK PLC (GSK) - Director Dealing Notification",
        "Multiple directors purchased shares during open period",
    )
    assert m is not None
    assert m["severity"] >= 0.75


def test_several_directors_boost():
    m = classify(
        "Next PLC (NXT) - Director/PDMR Shareholding",
        "Several directors bought shares ahead of capital markets day",
    )
    assert m is not None
    assert m["severity"] >= 0.75


def test_severity_capped_at_095():
    m = classify(
        "Company PLC (XYZ) - Director/PDMR Dealing",
        "Multiple directors purchased a large number of shares and bought more shares at acquisition price",
    )
    assert m is not None
    assert m["severity"] <= 0.95


def test_neutral_direction_flagged():
    """A dealing notification with no buy or sell language is flagged as neutral."""
    m = classify("XYZ PLC (XYZ) - Notification of Director Shareholding", "Director shareholding update")
    assert m is not None
    assert m["direction"] == "neutral"


def test_unrelated_headline_rejected():
    assert classify("Apple launches new MacBook Pro lineup", "") is None


def test_annual_report_rejected():
    assert classify("Company PLC (XYZ) - Annual Report and Accounts 2024", "Full year results") is None


def test_director_and_pdmr_notification():
    m = classify("Barclays PLC (BARC) - Directors & PDMR Dealing", "CEO purchased 10000 shares")
    assert m is not None
    assert m["direction"] == "buy"
