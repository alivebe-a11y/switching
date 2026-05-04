"""Tests for the analyst_upgrade detector."""

from switching.detectors.analyst_upgrade import classify


def test_upgrade_to_buy():
    m = classify("Goldman Sachs upgrades Apple to Buy", "")
    assert m is not None
    assert m["direction"] == "upgrade"
    assert m["top_tier"] is True
    assert m["severity"] >= 0.70


def test_upgrade_to_outperform():
    m = classify("Barclays upgrades MSFT to Outperform", "")
    assert m is not None
    assert m["direction"] == "upgrade"
    assert m["top_tier"] is False


def test_downgrade_to_sell():
    m = classify("Morgan Stanley downgrades SNAP to Underperform", "")
    assert m is not None
    assert m["direction"] == "downgrade"
    assert m["top_tier"] is True
    assert m["severity"] >= 0.65


def test_downgrade_to_underweight():
    m = classify("JP Morgan downgrades PYPL to Underweight", "")
    assert m is not None
    assert m["direction"] == "downgrade"


def test_initiation_with_buy():
    m = classify("Needham initiates coverage on PLTR with Buy", "")
    assert m is not None
    assert m["direction"] == "initiation"
    assert m["severity"] >= 0.65


def test_initiation_without_rating():
    m = classify("Wedbush initiates coverage on RBLX", "")
    assert m is not None
    assert m["direction"] == "initiation"
    assert m["severity"] >= 0.50


def test_price_target_raise():
    m = classify("Jefferies raises price target on NVDA to $150", "")
    assert m is not None
    assert m["direction"] == "pt_raise"
    assert m["price_target"] == 150.0


def test_price_target_lower():
    m = classify("UBS lowers price target on INTC to $25", "")
    assert m is not None
    assert m["direction"] == "pt_lower"
    assert m["price_target"] == 25.0


def test_reiterate_buy_raises_target():
    m = classify("Goldman Sachs reiterates Buy on AAPL, raises price target to $250", "")
    assert m is not None
    assert m["direction"] == "pt_raise"
    assert m["top_tier"] is True
    assert m["price_target"] == 250.0


def test_double_upgrade():
    m = classify("Citi upgrades RIVN to Buy from Sell", "")
    assert m is not None
    assert m["direction"] == "upgrade"
    assert m["double_upgrade"] is True
    assert m["severity"] >= 0.70


def test_top_tier_bonus():
    m_top = classify("Goldman Sachs upgrades TSLA to Buy", "")
    m_low = classify("Needham upgrades TSLA to Buy", "")
    assert m_top is not None and m_low is not None
    assert m_top["severity"] > m_low["severity"]


def test_rejects_unrelated():
    assert classify("Apple launches new MacBook Pro lineup", "") is None


def test_rejects_earnings():
    assert classify("NVDA beats estimates on surging AI demand", "") is None


def test_severity_capped():
    m = classify("Goldman Sachs upgrades RIVN to Buy from Sell", "Also raises price target.")
    assert m is not None
    assert m["severity"] <= 0.95


def test_firm_detected():
    m = classify("Morgan Stanley upgrades META to Overweight", "")
    assert m is not None
    assert m["firm"] is not None
    assert "Morgan Stanley" in m["firm"]
