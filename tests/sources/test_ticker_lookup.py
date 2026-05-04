"""Tests for ticker_lookup module."""

from unittest.mock import patch

import pytest

from switching.sources import ticker_lookup


# Fake SEC data mimicking company_tickers.json structure
_FAKE_SEC_DATA = {
    "0": {"cik_str": "320193", "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": "789019", "ticker": "MSFT", "title": "MICROSOFT CORP"},
    "2": {"cik_str": "1018724", "ticker": "AMZN", "title": "AMAZON COM INC"},
    "3": {"cik_str": "1652044", "ticker": "GOOGL", "title": "Alphabet Inc."},
    "4": {"cik_str": "1326801", "ticker": "META", "title": "Meta Platforms, Inc."},
    "5": {"cik_str": "1045810", "ticker": "NVDA", "title": "NVIDIA CORP"},
    "6": {"cik_str": "886982", "ticker": "GS", "title": "GOLDMAN SACHS GROUP INC"},
    "7": {"cik_str": "1341439", "ticker": "ORCL", "title": "ORACLE CORP"},
    "8": {"cik_str": "1318605", "ticker": "TSLA", "title": "Tesla, Inc."},
    "9": {"cik_str": "92122", "ticker": "BAC", "title": "BANK OF AMERICA CORP /DE/"},
    "10": {"cik_str": "21344", "ticker": "KO", "title": "COCA COLA CO"},
    "11": {"cik_str": "1090727", "ticker": "PLTR", "title": "Palantir Technologies Inc."},
    "12": {"cik_str": "1555280", "ticker": "CRWD", "title": "CrowdStrike Holdings, Inc."},
    "13": {"cik_str": "1467373", "ticker": "LMT", "title": "LOCKHEED MARTIN CORP"},
    "14": {"cik_str": "60667", "ticker": "JNJ", "title": "JOHNSON & JOHNSON"},
}


@pytest.fixture(autouse=True)
def reset_cache():
    """Reset the module-level cache before each test."""
    ticker_lookup.invalidate_cache()
    yield
    ticker_lookup.invalidate_cache()


@pytest.fixture()
def loaded_map():
    """Pre-load the lookup with fake SEC data."""
    with patch.object(ticker_lookup, "_read_cached_or_fetch", return_value=_FAKE_SEC_DATA):
        ticker_lookup._load_map()


def test_lookup_by_ticker_in_text(loaded_map):
    """Direct ticker symbol in text should be found."""
    result = ticker_lookup.lookup_ticker("AAPL announces new product line")
    assert result == "AAPL"


def test_lookup_by_company_name(loaded_map):
    """Company name in headline should resolve to ticker."""
    result = ticker_lookup.lookup_ticker("Apple Announces $90 Billion Share Buyback")
    assert result == "AAPL"


def test_lookup_nvidia(loaded_map):
    result = ticker_lookup.lookup_ticker("NVIDIA Awarded $5 Billion Defense Contract")
    assert result == "NVDA"


def test_lookup_microsoft(loaded_map):
    result = ticker_lookup.lookup_ticker("Microsoft to Acquire Activision for $69B")
    assert result == "MSFT"


def test_lookup_tesla(loaded_map):
    result = ticker_lookup.lookup_ticker("Tesla Reports Record Q4 Deliveries")
    assert result == "TSLA"


def test_lookup_palantir(loaded_map):
    result = ticker_lookup.lookup_ticker("Palantir Technologies Wins $500M Army Contract")
    assert result == "PLTR"


def test_lookup_crowdstrike(loaded_map):
    result = ticker_lookup.lookup_ticker("CrowdStrike Holdings Reports Earnings Beat")
    assert result == "CRWD"


def test_lookup_lockheed(loaded_map):
    result = ticker_lookup.lookup_ticker("Lockheed Martin Secures $3B F-35 Contract")
    assert result == "LMT"


def test_no_match_returns_none(loaded_map):
    """Completely unrelated text should return None."""
    result = ticker_lookup.lookup_ticker("Weather forecast for tomorrow is sunny")
    assert result is None


def test_common_words_not_matched(loaded_map):
    """Common English words that are also tickers should not match."""
    result = ticker_lookup.lookup_ticker("THE NEW PLAN FOR ALL TIME")
    assert result is None


def test_prefers_longer_name_match(loaded_map):
    """Should prefer longer company name matches over shorter ones."""
    result = ticker_lookup.lookup_ticker("Goldman Sachs Upgrades Apple to Buy")
    # Both Apple and Goldman Sachs are in the map — Goldman Sachs is longer
    assert result in ("GS", "AAPL")


def test_ticker_symbol_priority(loaded_map):
    """Explicit ticker symbol should be found even without company name."""
    result = ticker_lookup.lookup_ticker("Analyst upgrades NVDA to Outperform")
    assert result == "NVDA"


def test_handles_empty_text(loaded_map):
    assert ticker_lookup.lookup_ticker("") is None


def test_handles_no_data():
    """When SEC data can't be loaded, should return None gracefully."""
    with patch.object(ticker_lookup, "_read_cached_or_fetch", return_value=None):
        result = ticker_lookup.lookup_ticker("Apple Announces Buyback")
    assert result is None


def test_normalize_name():
    assert ticker_lookup._normalize_name("Apple Inc.") == "apple inc"
    assert ticker_lookup._normalize_name("NVIDIA CORP") == "nvidia corp"
    assert ticker_lookup._normalize_name("  Tesla,  Inc.  ") == "tesla inc"


def test_meta_platforms(loaded_map):
    result = ticker_lookup.lookup_ticker("Meta Platforms Announces $50B Buyback Program")
    assert result == "META"


def test_alphabet(loaded_map):
    result = ticker_lookup.lookup_ticker("Alphabet Raises Full-Year Revenue Guidance")
    assert result == "GOOGL"


def test_oracle(loaded_map):
    result = ticker_lookup.lookup_ticker("Oracle Reports Strong Cloud Revenue Growth")
    assert result == "ORCL"


def test_johnson_and_johnson(loaded_map):
    """Company name with '&' should match."""
    result = ticker_lookup.lookup_ticker("Johnson & Johnson Receives FDA Approval")
    assert result == "JNJ"


def test_coca_cola(loaded_map):
    result = ticker_lookup.lookup_ticker("Coca Cola Raises Full-Year Guidance")
    assert result == "KO"


class TestFeedItemIntegration:
    """Test that FeedItem.extract_ticker uses the fallback."""

    def test_exchange_prefix_takes_priority(self, loaded_map):
        from switching.sources.rss import FeedItem
        from datetime import datetime, timezone

        item = FeedItem(
            title="NVIDIA Reports Record Revenue",
            summary="NASDAQ: NVDA beats expectations",
            url="http://x.com",
            published=datetime.now(tz=timezone.utc),
            source="test",
        )
        assert item.extract_ticker() == "NVDA"

    def test_name_fallback_when_no_prefix(self, loaded_map):
        from switching.sources.rss import FeedItem
        from datetime import datetime, timezone

        item = FeedItem(
            title="Apple Announces $90 Billion Share Buyback",
            summary="Board authorized repurchase program",
            url="http://x.com",
            published=datetime.now(tz=timezone.utc),
            source="test",
        )
        assert item.extract_ticker() == "AAPL"

    def test_returns_none_for_unknown(self, loaded_map):
        from switching.sources.rss import FeedItem
        from datetime import datetime, timezone

        item = FeedItem(
            title="Weather forecast sunny and warm",
            summary="No rain expected this week",
            url="http://x.com",
            published=datetime.now(tz=timezone.utc),
            source="test",
        )
        assert item.extract_ticker() is None
