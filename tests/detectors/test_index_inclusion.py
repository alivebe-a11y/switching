from switching.detectors.index_inclusion import classify, _normalize_index, _guess_ticker


def test_classify_sp500_addition():
    title = "Palantir Technologies Set to Join S&P 500"
    summary = "Palantir will be added to the S&P 500 index effective September 23."
    result = classify(title, summary)
    assert result is not None
    assert result["index"] == "S&P 500"
    assert result["direction"] == "add"
    assert result["severity"] == 0.90


def test_classify_sp400_addition():
    title = "Warner Bros. Discovery Added to S&P MidCap 400"
    summary = "S&P Dow Jones Indices announced Warner Bros. Discovery will be added to the S&P 400."
    result = classify(title, summary)
    assert result is not None
    assert result["index"] == "S&P 400"
    assert result["direction"] == "add"
    assert result["severity"] == 0.70


def test_classify_sp600_addition():
    title = "Acme Corp Added to S&P SmallCap 600"
    summary = "Acme Corp will be added to the S&P 600 index."
    result = classify(title, summary)
    assert result is not None
    assert result["index"] == "S&P 600"
    assert result["direction"] == "add"
    assert result["severity"] == 0.55


def test_classify_russell_1000():
    title = "Palantir Joining the Russell 1000 Index"
    summary = "As part of Russell reconstitution, Palantir will be included in the Russell 1000."
    result = classify(title, summary)
    assert result is not None
    assert result["index"] == "Russell 1000"
    assert result["direction"] == "add"
    assert result["severity"] == 0.60


def test_classify_russell_2000():
    title = "Small Co will be added to the Russell 2000"
    summary = ""
    result = classify(title, summary)
    assert result is not None
    assert result["index"] == "Russell 2000"
    assert result["direction"] == "add"
    assert result["severity"] == 0.40


def test_classify_deletion():
    title = "Company XYZ removed from S&P 500"
    summary = "S&P Dow Jones Indices announced that XYZ will be removed from the S&P 500 and will be replaced by ABC."
    result = classify(title, summary)
    assert result is not None
    assert result["direction"] == "delete"
    assert result["severity"] == 0.80


def test_classify_no_match_unrelated_headline():
    result = classify("Apple reports record Q3 earnings", "Revenue beat expectations.")
    assert result is None


def test_classify_no_match_index_without_action():
    result = classify("S&P 500 closes at record high", "The index gained 1.2% today.")
    assert result is None


def test_classify_replacement_verb():
    title = "Dell Technologies Will Replace Etsy in S&P 500"
    summary = ""
    result = classify(title, summary)
    assert result is not None
    assert result["index"] == "S&P 500"
    assert result["direction"] == "add"
    assert result["severity"] == 0.90


def test_normalize_index():
    assert _normalize_index("S&P 500") == "S&P 500"
    assert _normalize_index("S&P 400") == "S&P 400"
    assert _normalize_index("S&P 600") == "S&P 600"
    assert _normalize_index("Russell 1000") == "Russell 1000"
    assert _normalize_index("Russell 2000") == "Russell 2000"


def test_guess_ticker():
    assert _guess_ticker("Palantir (PLTR) Joins S&P 500") == "PLTR"
    assert _guess_ticker("Some headline without a ticker") is None
    assert _guess_ticker("Dell (DELL) to replace Etsy (ETSY)") == "DELL"


def test_deletion_severity_lower_than_addition():
    add_result = classify(
        "ACME Added to S&P 500",
        "ACME will be added to the S&P 500 index.",
    )
    del_result = classify(
        "ACME removed from S&P 500",
        "ACME will be removed and replaced in the S&P 500.",
    )
    assert add_result is not None and del_result is not None
    assert del_result["severity"] < add_result["severity"]
