from __future__ import annotations

import logging
import re
from dataclasses import dataclass
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

# "NASDAQ: BIRD", "NYSE:ABC", "(OTC: FOOB)" — capture the ticker code.
_TICKER_RX = re.compile(
    r"\b(?:NASDAQ|NYSE|NYSE\s*American|AMEX|OTC|OTCQB|OTCQX|TSX|CBOE)\s*[:\-]\s*([A-Z][A-Z0-9\.\-]{0,6})\b"
)


@dataclass(frozen=True)
class FeedItem:
    title: str
    summary: str
    url: str
    published: datetime
    source: str

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.summary}"

    def extract_ticker(self) -> str | None:
        match = _TICKER_RX.search(self.text)
        return match.group(1) if match else None


def _coerce_dt(entry: feedparser.FeedParserDict) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    return datetime.now(tz=timezone.utc)


def fetch(urls: Iterable[str] = DEFAULT_FEEDS, since: datetime | None = None) -> list[FeedItem]:
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
                )
            )
    return items
