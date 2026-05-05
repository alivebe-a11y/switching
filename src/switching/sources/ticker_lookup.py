"""Company-name-to-ticker resolution using SEC company_tickers.json.

This module provides a fallback ticker extraction mechanism for RSS headlines
that don't include exchange-prefixed tickers (e.g. "NASDAQ:AAPL"). Most
financial press releases mention the company name but not the ticker symbol.

The SEC publishes ~13,000 company-to-ticker mappings at:
  https://www.sec.gov/files/company_tickers.json

We cache this data in memory (refreshed once per process lifetime) and provide
a `lookup_ticker(text)` function that scans text for known company names.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_CACHE_FILE = Path(os.environ.get("SWITCHING_CACHE_DIR", "/tmp")) / "sec_company_tickers.json"
_CACHE_MAX_AGE = 86400 * 7  # refresh weekly

_lock = threading.Lock()
_name_to_ticker: dict[str, str] | None = None
_ticker_to_name: dict[str, str] | None = None

_MNA_VERB_RX = re.compile(r"\b(?:acquires?|acquisition|buys?|purchases?|merges?\s+with)\b")

# Common suffixes to strip for matching
_SUFFIXES = re.compile(
    r"\s*(?:,?\s*(?:Inc\.?|Corp\.?|Corporation|Ltd\.?|Limited|PLC|plc|LLC|L\.P\.|LP|N\.V\.|S\.A\.|SE|AG|Co\.?|Group|Holdings?|Bancorp|Technologies|Technology|Therapeutics|Pharmaceuticals|Biosciences|Solutions))+\s*$",
    re.IGNORECASE,
)

# Words that are too generic to be company names
_STOP_WORDS = frozenset({
    "the", "a", "an", "and", "or", "for", "to", "in", "of", "on", "at",
    "by", "from", "with", "that", "this", "its", "new", "all", "first",
    "today", "announces", "reports", "company", "board", "shares",
})


def _load_map() -> tuple[dict[str, str], dict[str, str]]:
    """Load or refresh the SEC company name → ticker mapping."""
    global _name_to_ticker, _ticker_to_name

    with _lock:
        if _name_to_ticker is not None:
            return _name_to_ticker, _ticker_to_name  # type: ignore

        raw_data = _read_cached_or_fetch()
        if raw_data is None:
            _name_to_ticker = {}
            _ticker_to_name = {}
            return _name_to_ticker, _ticker_to_name

        n2t: dict[str, str] = {}
        t2n: dict[str, str] = {}

        for entry in raw_data.values():
            ticker = entry.get("ticker", "").upper()
            title = entry.get("title", "")
            if not ticker or not title:
                continue

            t2n[ticker] = title

            # Index both the full name and the stripped name
            normalized = _normalize_name(title)
            if normalized and len(normalized) >= 3:
                n2t[normalized] = ticker

            # Also index without suffixes
            stripped = _SUFFIXES.sub("", title).strip()
            norm_stripped = _normalize_name(stripped)
            if norm_stripped and len(norm_stripped) >= 3 and norm_stripped != normalized:
                if norm_stripped not in n2t:
                    n2t[norm_stripped] = ticker

        _name_to_ticker = n2t
        _ticker_to_name = t2n
        log.info("ticker_lookup: loaded %d company names → tickers", len(n2t))
        return _name_to_ticker, _ticker_to_name


def _read_cached_or_fetch() -> dict | None:
    """Read from disk cache or fetch from SEC."""
    if _CACHE_FILE.exists():
        age = time.time() - _CACHE_FILE.stat().st_mtime
        if age < _CACHE_MAX_AGE:
            try:
                return json.loads(_CACHE_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                pass

    try:
        ua = os.environ.get("SWITCHING_EDGAR_UA", "switching-bot admin@example.com")
        req = urllib.request.Request(
            _TICKER_MAP_URL,
            headers={"User-Agent": ua},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        raw = json.loads(data)
        # Cache to disk
        try:
            _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_FILE.write_bytes(data)
        except OSError:
            pass
        return raw
    except Exception as exc:
        log.warning("ticker_lookup: failed to fetch SEC data: %s", exc)
        # Try stale cache
        if _CACHE_FILE.exists():
            try:
                return json.loads(_CACHE_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return None


def _normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    s = name.lower().strip()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def lookup_ticker(text: str) -> str | None:
    """Scan text for a known SEC-registered company name, return its ticker.

    Two strategies, in order:
    1. Parenthesized ticker: (AAPL) or (NASDAQ: AAPL) patterns
    2. Company name match in the headline's first ~120 chars (longest wins)

    We do NOT scan for bare uppercase words as ticker symbols — too many
    false positives (AI, G, F, V, etc. appear as common words in headlines).
    """
    n2t, _ = _load_map()
    if not n2t:
        return None

    # Strategy 1: Parenthesized ticker symbols — high confidence
    # Matches "(AAPL)", "(ticker: MSFT)", "(NYSE: XOM)" etc.
    paren_tickers = re.findall(r"\((?:\w+:\s*)?([A-Z]{2,5})\)", text)
    _, t2n = _load_map()
    for candidate in paren_tickers:
        if candidate in t2n and candidate not in _COMMON_WORDS_UPPER:
            return candidate

    # Strategy 2: Match company names from the headline
    # Press releases typically start with "CompanyName Announces..."
    normalized_text = _normalize_name(text)

    # Only search the headline portion (first line, or first ~120 chars)
    first_line = normalized_text.split("\n")[0] if "\n" in normalized_text else normalized_text
    search_text = first_line[:120]

    best_match: str | None = None
    best_len = 0

    has_mna_verb = bool(_MNA_VERB_RX.search(search_text))

    for name, ticker in n2t.items():
        if len(name) <= best_len:
            continue
        if len(name) < 5:
            continue
        if name in search_text:
            idx = search_text.find(name)
            before_ok = idx == 0 or not search_text[idx - 1].isalnum()
            after_idx = idx + len(name)
            after_ok = after_idx >= len(search_text) or not search_text[after_idx].isalnum()
            if not (before_ok and after_ok):
                continue
            # Names not at position 0 must be longer to reduce false positives
            if idx > 0 and len(name) < 8:
                continue
            # In M&A headlines, reject matches after the verb (likely a private target)
            if has_mna_verb and idx > 0:
                before_text = search_text[:idx]
                if _MNA_VERB_RX.search(before_text):
                    continue
            best_match = ticker
            best_len = len(name)

    return best_match


# Common English words that happen to be valid ticker symbols
_COMMON_WORDS_UPPER = frozenset({
    "A", "I", "AM", "AN", "AS", "AT", "BE", "BY", "DO", "GO",
    "HE", "IF", "IN", "IS", "IT", "ME", "MY", "NO", "OF", "ON",
    "OR", "SO", "TO", "UP", "US", "WE", "CEO", "CFO", "FDA", "SEC",
    "IPO", "ETF", "CEO", "COO", "ALL", "FOR", "NEW", "NOW", "ONE",
    "OUR", "OUT", "OWN", "TWO", "WAR", "BIG", "CAN", "HAS", "HER",
    "HIS", "HOW", "ITS", "LET", "MAY", "OLD", "RUN", "SAY", "SHE",
    "THE", "TOO", "TOP", "TRY", "USE", "WAY", "WHO", "WIN", "MAN",
    "ARE", "BUT", "DAY", "DID", "GET", "GOT", "HAD", "HIM", "NOT",
    "SET", "WAS", "ADD", "AGO", "AID", "AIM", "AIR", "ARM", "ART",
    "BAD", "BAR", "BED", "BIT", "BOX", "BUS", "BUY", "CAR", "CUT",
    "DOD", "NASA", "NYSE", "ALSO", "BEEN", "BEST", "BOTH", "CASE",
    "DEAL", "EACH", "EVEN", "FACT", "FIND", "FIVE", "FOUR", "FROM",
    "FULL", "GAIN", "GAVE", "GOOD", "HALF", "HAVE", "HEAD", "HELD",
    "HERE", "HIGH", "HOLD", "HOME", "HUGE", "IDEA", "INTO", "JUST",
    "KEEP", "KNEW", "KNOW", "LAND", "LAST", "LATE", "LEAD", "LEFT",
    "LESS", "LIFE", "LINE", "LIST", "LONG", "LOOK", "LOST", "MADE",
    "MAIN", "MAKE", "MANY", "MARK", "MOST", "MOVE", "MUCH", "MUST",
    "NAME", "NEAR", "NEED", "NEXT", "NOTE", "ONCE", "ONLY", "OPEN",
    "OVER", "PAID", "PART", "PAST", "PATH", "PLAN", "PLUS", "POST",
    "PULL", "PUSH", "RATE", "REAL", "REST", "RISE", "RISK", "ROLE",
    "RULE", "RUNS", "SAID", "SALE", "SAME", "SAVE", "SELF", "SELL",
    "SENT", "SHOW", "SIDE", "SIGN", "SIZE", "SOME", "SORT", "STEP",
    "STOP", "SURE", "TAKE", "TALK", "TEAM", "TELL", "TERM", "TEST",
    "THAN", "THAT", "THEM", "THEN", "THEY", "THIS", "THUS", "TIME",
    "TOLD", "TOOK", "TURN", "TYPE", "UNIT", "UPON", "USED", "VERY",
    "VIEW", "VOTE", "WAIT", "WALK", "WALL", "WANT", "WARS", "WEEK",
    "WELL", "WENT", "WERE", "WHAT", "WHEN", "WIDE", "WILL", "WITH",
    "WORD", "WORK", "YEAR", "YOUR", "ZERO", "FREE", "CASH", "DEBT",
    "DRUG", "FUND", "GROW", "JOBS", "LOAN", "LOSS", "MISS", "PAID",
    "ROSE", "SEES", "WINS",
})


def invalidate_cache() -> None:
    """Force reload on next lookup (for testing)."""
    global _name_to_ticker, _ticker_to_name
    with _lock:
        _name_to_ticker = None
        _ticker_to_name = None
