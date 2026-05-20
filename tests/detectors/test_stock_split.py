"""Tests for the stock_split detector."""

from switching.detectors.stock_split import classify
from switching.sources.rss import FeedItem
import datetime


# ── Positive matches ──────────────────────────────────────────────────────────

def test_declares_ratio():
    m = classify("Nvidia Declares 10-for-1 Stock Split")
    assert m is not None
    assert m["severity"] >= 0.80
    assert m["ratio"] == "10-for-1"


def test_announces_ratio():
    m = classify("Apple Announces 4-for-1 Stock Split Effective August 28")
    assert m is not None
    assert m["ratio"] == "4-for-1"


def test_approves_ratio():
    m = classify("Tesla Board Approves 3-for-1 Stock Split")
    assert m is not None
    assert m["severity"] >= 0.75  # board approval bonus


def test_twenty_for_one():
    m = classify("Amazon Announces 20-for-1 Stock Split")
    assert m is not None
    assert m["severity"] >= 0.80  # ≥10-for-1 bonus


def test_board_bonus():
    m = classify("Broadcom Board of Directors Approves 10-for-1 Stock Split")
    assert m is not None
    assert m["severity"] >= 0.85  # big ratio + board


def test_no_ratio_still_matches():
    # No ratio given but action verb + "stock split" present
    m = classify("Shopify Announces Forward Stock Split")
    assert m is not None
    assert m["severity"] >= 0.65


def test_summary_boosts_evidence():
    m = classify(
        "Palo Alto Networks Declares Stock Split",
        "The board approved a 3-for-1 forward stock split effective June 14.",
    )
    assert m is not None
    assert m["ratio"] == "3-for-1"


# ── Reverse split filter ──────────────────────────────────────────────────────

def test_rejects_reverse_split():
    assert classify("Bed Bath & Beyond Announces Reverse Stock Split") is None


def test_rejects_reverse_in_summary():
    assert classify("Company Stock Split Announcement", "This is a reverse stock split.") is None


# ── Noise rejections ──────────────────────────────────────────────────────────

def test_rejects_no_action_verb():
    # "stock split" present but no action verb — historical reference
    assert classify("History of Apple's Stock Split Track Record") is None


def test_rejects_no_stock_split():
    assert classify("Apple Announces Record Quarterly Earnings") is None


def test_rejects_generic_split_word():
    assert classify("Company plans to split into two divisions") is None


# ── Severity cap ─────────────────────────────────────────────────────────────

def test_severity_capped():
    m = classify(
        "Nvidia Board Approves 10-for-1 Stock Split Effective June 10",
        "Record date set. Effective date June 10.",
    )
    assert m is not None
    assert m["severity"] <= 0.95


# ── Ticker extraction ─────────────────────────────────────────────────────────

def test_ticker_extraction():
    item = FeedItem(
        title="Nvidia (NASDAQ: NVDA) Announces 10-for-1 Stock Split",
        summary="",
        url="https://example.com",
        published=datetime.datetime(2024, 5, 22),
        source="test",
    )
    assert item.extract_ticker() == "NVDA"
