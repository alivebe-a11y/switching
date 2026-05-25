"""Tests for UK RSS feed support in sources/rss.py."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from switching.sources.rss import UK_FEEDS, FeedItem, fetch


# ---------------------------------------------------------------------------
# UK_FEEDS constant
# ---------------------------------------------------------------------------

def test_uk_feeds_exists():
    assert UK_FEEDS is not None


def test_uk_feeds_is_non_empty_tuple():
    assert isinstance(UK_FEEDS, tuple)
    assert len(UK_FEEDS) > 0


def test_uk_feeds_use_google_news():
    # The old direct RNS feeds are all dead; Google News RSS is the working source.
    assert all("news.google.com" in url for url in UK_FEEDS)


def test_uk_feeds_drop_dead_sources():
    joined = " ".join(UK_FEEDS).lower()
    assert "feeds.reuters.com" not in joined          # Reuters killed RSS in 2020
    assert "investegate.co.uk/rss" not in joined      # 404 after 2023 redesign
    assert "proactiveinvestors.co.uk/rss" not in joined


# ---------------------------------------------------------------------------
# extract_ticker() with market="uk"
# ---------------------------------------------------------------------------

def _make_uk_item(title: str, summary: str = "") -> FeedItem:
    return FeedItem(
        title=title,
        summary=summary,
        url="https://www.investegate.co.uk/test",
        published=datetime(2024, 6, 15, 10, 0, tzinfo=timezone.utc),
        source="https://www.investegate.co.uk/rss/allnews.aspx",
        market="uk",
    )


def test_extract_ticker_uk_epic_in_title():
    item = _make_uk_item("Barclays PLC (BARC) - Director/PDMR Shareholding")
    ticker = item.extract_ticker()
    assert ticker == "BARC.L"


def test_extract_ticker_uk_epic_in_summary():
    item = _make_uk_item("Director Dealing", "Lloyds Banking Group (LLOY) purchased shares")
    ticker = item.extract_ticker()
    assert ticker == "LLOY.L"


def test_extract_ticker_uk_longer_epic():
    item = _make_uk_item("AstraZeneca (AZN) - Results", "")
    ticker = item.extract_ticker()
    assert ticker == "AZN.L"


def test_extract_ticker_uk_prefixed_lse():
    # Google News format: "(LSE:VOD)"
    item = _make_uk_item("Vodafone Group (LSE:VOD) raises full-year guidance")
    assert item.extract_ticker() == "VOD.L"


def test_extract_ticker_uk_prefixed_lon():
    item = _make_uk_item("Gamma Communications (LON:GAMA) trading update")
    assert item.extract_ticker() == "GAMA.L"


def test_extract_ticker_uk_rejects_stopword_parens():
    # Bare parenthesised non-tickers must not be mistaken for an EPIC.
    item = _make_uk_item("Trading statement (RNS) and (AGM) notice", "")
    assert item.extract_ticker() is None


def test_extract_ticker_uk_prefixed_beats_bare_stopword():
    # A real prefixed EPIC should win even if a stopword-paren precedes it.
    item = _make_uk_item("(RNS) Reach (LON:RCH) results ahead of expectations")
    assert item.extract_ticker() == "RCH.L"


def test_fetch_uses_default_market_when_unset():
    """Detectors call fetch() without market=; the per-process default applies."""
    import switching.sources.rss as rss_mod
    mock_parsed = MagicMock()
    mock_parsed.entries = [_mock_feedparser_entry("Tesco (LSE:TSCO) trading update")]
    rss_mod.set_default_market("uk")
    try:
        # Mock the Investegate scrape (offline + deterministic) so only the
        # mocked Google/feedparser item is returned.
        with patch("switching.sources.investegate.scrape", return_value=[]), \
             patch("switching.sources.rss._alert_uk_failover"), \
             patch("switching.sources.rss.feedparser.parse", return_value=mock_parsed):
            items = fetch(["https://news.google.com/rss/search?q=x"])  # no market arg
        assert items[0].market == "uk"
        assert items[0].extract_ticker() == "TSCO.L"
    finally:
        rss_mod.set_default_market("us")


def test_extract_ticker_uk_never_uses_sec_lookup():
    """A UK item with no EPIC must NOT fall through to the US SEC lookup
    (which would mis-resolve parenthesised codes like (AGM) to a US ticker)."""
    item = _make_uk_item("Trading statement (RNS) and (AGM) notice", "")
    with patch("switching.sources.ticker_lookup.lookup_ticker", return_value="AGM") as m:
        assert item.extract_ticker() is None
    m.assert_not_called()


def test_extract_ticker_uk_cross_listed_exchange_prefix():
    """An explicit US exchange prefix in a UK item still resolves (cross-listed)."""
    item = _make_uk_item("Vodafone reports (NASDAQ: VOD) ADR update", "")
    assert item.extract_ticker() == "VOD"


def test_extract_ticker_us_market_still_works():
    """Regression: US FeedItems still extract tickers without .L suffix."""
    item = FeedItem(
        title="Microsoft (NASDAQ: MSFT) announces quarterly results",
        summary="",
        url="https://example.com",
        published=datetime(2024, 6, 15, 10, 0, tzinfo=timezone.utc),
        source="https://www.prnewswire.com/rss/",
        market="us",
    )
    ticker = item.extract_ticker()
    assert ticker == "MSFT"
    assert not (ticker or "").endswith(".L")


def test_feeditem_market_defaults_to_us():
    """Existing code that creates FeedItem without market= must still work."""
    item = FeedItem(
        title="Test",
        summary="",
        url="https://example.com",
        published=datetime(2024, 6, 15, 10, 0, tzinfo=timezone.utc),
        source="https://example.com",
    )
    assert item.market == "us"


# ---------------------------------------------------------------------------
# fetch() with market="uk"
# ---------------------------------------------------------------------------

def _mock_feedparser_entry(title: str = "Test", summary: str = "Summary") -> MagicMock:
    entry = MagicMock()
    entry.get = lambda key, default=None: {
        "title": title,
        "summary": summary,
        "link": "https://example.com/test",
        "published_parsed": None,
        "updated_parsed": None,
    }.get(key, default)
    return entry


def test_fetch_with_market_uk_sets_market_field():
    # market=uk routes through _fetch_uk (Investegate + Google). Mock the
    # Investegate scrape so the test stays offline and deterministic — here it
    # returns nothing, so only the mocked Google/feedparser item comes through.
    mock_parsed = MagicMock()
    mock_parsed.entries = [_mock_feedparser_entry("Barclays PLC (BARC) - Dealing")]

    with patch("switching.sources.investegate.scrape", return_value=[]), \
         patch("switching.sources.rss._alert_uk_failover"), \
         patch("switching.sources.rss.feedparser.parse", return_value=mock_parsed):
        items = fetch(["https://news.google.com/rss/search?q=x"], market="uk")

    assert len(items) == 1
    assert items[0].market == "uk"


def test_fetch_default_market_is_us():
    mock_parsed = MagicMock()
    mock_parsed.entries = [_mock_feedparser_entry("Microsoft (NASDAQ: MSFT) earnings")]

    with patch("switching.sources.rss.feedparser.parse", return_value=mock_parsed):
        items = fetch(["https://www.prnewswire.com/rss/"])

    assert len(items) == 1
    assert items[0].market == "us"


# ---------------------------------------------------------------------------
# UK source orchestration: Investegate primary + Google fallback + dedup
# ---------------------------------------------------------------------------

def _item(title, url, source="x"):
    return FeedItem(title=title, summary="", url=url,
                    published=datetime(2026, 5, 22, tzinfo=timezone.utc),
                    source=source, market="uk")


def test_uk_fetch_merges_and_dedupes():
    inv = [_item("Trading Statement (SGE)", "inv://sge")]
    gn = [_item("Trading Statement (SGE)", "gn://sge"),     # dup title -> dropped
          _item("Vodafone guidance (VOD)", "gn://vod")]     # unique -> kept
    from switching.sources import rss
    with patch("switching.sources.investegate.scrape", return_value=inv), \
         patch("switching.sources.rss._fetch_feedparser", return_value=gn):
        items = rss._fetch_uk(["g"], None)
    titles = [i.title for i in items]
    assert titles.count("Trading Statement (SGE)") == 1   # deduped
    assert "Vodafone guidance (VOD)" in titles            # supplemented
    assert items[0].url == "inv://sge"                     # Investegate wins


def test_uk_fetch_falls_back_to_google_on_scrape_error():
    from switching.sources import rss
    alerts = []
    with patch("switching.sources.investegate.scrape", side_effect=RuntimeError("boom")), \
         patch("switching.sources.rss._fetch_feedparser", return_value=[_item("X (VOD)", "gn://x")]), \
         patch("switching.sources.rss._alert_uk_failover", side_effect=lambda r: alerts.append(r)):
        items = rss._fetch_uk(["g"], None)
    assert [i.url for i in items] == ["gn://x"]
    assert len(alerts) == 1


def test_uk_fetch_alerts_when_investegate_returns_zero():
    from switching.sources import rss
    alerts = []
    with patch("switching.sources.investegate.scrape", return_value=[]), \
         patch("switching.sources.rss._fetch_feedparser", return_value=[_item("X (VOD)", "gn://x")]), \
         patch("switching.sources.rss._alert_uk_failover", side_effect=lambda r: alerts.append(r)):
        items = rss._fetch_uk(["g"], None)
    assert [i.url for i in items] == ["gn://x"]
    assert len(alerts) == 1


def test_fetch_market_uk_routes_through_orchestration():
    from switching.sources import rss
    with patch("switching.sources.rss._fetch_uk", return_value=[_item("a", "u")]) as m:
        items = fetch(["g"], market="uk")
    m.assert_called_once()
    assert len(items) == 1


def test_uk_failover_alert_is_cooldowned():
    """Repeated failures within the cooldown window send at most one alert."""
    from switching.sources import rss
    rss._last_uk_failover_alert = 0.0
    sent = []
    with patch("switching.notifications.notify_text", side_effect=lambda t: sent.append(t)):
        rss._alert_uk_failover("first")
        rss._alert_uk_failover("second")   # within cooldown -> suppressed
    assert len(sent) == 1
    rss._last_uk_failover_alert = 0.0
