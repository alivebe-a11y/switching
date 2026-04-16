from switching.detectors.buyback import classify, _parse_amount


def test_classify_apple_90b():
    m = classify(
        "Apple Announces Strong Quarter, Authorizes $90 Billion Share Repurchase",
        "The board of directors has authorized up to $90 billion in share repurchases.",
    )
    assert m is not None
    assert m["amount_usd"] == 90_000_000_000
    assert m["severity"] >= 0.80


def test_classify_small_cap_100m():
    m = classify(
        "ACME Inc. Board Authorizes $250 Million Share Repurchase Program",
        "The company's board of directors has approved a new repurchase program of up to $250 million.",
    )
    assert m is not None
    assert m["amount_usd"] == 250_000_000
    # Only the small-amount bonus kicks in — should stay below the $1B tier.
    assert 0.60 <= m["severity"] < 0.85


def test_classify_accelerated_bumps_severity():
    m_normal = classify(
        "Foo Corp board authorizes $1 billion share repurchase program",
        "",
    )
    m_asr = classify(
        "Foo Corp board authorizes $1 billion accelerated share repurchase program",
        "",
    )
    assert m_normal is not None and m_asr is not None
    assert m_asr["severity"] > m_normal["severity"]


def test_classify_rejects_routine_dividend_mention():
    m = classify(
        "ACME Corp declares quarterly dividend",
        "The board approved the regular quarterly dividend; no repurchase activity this quarter.",
    )
    assert m is None


def test_classify_rejects_mention_without_authorize():
    m = classify(
        "ACME Corp repurchases shares under existing program",
        "The company continued purchases under its existing repurchase program; no new authorization.",
    )
    # Has "repurchase program" but not authorize/approve verb → reject.
    assert m is None


def test_parse_amount_handles_units():
    import re

    rx = __import__("switching.detectors.buyback", fromlist=["_AMOUNT_RX"])._AMOUNT_RX
    cases = {
        "up to $5 billion": 5_000_000_000,
        "up to $500 million": 500_000_000,
        "up to $1.5B": 1_500_000_000,
        "up to $250M": 250_000_000,
    }
    for text, want in cases.items():
        m = rx.search(text)
        assert m is not None, text
        assert _parse_amount(m) == want
