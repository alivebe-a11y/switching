"""Tests for the mna_target detector."""

from switching.detectors.mna_target import classify


def test_acquisition_target():
    m = classify("Activision Blizzard to Be Acquired by Microsoft for $95 Per Share in All-Cash Deal", "")
    assert m is not None
    assert m["direction"] == "target"
    assert m["all_cash"] is True
    assert m["price_per_share"] == 95.0
    assert m["severity"] >= 0.85


def test_definitive_agreement():
    m = classify("VMware Signs Definitive Agreement to Be Acquired by Broadcom", "")
    assert m is not None
    assert m["direction"] == "target"
    assert m["definitive"] is True
    assert m["severity"] >= 0.85


def test_acquirer_side():
    m = classify("Pfizer Acquires Seagen for $43 Billion", "")
    assert m is not None
    assert m["direction"] == "acquirer"
    assert m["severity"] >= 0.50


def test_merger_agreement():
    m = classify("Kroger and Albertsons Enter into Merger Agreement", "")
    assert m is not None
    assert m["direction"] == "target"
    assert m["severity"] >= 0.80


def test_tender_offer():
    m = classify("LVMH Launches Tender Offer for Tiffany at $135 Per Share", "")
    assert m is not None
    assert m["direction"] == "target"
    assert m["price_per_share"] == 135.0


def test_all_cash_bonus():
    m_cash = classify("Company to Be Acquired by BigCorp in All-Cash Deal", "Definitive agreement signed.")
    m_mix = classify("Company to Be Acquired by BigCorp in Cash-and-Stock Deal", "")
    assert m_cash is not None and m_mix is not None
    assert m_cash["severity"] > m_mix["severity"]


def test_uncertain_penalty():
    m = classify("BigCorp Exploring Potential Acquisition of SmallCo", "")
    assert m is not None
    assert m["uncertain"] is True
    assert m["severity"] < 0.80


def test_premium_extraction():
    m = classify("Target Corp agrees to be acquired", "The deal represents a 40% premium to the closing price.")
    assert m is not None
    assert m["premium_pct"] == 40.0


def test_rejects_unrelated():
    assert classify("Apple launches new MacBook Pro lineup", "") is None


def test_rejects_earnings():
    assert classify("Microsoft reports Q3 revenue of $52.9 billion", "") is None


def test_severity_capped():
    m = classify(
        "Company to Be Acquired in All-Cash Definitive Agreement",
        "The deal represents a 50% premium.",
    )
    assert m is not None
    assert m["severity"] <= 0.95


def test_to_acquire_is_acquirer():
    """'Company to Acquire X' = acquirer-side — should be direction=acquirer."""
    m = classify("Amazon to Acquire One Medical for $3.9 Billion", "")
    assert m is not None
    assert m["direction"] == "acquirer"
    assert m["severity"] >= 0.50


def test_acquirer_definitive_agreement():
    """'Signs Definitive Agreement to Acquire X' = acquirer, not target."""
    m = classify("Microsoft Signs Definitive Agreement to Acquire Activision Blizzard", "")
    assert m is not None
    assert m["direction"] == "acquirer"


def test_completes_acquisition_is_acquirer():
    """'Completes acquisition of X' = acquirer."""
    m = classify("Suncrete Inc Completes Acquisition of Nelson Bros Ready Mix", "")
    assert m is not None
    assert m["direction"] == "acquirer"


def test_following_acquisition_of_is_acquirer():
    """'Revenue growth following acquisition of X' = acquirer."""
    m = classify("SEGG Media Reports 1400% Revenue Growth Following Acquisition of Veloce Media Group", "")
    assert m is not None
    assert m["direction"] == "acquirer"


def test_exercises_option_to_acquire_is_acquirer():
    """'Exercises option to acquire X' = acquirer."""
    m = classify("Artivion Reports Q1 Results and Announces Exercise of Option to Acquire Endospan", "")
    assert m is not None
    assert m["direction"] == "acquirer"


def test_divestiture_is_acquirer():
    """Divestitures are detected as acquirer-side (neither party is a pure target)."""
    m = classify("FMC Corporation Announces Agreement to Divest India Business to Crystal Crop", "")
    assert m is not None
    assert m["direction"] == "acquirer"
