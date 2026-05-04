"""Tests for the guidance_raise detector."""

from switching.detectors.guidance_raise import classify


def test_raises_guidance():
    m = classify("NVDA Raises Full-Year Revenue Guidance", "")
    assert m is not None
    assert m["direction"] == "raise"
    assert m["full_year"] is True
    assert m["severity"] >= 0.70


def test_raises_outlook():
    m = classify("AMD Raises Outlook for Q3 Revenue", "")
    assert m is not None
    assert m["direction"] == "raise"


def test_increases_forecast():
    m = classify("Tesla Increases Full-Year Delivery Forecast", "")
    assert m is not None
    assert m["direction"] == "raise"
    assert m["full_year"] is True


def test_lowers_guidance():
    m = classify("Intel Lowers Full-Year Revenue Guidance", "")
    assert m is not None
    assert m["direction"] == "lower"
    assert m["severity"] >= 0.65


def test_cuts_outlook():
    m = classify("FedEx Cuts Outlook Amid Weakening Demand", "")
    assert m is not None
    assert m["direction"] == "lower"


def test_pre_announce_beat():
    m = classify("Shopify Pre-Announces Results Above Guidance", "")
    assert m is not None
    assert m["direction"] == "pre_announce_beat"
    assert m["severity"] >= 0.70


def test_pre_announce_miss():
    m = classify("Snap Pre-Announces Results Below Expectations", "")
    assert m is not None
    assert m["direction"] == "pre_announce_miss"
    assert m["severity"] >= 0.65


def test_revises_upward():
    m = classify("CrowdStrike Revises Guidance Upward for FY2025", "")
    assert m is not None
    assert m["direction"] == "raise"


def test_large_raise_bonus():
    m = classify(
        "Meta Raises FY Revenue Guidance",
        "Company now expects revenue of $150 billion, up from $120 billion to $150 billion.",
    )
    assert m is not None
    assert m["large_raise"] is True
    assert m["severity"] >= 0.80


def test_rejects_quarterly_earnings():
    assert classify("AAPL Reports Q3 Quarterly Results Above Expectations", "") is None


def test_rejects_regular_report():
    assert classify("Amazon Reports Q2 Revenue of $134 Billion", "") is None


def test_rejects_unrelated():
    assert classify("Google announces new Pixel phone lineup", "") is None


def test_severity_capped():
    m = classify(
        "Company Raises Full-Year Guidance",
        "Now expects revenue of $200 billion, up from $150 billion to $200 billion.",
    )
    assert m is not None
    assert m["severity"] <= 0.95


def test_full_year_bonus():
    m_fy = classify("MSFT Raises Full-Year Guidance", "")
    m_q = classify("MSFT Raises Guidance", "")
    assert m_fy is not None and m_q is not None
    assert m_fy["severity"] > m_q["severity"]


def test_narrows_range():
    m = classify("Apple Narrows Guidance Range to Upper End", "")
    assert m is not None
    assert m["direction"] == "raise"
