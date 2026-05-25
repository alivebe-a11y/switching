"""Investegate RNS scraper — primary UK source.

Investegate dropped its RSS feed in the 2023 redesign, but the site is still
server-rendered HTML (not a JS SPA), so we scrape the announcements table
directly. It's a near-primary RNS aggregator: far more complete and lower
latency than Google News, and every row carries the EPIC ticker.

Row shape (from the live page):

    <tr>
      <td>22 May 2026 06:31 PM</td>                         # timestamp
      <td><a href=".../source/RNS">RNS</a></td>             # source
      <td><a href=".../company/GLV"><img/></a></td>          # chart icon
      <td><a href=".../company/GLV">Glenveagh Properties (CDI) (GLV)</a></td>
      <td><a href=".../announcement/rns/glenveagh-properties-cdi---glv/
              transaction-in-own-shares/9583338">Transaction in Own Shares</a></td>
    </tr>

The EPIC is in the announcement URL slug (``---glv/``) which is the most stable
anchor to parse, so extraction doesn't depend on the exact table markup. The
parser is deliberately tolerant and **fails loud** — if it extracts zero items
the caller treats it as a failure and falls back to Google News (see rss.py).

Scrape results are TTL-cached so the ~13 UK detectors that each call fetch()
within one scan cycle don't re-scrape the site every time.
"""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime, timezone

import requests

from switching.sources.rss import FeedItem

log = logging.getLogger(__name__)

# Homepage lists the latest announcements (verified to contain the RNS table).
INVESTEGATE_URL = "https://www.investegate.co.uk/"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; switching-bot/1.0)"}
_TIMEOUT = 15
_CACHE_TTL = 240.0  # seconds — one scrape covers a full scan cycle's detectors

_cache: dict = {"ts": 0.0, "items": []}

# Announcement anchor — tolerant of attributes before href (the real markup is
# `<a class="announcement-link" href="...">`) and of relative or absolute hrefs.
_ANN_RX = re.compile(
    r'<a\b[^>]*?href="([^"]*?/announcement/[^"]+)"[^>]*>\s*([^<]+?)\s*</a>',
    re.I,
)
# Announcement URL path: /announcement/<src>/<company-slug>/<title-slug>/<id>.
# The company-slug ends with the EPIC after a run of dashes, e.g.
# "nokia-oyj--0haf", "sage-group--sge", "glenveagh-properties-cdi---glv".
_SLUG_RX = re.compile(r"/announcement/[^/]+/([^/]+)/[^/]+/\d+")
# A tradeable UK EPIC: letter-leading, 2-5 alphanumerics. This deliberately
# skips digit-leading depositary lines (0HAF, 0KEH) — foreign-company LSE lines
# we don't want to trade.
_EPIC_OK_RX = re.compile(r"^[A-Z][A-Z0-9]{1,4}$")
# Timestamp like "22 May 2026 06:31 PM".
_TS_RX = re.compile(r'(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\s+\d{1,2}:\d{2}\s+[AP]M)')


def _epic_from_url(url: str) -> str | None:
    m = _SLUG_RX.search(url)
    if not m:
        return None
    tokens = [t for t in m.group(1).split("-") if t]
    if not tokens:
        return None
    epic = tokens[-1].upper()
    return epic if _EPIC_OK_RX.match(epic) else None


def _parse_ts(s: str) -> datetime | None:
    for fmt in ("%d %b %Y %I:%M %p", "%d %B %Y %I:%M %p"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse(raw_html: str) -> list[FeedItem]:
    """Parse the announcements table HTML into FeedItems (market='uk').

    Tolerant: iterates table-row chunks, and for each that contains an
    announcement anchor with a resolvable EPIC in its URL, emits an item. Rows
    without a tradeable EPIC (or digit-leading depositary lines) are skipped.
    """
    items: list[FeedItem] = []
    now = datetime.now(tz=timezone.utc)
    seen_urls: set[str] = set()
    for chunk in re.split(r"<tr[ >]", raw_html):
        ann = _ANN_RX.search(chunk)
        if not ann:
            continue
        url = ann.group(1)
        headline = html.unescape(ann.group(2).strip())
        if url in seen_urls:
            continue
        epic = _epic_from_url(url)
        if not epic:
            continue
        ts_m = _TS_RX.search(chunk)
        dt = _parse_ts(ts_m.group(1)) if ts_m else None
        if dt is None:
            dt = now
        seen_urls.add(url)
        # Append "(EPIC)" so the shared extract_ticker() resolves it to EPIC.L.
        items.append(FeedItem(
            title=f"{headline} ({epic})",
            summary="",
            url=url,
            published=dt,
            source=INVESTEGATE_URL,
            market="uk",
        ))
    return items


def scrape(since: datetime | None = None, *, force: bool = False) -> list[FeedItem]:
    """Return Investegate RNS items, TTL-cached. Raises on HTTP failure;
    returns [] if the page parsed to zero items (caller treats both as failover).
    """
    now = time.time()
    if not force and _cache["items"] and (now - _cache["ts"] < _CACHE_TTL):
        items = _cache["items"]
    else:
        resp = requests.get(INVESTEGATE_URL, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        items = parse(resp.text)
        _cache["ts"] = now
        _cache["items"] = items
        log.info("investegate: scraped %d announcements", len(items))
    if since:
        return [it for it in items if it.published >= since]
    return list(items)


def _reset_cache() -> None:
    """Clear the scrape cache (used by tests)."""
    _cache["ts"] = 0.0
    _cache["items"] = []
