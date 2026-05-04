"""Tests for the contract_win detector."""

from switching.detectors.contract_win import classify


def test_dod_contract():
    m = classify(
        "Lockheed Martin Awarded $2.4 Billion Contract by Department of Defense",
        "",
    )
    assert m is not None
    assert m["government"] is True
    assert m["defense"] is True
    assert m["contract_value"] == 2_400_000_000.0
    assert m["severity"] >= 0.85


def test_nasa_contract():
    m = classify("SpaceX Wins $1.4 Billion NASA Contract for ISS Missions", "")
    assert m is not None
    assert m["government"] is True
    assert m["contract_value"] == 1_400_000_000.0
    assert m["severity"] >= 0.80


def test_army_contract():
    m = classify(
        "Raytheon Receives $800 Million U.S. Army Contract for Missile Systems",
        "",
    )
    assert m is not None
    assert m["government"] is True
    assert m["defense"] is True
    assert m["contract_value"] == 800_000_000.0


def test_federal_contract():
    m = classify(
        "Palantir Secures $250 Million Federal Contract for Data Analytics",
        "",
    )
    assert m is not None
    assert m["government"] is True
    assert m["contract_value"] == 250_000_000.0


def test_billion_dollar_bonus():
    m_big = classify("RTX Awarded $3 Billion DoD Contract", "")
    m_small = classify("RTX Awarded $50 Million DoD Contract", "")
    assert m_big is not None and m_small is not None
    assert m_big["severity"] > m_small["severity"]


def test_multi_year():
    m = classify(
        "Northrop Grumman Wins $5.6 Billion Multi-Year Contract for B-21 Raider",
        "",
    )
    assert m is not None
    assert m["multi_year"] is True
    assert m["defense"] is True


def test_non_defense_gov():
    m = classify(
        "Leidos Awarded $450 Million Department of Veterans Affairs Contract",
        "",
    )
    assert m is not None
    assert m["government"] is True
    assert m["defense"] is False


def test_commercial_contract():
    m = classify("Boeing Receives $2 Billion Contract for 737 MAX Deliveries", "")
    assert m is not None
    assert m["government"] is False
    assert m["defense"] is False
    assert m["severity"] >= 0.60


def test_rejects_unrelated():
    assert classify("Apple launches new MacBook Pro lineup", "") is None


def test_rejects_earnings():
    assert classify("Lockheed Martin reports Q3 earnings beat", "") is None


def test_severity_capped():
    m = classify(
        "Company Awarded $10 Billion Multi-Year DoD Defense Contract",
        "Indefinite-delivery contract for missile defense systems.",
    )
    assert m is not None
    assert m["severity"] <= 0.95


def test_million_unit():
    m = classify("General Dynamics Wins $300M Navy Contract", "")
    assert m is not None
    assert m["contract_value"] == 300_000_000.0
