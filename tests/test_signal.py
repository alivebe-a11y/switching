from datetime import datetime, timezone

from switching.signal import PriceReaction, Signal


def _make(**overrides):
    base = dict(
        detector="ai_pivot",
        ticker="BIRD",
        company="Allbirds",
        event_dt=datetime(2026, 4, 16, 14, 30, tzinfo=timezone.utc),
        headline="Allbirds Announces AI-Driven Retail Transformation",
        url="https://example.com/bird",
        evidence="AI-Driven Retail Transformation",
        severity=0.8,
    )
    base.update(overrides)
    return Signal(**base)


def test_dedup_key_ignores_case_and_whitespace():
    a = _make(headline="Allbirds   Announces AI-Driven  Retail  Transformation")
    b = _make(headline="ALLBIRDS Announces AI-Driven Retail Transformation")
    assert a.dedup_key() == b.dedup_key()


def test_with_reaction_returns_new_signal():
    s = _make()
    r = PriceReaction(baseline_close=10.0, pct_change_1d=0.12, pct_change_5d=0.25, volume_ratio=3.5)
    s2 = s.with_reaction(r)
    assert s.price_reaction is None
    assert s2.price_reaction == r


def test_to_dict_is_json_serializable():
    import json

    s = _make().with_reaction(PriceReaction(1.0, 0.1, 0.2, 2.0))
    payload = json.dumps(s.to_dict())
    loaded = json.loads(payload)
    assert loaded["ticker"] == "BIRD"
    assert loaded["price_reaction"]["pct_change_1d"] == 0.1
