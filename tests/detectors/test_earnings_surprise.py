from switching.detectors.earnings_surprise import classify


def test_classify_beats_estimates():
    m = classify("NVDA beats estimates on surging AI demand", "")
    assert m is not None
    assert m["direction"] == "beat"
    assert m["severity"] == 0.65


def test_classify_tops_expectations():
    m = classify("Meta Platforms tops expectations with strong ad revenue", "")
    assert m is not None
    assert m["direction"] == "beat"


def test_classify_exceeds_expectations():
    m = classify("Amazon exceeds expectations on AWS strength", "")
    assert m is not None
    assert m["direction"] == "beat"


def test_classify_crushes_big_beat():
    m = classify("NVDA crushes estimates with record data center revenue", "")
    assert m is not None
    assert m["direction"] == "beat"
    assert m["severity"] == 0.80


def test_classify_blows_past_big_beat():
    m = classify("Apple blows past estimates with iPhone super-cycle", "")
    assert m is not None
    assert m["direction"] == "beat"
    assert m["severity"] == 0.80


def test_classify_revenue_beat_bonus():
    m = classify(
        "Microsoft beats estimates",
        "Revenue also beats consensus as Azure accelerates.",
    )
    assert m is not None
    assert m["direction"] == "beat"
    assert m["severity"] == 0.75


def test_classify_big_beat_plus_revenue():
    m = classify(
        "NVDA smashes estimates",
        "Revenue also tops consensus with data center demand surging.",
    )
    assert m is not None
    assert m["severity"] == 0.90


def test_classify_miss():
    m = classify("Snap misses estimates as ad revenue declines", "")
    assert m is not None
    assert m["direction"] == "miss"
    assert m["severity"] == 0.55


def test_classify_miss_with_warning():
    m = classify(
        "FedEx misses estimates and warns on global slowdown",
        "",
    )
    assert m is not None
    assert m["direction"] == "miss"
    assert m["severity"] == 0.65


def test_classify_falls_short():
    m = classify("Walgreens falls short of estimates and cuts outlook", "")
    assert m is not None
    assert m["direction"] == "miss"
    assert m["severity"] == 0.65


def test_classify_eps_vs_beat():
    m = classify(
        "Acme Corp reports EPS of $1.25 vs consensus $1.10",
        "",
    )
    assert m is not None
    assert m["direction"] == "beat"
    assert m["magnitude"] is not None
    assert m["magnitude"] > 0


def test_classify_eps_vs_miss():
    m = classify(
        "Acme Corp reports EPS of $0.80 vs expected $0.95",
        "",
    )
    assert m is not None
    assert m["direction"] == "miss"


def test_classify_rejects_unrelated():
    assert classify("Apple launches new MacBook Pro lineup", "") is None


def test_classify_rejects_dividend():
    assert classify("Cisco declares quarterly dividend of $0.40", "") is None


def test_severity_capped_at_095():
    m = classify(
        "NVDA smashes estimates",
        "Revenue also beats consensus. Company crushes on every metric.",
    )
    assert m is not None
    assert m["severity"] <= 0.95
