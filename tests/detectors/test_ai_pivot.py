from switching.detectors.ai_pivot import classify
from switching.sources.rss import FeedItem


def test_classify_matches_allbirds_style_pivot():
    m = classify(
        "Allbirds Announces AI-Driven Retail Transformation",
        "The company is launching an AI platform to reposition its brand.",
    )
    assert m is not None
    assert m["severity"] >= 0.75


def test_classify_matches_buzzfeed_style():
    m = classify(
        "BuzzFeed to use OpenAI to create content, announces AI-powered strategy shift",
    )
    assert m is not None
    assert m["severity"] >= 0.70


def test_classify_rejects_routine_ai_mention():
    m = classify(
        "Quarterly earnings report from ACME Corp mentions AI in passing",
        "Routine update with no new announcement or launch.",
    )
    assert m is None


def test_classify_rejects_pivot_without_ai():
    m = classify(
        "Retailer unveils new handbag line",
        "Company launches fall collection with updated branding.",
    )
    assert m is None


def test_feed_item_extracts_ticker():
    item = FeedItem(
        title="Allbirds, Inc. (NASDAQ: BIRD) Announces AI Retail Pivot",
        summary="",
        url="https://example.com",
        published=__import__("datetime").datetime(2026, 4, 16),
        source="test",
    )
    assert item.extract_ticker() == "BIRD"


def test_feed_item_extracts_nyse_ticker():
    item = FeedItem(
        title="Palantir (NYSE: PLTR) unveils AIP artificial intelligence platform",
        summary="",
        url="https://example.com",
        published=__import__("datetime").datetime(2023, 4, 24),
        source="test",
    )
    assert item.extract_ticker() == "PLTR"
