"""Tests for the movers researcher attribution + persistence."""

from __future__ import annotations

from pathlib import Path

from switching import movers
from switching.movers import attribute, _norm, save_audit, load_audit, audit_dates


# A fake classifier that "fires" on any headline containing its keyword.
def _clf(keyword):
    def fn(title, summary=""):
        return {"severity": 0.7} if keyword.lower() in (title or "").lower() else None
    return fn


CLASSIFIERS = [("mna_target", _clf("acquire")), ("earnings_surprise", _clf("beats"))]


def _mover(symbol="ZZZZ", pct=12.0):
    return {"symbol": symbol, "name": "Zzz Inc", "pct_change": pct,
            "price": 5.0, "vol_ratio": 4.0, "had_earnings": False}


class TestAttribute:
    def test_caught_when_in_seen_tickers(self):
        r = attribute(_mover("NVDA"), ["NVDA beats estimates"], {"NVDA"}, set(), CLASSIFIERS)
        assert r["status"] == "caught"
        assert r["reason"] == "caught"

    def test_caught_matches_on_bare_root_for_lse(self):
        # mover comes back as VOD.L; our records store the bare root
        r = attribute(_mover("VOD.L"), ["whatever"], {"VOD"}, set(), CLASSIFIERS)
        assert r["reason"] == "caught"

    def test_feed_gap_when_classifies_but_not_ingested(self):
        # a detector WOULD classify it, but the story isn't in our records → source gap
        r = attribute(_mover("ABCD"), ["ABCD to acquire rival in $2bn deal"],
                      set(), set(), CLASSIFIERS)
        assert r["status"] == "missed"
        assert r["reason"] == "feed_gap"
        assert r["detector"] == "mna_target"
        assert "acquire" in r["evidence"]

    def test_ticker_drop_when_classifies_and_ingested(self):
        headline = "ABCD to acquire rival in $2bn deal"
        r = attribute(_mover("ABCD"), [headline], set(), {_norm(headline)}, CLASSIFIERS)
        assert r["reason"] == "ticker_drop"   # we saw the story, just couldn't ticker it
        assert r["detector"] == "mna_target"

    def test_no_detector_when_news_but_nothing_classifies(self):
        r = attribute(_mover("WXYZ"), ["WXYZ soars on heavy retail buying"],
                      set(), set(), CLASSIFIERS)
        assert r["reason"] == "no_detector"
        assert r["detector"] is None

    def test_no_news_when_no_headlines(self):
        r = attribute(_mover("QQQQ"), [], set(), set(), CLASSIFIERS)
        assert r["reason"] == "no_news"


class TestPersistence:
    def test_save_and_load_roundtrip(self, tmp_path: Path):
        report = {"generated_at": "2026-06-10T12:00:00+00:00", "market": "us",
                  "count": 1, "summary": {"caught": 1}, "movers": [_mover()]}
        save_audit(tmp_path, "us", report)
        loaded = load_audit(tmp_path, "us")
        assert loaded["market"] == "us"
        assert loaded["movers"][0]["symbol"] == "ZZZZ"

    def test_load_missing_returns_none(self, tmp_path: Path):
        assert load_audit(tmp_path, "us") is None

    def test_keeps_one_file_per_day(self, tmp_path: Path):
        for day in ("2026-06-08", "2026-06-09", "2026-06-10"):
            save_audit(tmp_path, "us", {"generated_at": f"{day}T21:00:00+00:00",
                                        "market": "us", "count": 0, "summary": {}, "movers": []})
        # all three days kept + listed newest-first
        assert audit_dates(tmp_path, "us") == ["2026-06-10", "2026-06-09", "2026-06-08"]
        # default load = newest day
        assert load_audit(tmp_path, "us")["generated_at"].startswith("2026-06-10")
        # specific day selectable
        assert load_audit(tmp_path, "us", date="2026-06-08")["generated_at"].startswith("2026-06-08")
        # a re-run on the same day overwrites only that day
        save_audit(tmp_path, "us", {"generated_at": "2026-06-10T22:00:00+00:00",
                                    "market": "us", "count": 9, "summary": {}, "movers": []})
        assert len(audit_dates(tmp_path, "us")) == 3
        assert load_audit(tmp_path, "us")["count"] == 9


def test_fetch_headlines_uses_benzinga_when_selected():
    from unittest.mock import patch
    with patch("switching.sources.benzinga.fetch_news",
               return_value=[{"title": "Acme to acquire Beta"}, {"title": "X soars"}]) as mo:
        out = movers._fetch_headlines("ACME", limit=8, source="benzinga")
    assert out == ["Acme to acquire Beta", "X soars"]
    mo.assert_called_once()


def test_real_classifiers_load():
    # Smoke test: the registry-backed classifier collection returns real detectors
    clfs = movers._load_classifiers()
    names = {n for n, _ in clfs}
    assert "mna_target" in names and "earnings_surprise" in names
    assert len(clfs) >= 8
