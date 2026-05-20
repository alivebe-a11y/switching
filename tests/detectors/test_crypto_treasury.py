"""Tests for the crypto_treasury detector."""

from switching.detectors.crypto_treasury import classify
from switching.sources.rss import FeedItem
import datetime


# ── Positive matches ──────────────────────────────────────────────────────────

def test_microstrategy_style_purchase():
    m = classify("MicroStrategy Purchases 12,222 Bitcoin for $805.2 Million")
    assert m is not None
    assert m["severity"] >= 0.80
    assert m["purchase_size"] is not None


def test_treasury_adoption():
    m = classify(
        "Semler Scientific Adopts Bitcoin as Primary Treasury Reserve Asset",
        "The company has adopted bitcoin as its primary treasury reserve asset.",
    )
    assert m is not None
    assert m["severity"] >= 0.70


def test_board_approves_strategy():
    m = classify("GameStop Board Approves Bitcoin Treasury Strategy")
    assert m is not None
    assert m["severity"] >= 0.75  # board approval bonus


def test_strategic_reserve_language():
    m = classify("Company Announces Strategic Bitcoin Reserve")
    assert m is not None
    assert m["severity"] >= 0.75


def test_billion_dollar_purchase():
    m = classify("Strategy Purchases 20,356 Bitcoin for $2.0 Billion")
    assert m is not None
    assert m["severity"] >= 0.85  # $500M+ bonus


def test_hundred_million_purchase():
    m = classify("Metaplanet Announces Bitcoin Treasury Investment of $100 Million")
    assert m is not None
    assert m["severity"] >= 0.80


def test_adds_bitcoin_to_balance_sheet():
    m = classify(
        "ACM Corp Announces Bitcoin Treasury Allocation",
        "The company plans to add bitcoin to its corporate balance sheet as a reserve asset.",
    )
    assert m is not None


def test_acquires_btc():
    m = classify("MicroStrategy Acquires Additional 5,050 Bitcoin for $242.9 Million")
    assert m is not None
    assert m["severity"] >= 0.80


# ── Noise rejections ──────────────────────────────────────────────────────────

def test_rejects_bitcoin_etf_approval():
    assert classify("SEC Approves Bitcoin ETF Applications from BlackRock") is None


def test_rejects_bitcoin_price_news():
    assert classify("Bitcoin Price Falls 5% as Market Volatility Increases") is None


def test_rejects_crypto_exchange_launch():
    assert classify("Coinbase Launches Bitcoin Trading for Institutional Clients") is None


def test_rejects_no_treasury_action():
    # Bitcoin mentioned but no treasury/acquisition language
    assert classify("Company Reports Q3 Earnings, Notes Bitcoin Market Uncertainty") is None


def test_rejects_bitcoin_mining():
    assert classify("Marathon Digital Announces Bitcoin Mining Expansion") is None


def test_rejects_no_bitcoin():
    assert classify("Company Announces Treasury Reserve Strategy Update") is None


# ── Severity cap ─────────────────────────────────────────────────────────────

def test_severity_capped():
    m = classify(
        "Board Approves Strategic Bitcoin Reserve Purchase of $5 Billion",
        "Company announces strategic bitcoin treasury reserve.",
    )
    assert m is not None
    assert m["severity"] <= 0.95


# ── Ticker extraction ─────────────────────────────────────────────────────────

def test_ticker_extraction():
    item = FeedItem(
        title="MicroStrategy (NASDAQ: MSTR) Purchases 11,000 Bitcoin for $489 Million",
        summary="",
        url="https://example.com",
        published=datetime.datetime(2024, 7, 8),
        source="test",
    )
    assert item.extract_ticker() == "MSTR"
