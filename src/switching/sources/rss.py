from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

import feedparser

log = logging.getLogger(__name__)

DEFAULT_FEEDS: tuple[str, ...] = (
    # PR Newswire — technology
    "https://www.prnewswire.com/rss/technology-latest-news/technology-latest-news-list.rss",
    # BusinessWire — technology
    "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeGVtRVA==",
    # GlobeNewswire — technology
    "https://www.globenewswire.com/RssFeed/industry/9576-Technology/feedTitle/GlobeNewswire%20-%20Technology",
)

EARNINGS_FEEDS: tuple[str, ...] = (
    # PR Newswire — financial services / earnings
    "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss",
    # BusinessWire — financial
    "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeGVpSWA==",
    # GlobeNewswire — financial services
    "https://www.globenewswire.com/RssFeed/industry/9531-Financial%20Services/feedTitle/GlobeNewswire%20-%20Financial%20Services",
    # PR Newswire — all general news (catches cross-sector earnings)
    "https://www.prnewswire.com/rss/news-releases-list.rss",
    # GlobeNewswire — all news
    "https://www.globenewswire.com/RssFeed/feedTitle/GlobeNewswire%20-%20All%20News",
)

CORPORATE_FEEDS: tuple[str, ...] = (
    # PR Newswire — general (buybacks, spinoffs, index changes cross all sectors)
    "https://www.prnewswire.com/rss/news-releases-list.rss",
    # BusinessWire — all news
    "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEQ==",
    # GlobeNewswire — all news
    "https://www.globenewswire.com/RssFeed/feedTitle/GlobeNewswire%20-%20All%20News",
    # PR Newswire — financial services
    "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss",
    # GlobeNewswire — financial services
    "https://www.globenewswire.com/RssFeed/industry/9531-Financial%20Services/feedTitle/GlobeNewswire%20-%20Financial%20Services",
)

UK_FEEDS: tuple[str, ...] = (
    # Investegate — all RNS regulatory announcements for LSE-listed companies
    "https://www.investegate.co.uk/rss/allnews.aspx",
    # Reuters UK business news
    "https://feeds.reuters.com/reuters/UKBusinessNews",
    # Proactive Investors — UK small/mid-cap coverage
    "https://www.proactiveinvestors.co.uk/rss",
)

# "NASDAQ: BIRD", "NYSE:ABC", "(OTC: FOOB)" — capture the ticker code.
_TICKER_RX = re.compile(
    r"\b(?:NASDAQ|NYSE|NYSE\s*American|AMEX|OTC|OTCQB|OTCQX|TSX|CBOE)\s*[:\-]\s*([A-Z][A-Z0-9\.\-]{0,6})\b"
)

# UK EPIC ticker codes appear in parentheses, e.g. "(BARC)", "(VOD)", "(RIO)"
_EPIC_RX = re.compile(r"\(([A-Z]{2,5})\)")


@dataclass(frozen=True)
class FeedItem:
    title: str
    summary: str
    url: str
    published: datetime
    source: str
    market: str = "us"

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.summary}"

    def extract_ticker(self) -> str | None:
        if self.market == "uk":
            # First try EPIC code in parentheses — return as yfinance LSE format
            epic_match = _EPIC_RX.search(self.text)
            if epic_match:
                return f"{epic_match.group(1)}.L"
            # Fall through to US pipeline for cross-listed companies
        match = _TICKER_RX.search(self.text)
        if match:
            return match.group(1)
        from switching.sources.ticker_lookup import lookup_ticker
        return lookup_ticker(self.text)


def _coerce_dt(entry: feedparser.FeedParserDict) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    return datetime.now(tz=timezone.utc)


def fetch(
    urls: Iterable[str] = DEFAULT_FEEDS,
    since: datetime | None = None,
    market: str = "us",
) -> list[FeedItem]:
    items: list[FeedItem] = []
    for url in urls:
        try:
            parsed = feedparser.parse(url)
        except Exception as exc:  # pragma: no cover — network paths
            log.warning("feed %s failed: %s", url, exc)
            continue
        for entry in parsed.entries:
            dt = _coerce_dt(entry)
            if since and dt < since:
                continue
            items.append(
                FeedItem(
                    title=(entry.get("title") or "").strip(),
                    summary=re.sub(r"<[^>]+>", " ", entry.get("summary") or "").strip(),
                    url=entry.get("link") or "",
                    published=dt,
                    source=url,
                    market=market,
                )
            )
    return items
