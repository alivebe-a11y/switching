from switching.detectors.spinoff import classify


def test_classify_spinoff_announcement():
    m = classify("Danaher Announces Spin-Off of Fortive", "")
    assert m is not None
    assert m["type"] == "spinoff"
    assert m["severity"] == 0.70


def test_classify_completed_spinoff():
    m = classify("GE Completes Spin-Off of GE HealthCare", "")
    assert m is not None
    assert m["type"] == "spinoff"
    assert m["severity"] == 0.80


def test_classify_tax_free_bonus():
    m = classify(
        "Acme Corp Announces Tax-Free Spin-Off of Widget Division",
        "",
    )
    assert m is not None
    assert m["severity"] == 0.80


def test_classify_completed_and_tax_free():
    m = classify(
        "Parent Corp Completes Tax-Free Spin-Off of NewCo",
        "",
    )
    assert m is not None
    assert m["severity"] == 0.90


def test_classify_board_approved():
    m = classify(
        "Board Approved Spinoff of Consumer Division",
        "The company announces plans to create a standalone company.",
    )
    assert m is not None
    assert m["severity"] == 0.75


def test_classify_split_off():
    m = classify("Company Announces Split-Off of Subsidiary", "")
    assert m is not None
    assert m["type"] == "split-off"


def test_classify_carve_out():
    m = classify("Company Plans Carve-Out of Tech Division", "")
    assert m is not None
    assert m["type"] == "carve-out"


def test_classify_plans_to_separate():
    m = classify(
        "Johnson & Johnson Plans to Separate Consumer Health Business",
        "J&J announces plans to create an independent company.",
    )
    assert m is not None
    assert m["type"] == "spinoff"


def test_classify_rejects_unrelated():
    assert classify("Apple launches new MacBook Pro", "") is None


def test_classify_rejects_spinoff_without_action():
    assert classify("Spinoff stocks have outperformed historically", "") is None


def test_severity_capped():
    m = classify(
        "Board Approved Tax-Free Spin-Off Completed Successfully",
        "The company completes its tax-free distribution.",
    )
    assert m is not None
    assert m["severity"] <= 0.95
