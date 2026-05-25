from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

import feedparser

log = logging.getLogger(__name__)

# Default market for fetched items when a caller doesn't specify one. Each
# trading service runs in its own process and scans a single market, so
# scan_for_signals() sets this once per scan (per process) — see paper_trader.
# Kept as a module default rather than threaded through every detector's
# fetch() call.
_DEFAULT_MARKET = "us"


def set_default_market(market: str) -> None:
    """Set the market tag applied to fetched items that don't specify one."""
    global _DEFAULT_MARKET
    _DEFAULT_MARKET = market

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

# UK feeds via Google News RSS search.
#
# The old direct RNS sources are all dead: Investegate dropped RSS in its 2023
# redesign (404), Reuters discontinued public RSS in 2020 (DNS gone), and
# Proactive's /rss 404s. Google News RSS is the only free, always-on RSS
# firehose left — it lags the LSE primary RNS feed (it indexes journalist
# write-ups, not raw RNS) and ticker coverage is patchy, so this is a
# restore-flow probe. The proper low-latency fix (LSE RNS API direct + a UK
# ticker resolver) is the roadmap "primary-source ingestion" item.
_GOOGLE_NEWS_UK = "https://news.google.com/rss/search?q={q}&hl=en-GB&gl=GB&ceid=GB:en"
_UK_QUERIES: tuple[str, ...] = (
    "FTSE trading update ahead of expectations",
    "London listed company raises full year guidance",
    "recommended cash offer London listed takeover",
    "LSE special dividend OR dividend increase",
    "UK listed share buyback programme",
    "FTSE 100 OR FTSE 250 index reshuffle inclusion",
    "LSE director PDMR shareholding dealing",
    "London listed contract win awarded",
)
UK_FEEDS: tuple[str, ...] = tuple(
    _GOOGLE_NEWS_UK.format(q=urllib.parse.quote(q)) for q in _UK_QUERIES
)

# "NASDAQ: BIRD", "NYSE:ABC", "(OTC: FOOB)" — capture the ticker code.
_TICKER_RX = re.compile(
    r"\b(?:NASDAQ|NYSE|NYSE\s*American|AMEX|OTC|OTCQB|OTCQX|TSX|CBOE)\s*[:\-]\s*([A-Z][A-Z0-9\.\-]{0,6})\b"
)

# Prefixed EPIC, e.g. "(LSE:VOD)", "(LON:GAMA)", "(AIM:XYZ)" — Google News format.
_EPIC_PREFIXED_RX = re.compile(r"\((?:LSE|LON|AIM)\s*:\s*([A-Z][A-Z0-9]{1,5})\)")
# Bare EPIC in parentheses, e.g. "(BARC)", "(VOD)", "(RIO)" — Investegate format.
_EPIC_RX = re.compile(r"\(([A-Z]{2,5})\)")
# Words that look like a bare EPIC but aren't — reject to cut false positives.
_EPIC_STOPWORDS: frozenset[str] = frozenset({
    "LSE", "LON", "AIM", "RNS", "PLC", "LTD", "AGM", "EGM", "CEO", "CFO", "COO",
    "CIO", "GBP", "USD", "EUR", "GBX", "NAV", "EPS", "ESG", "IPO", "ETF", "REIT",
    "VAT", "HMRC", "FCA", "ISA", "FTSE", "UK", "USA", "EU", "US", "AI", "IT",
    "HR", "PR", "TV", "Q1", "Q2", "Q3", "Q4", "H1", "H2", "FY", "CET", "GMT",
})


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
            # 1. Prefixed EPIC "(LSE:VOD)" / "(LON:GAMA)" — unambiguous.
            pref = _EPIC_PREFIXED_RX.search(self.text)
            if pref:
                return f"{pref.group(1)}.L"
            # 2. Bare EPIC "(BARC)" — accept the first that isn't a common
            #    non-ticker word (PLC, AGM, GBP, FTSE, ...).
            for cand in _EPIC_RX.findall(self.text):
                if cand not in _EPIC_STOPWORDS:
                    return f"{cand}.L"
            # 3. Cross-listed: only an explicit US exchange prefix (NASDAQ:VOD).
            #    Do NOT use the SEC bare-paren lookup here — it's US-centric and
            #    would re-match parenthesised codes the stopword list rejected
            #    (e.g. "(AGM)" -> US ticker AGM), returning a US ticker for a UK
            #    item. Better to return None than trade the wrong instrument.
            m = _TICKER_RX.search(self.text)
            return m.group(1) if m else None
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


def _fetch_feedparser(urls: Iterable[str], since: datetime | None, mkt: str) -> list[FeedItem]:
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
                    market=mkt,
                )
            )
    return items


# --- UK source orchestration: Investegate (primary) + Google News (fallback) ---

_last_uk_failover_alert = 0.0
_UK_FAILOVER_COOLDOWN = 1800.0   # 30 min — don't spam the per-detector calls


def _norm_title(t: str) -> str:
    return " ".join(t.lower().split())


def _alert_uk_failover(reason: str) -> None:
    """Telegram alert when Investegate fails, cooldowned (fetch is called once
    per UK detector per cycle, so without this it would fire ~13x)."""
    global _last_uk_failover_alert
    import time as _time
    now = _time.time()
    if now - _last_uk_failover_alert < _UK_FAILOVER_COOLDOWN:
        return
    _last_uk_failover_alert = now
    log.warning("UK RSS failover: %s", reason)
    try:
        from switching import notifications
        notifications.notify_text(f"⚠️ UK RNS source failed ({reason}) — falling back to Google News.")
    except Exception:  # pragma: no cover — notifications optional
        pass


def _fetch_uk(google_urls: Iterable[str], since: datetime | None) -> list[FeedItem]:
    """Investegate is primary; Google News runs in parallel as a fallback/supplement.

    - Both succeed: merge, deduping Google items whose (normalised) headline a
      Investegate item already covers. Investegate wins.
    - Investegate fails / returns nothing: use Google News only + Telegram alert.
    """
    from switching.sources import investegate

    inv_items: list[FeedItem] = []
    inv_ok = False
    try:
        inv_items = investegate.scrape(since=since)
        inv_ok = len(inv_items) > 0
    except Exception as exc:
        _alert_uk_failover(f"scrape error: {type(exc).__name__}")
    else:
        if not inv_ok:
            _alert_uk_failover("0 items parsed")

    gn_items = _fetch_feedparser(google_urls, since, "uk")

    if not inv_ok:
        return gn_items

    # Merge: Investegate first, then Google items not already covered by title.
    seen = {_norm_title(it.title) for it in inv_items}
    merged = list(inv_items)
    added = 0
    for it in gn_items:
        if _norm_title(it.title) not in seen:
            merged.append(it)
            seen.add(_norm_title(it.title))
            added += 1
    log.info(
        "UK sources: investegate=%d, google=%d (+%d new after dedup) -> %d",
        len(inv_items), len(gn_items), added, len(merged),
    )
    return merged


def fetch(
    urls: Iterable[str] = DEFAULT_FEEDS,
    since: datetime | None = None,
    market: str | None = None,
) -> list[FeedItem]:
    # Fall back to the per-process default market when the caller doesn't pass
    # one (most detectors don't — see _DEFAULT_MARKET).
    mkt = market if market is not None else _DEFAULT_MARKET
    if mkt == "uk":
        return _fetch_uk(urls, since)
    return _fetch_feedparser(urls, since, mkt)
