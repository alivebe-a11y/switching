"""Tests for the detection funnel (dropped-headline capture)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from switching import detection_funnel, storage


@dataclass
class _Item:
    title: str = "Acme raises full-year guidance"
    url: str = "https://example.com/acme"
    summary: str = "Acme Corp raised its FY outlook today."


def setup_function(_):
    detection_funnel._reset()
    storage._reset_connection_cache()


def teardown_function(_):
    detection_funnel._reset()
    storage._reset_connection_cache()


def test_noop_until_configured(tmp_path):
    # record_drop before configure() must not write or raise.
    detection_funnel.record_drop("guidance_raise", _Item())
    assert detection_funnel.load_drops(tmp_path / "paper_portfolio.json") == []


def test_record_and_load_roundtrip(tmp_path):
    path = tmp_path / "paper_portfolio.json"
    detection_funnel.configure("us", path)
    detection_funnel.record_drop("guidance_raise", _Item(title="Foo raises guidance"))
    drops = detection_funnel.load_drops(path)
    assert len(drops) == 1
    d = drops[0]
    assert d["service"] == "us"
    assert d["detector"] == "guidance_raise"
    assert d["reason"] == "no_ticker"
    assert "Foo raises guidance" in d["headline"]


def test_services_are_separated(tmp_path):
    path = tmp_path / "paper_portfolio.json"   # same cache dir => same db
    detection_funnel.configure("us", path)
    detection_funnel.record_drop("mna_target", _Item(title="US drop"))
    detection_funnel.configure("uk", tmp_path / "uk_portfolio.json")
    detection_funnel.record_drop("uk_director_dealing", _Item(title="UK drop"))

    summary = detection_funnel.drop_summary(path)
    by = {(r["service"], r["detector"]): r["n"] for r in summary}
    assert by[("us", "mna_target")] == 1
    assert by[("uk", "uk_director_dealing")] == 1


def test_prune_keeps_recent(tmp_path, monkeypatch):
    monkeypatch.setattr(detection_funnel, "_MAX_PER_SERVICE", 5)
    monkeypatch.setattr(detection_funnel, "_PRUNE_EVERY", 3)
    path = tmp_path / "paper_portfolio.json"
    detection_funnel.configure("us", path)
    for i in range(20):
        detection_funnel.record_drop("ai_pivot", _Item(title=f"drop {i}"))
    detection_funnel._prune()
    drops = detection_funnel.load_drops(path, limit=100)
    assert len(drops) <= 5
    # Most recent kept (newest first)
    assert "drop 19" in drops[0]["headline"]


def test_record_signal_drop_persists_ticker_and_reason(tmp_path):
    """Buy-time failures (no yfinance price, broker rejected) are now captured."""
    path = tmp_path / "t212_portfolio.json"
    detection_funnel.configure("t212", path)

    class Sig:
        ticker = "RAASY"
        headline = "RAASY announces strategic review"
        detector = "mna_target"
        url = "https://example.com/raasy"

    detection_funnel.record_signal_drop("mna_target", Sig(), "price_unavailable")
    detection_funnel.record_signal_drop(
        "guidance_raise", Sig(),
        "t212_rejected: T212OrderError: instrument not found",
    )

    drops = detection_funnel.load_drops(path)
    assert len(drops) == 2
    reasons = [d["reason"] for d in drops]
    assert "price_unavailable" in reasons
    assert any(r.startswith("t212_rejected:") for r in reasons)
    # ticker prefixed onto the headline so the dashboard shows what we tried
    assert all("[RAASY]" in d["headline"] for d in drops)


def test_record_drop_never_raises_on_bad_item(tmp_path):
    detection_funnel.configure("us", tmp_path / "paper_portfolio.json")
    # An object missing attributes must not blow up a scan.
    detection_funnel.record_drop("buyback", object())
    drops = detection_funnel.load_drops(tmp_path / "paper_portfolio.json")
    assert len(drops) == 1
    assert drops[0]["headline"] == ""
