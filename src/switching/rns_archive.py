"""RNS stream archive — capture the FULL Investegate RNS feed, not just what a
detector classifies.

The UK detectors act on a handful of RNS categories (director dealings, a few
corporate actions). Everything else Investegate returns — results, trading
updates, guidance, holdings, placings, board changes — is fetched and then
discarded. That blindness is why we can't yet ask "does a director-buy that
coincides with a *supportive* RNS category outperform a lone director-buy?"
(measured 2026-07: 497 of 498 UK director-buys had NO other catalyst we detect).

This module archives every announcement the Investegate scrape returns, tagged
by EPIC + a coarse RNS category + timestamp, so that co-occurrence question
becomes answerable from data instead of a guess. LOG-ONLY: zero trading impact,
the UK analogue of the US movers `no_detector` headline capture.

Per-process: call ``configure(cache_path)`` once at UK loop start. Until then
``record()`` is a no-op, so US runs / tests / backtests never write. Recording
is deduped by announcement URL (unique per RNS item) so re-scrapes don't inflate.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_MAX_ROWS = 20000          # bound the table (RNS is high-volume) — keep newest N
_PRUNE_EVERY = 200         # prune cadence (inserts)

_db_path: Path | None = None
_insert_count = 0

# Per-process URL dedup — the Investegate scrape is TTL-cached and re-read across
# a scan cycle; without this the same announcement re-inserts repeatedly.
_MAX_SEEN_IN_MEMORY = 20000
_seen_urls: set[str] = set()

# Trailing "(EPIC)" the Investegate parser appends to each headline.
_EPIC_SUFFIX_RX = re.compile(r"\s*\(([A-Z][A-Z0-9]{1,4})\)\s*$")

# Coarse RNS category from the announcement title. RNS titles are semi-standard,
# so keyword matching is reliable. First match wins (order matters). Pure.
_CATEGORY_PATTERNS: list[tuple[str, str]] = [
    ("director_dealing",  r"director/pdmr|pdmr\b|director shareholding|directorate"),
    ("own_shares",        r"own shares|share buyback|buy-?back|repurchase"),
    ("holdings",          r"holding\(s\) in company|holdings in company|tr-1|major (share)?holding|notification of"),
    ("results",           r"final results|interim results|half-?year|annual results|results for|q[1-4] results|full year results"),
    ("trading_update",    r"trading update|trading statement"),
    ("guidance",          r"guidance|outlook|profit warning|profit warn|ahead of|in line with expectations|below expectations"),
    ("dividend",          r"dividend"),
    ("mna",               r"\boffer\b|acquisition|merger|recommended cash|scheme of arrangement|possible offer|takeover|disposal"),
    ("contract",          r"contract|\baward\b|new order|partnership|collaboration|agreement"),
    ("capital_raise",     r"placing|subscription|fundrais|open offer|capital raise|convertible|retail offer"),
    ("board_change",      r"board change|appointment|resignation|steps down|chief executive|\bceo\b|\bcfo\b|chair"),
    ("admission",         r"admission|first day of dealing|ipo|listing"),
    ("agm",               r"\bagm\b|annual general meeting|result of meeting|general meeting"),
]
_COMPILED = [(name, re.compile(pat, re.I)) for name, pat in _CATEGORY_PATTERNS]


def rns_category(title: str) -> str:
    """Coarse RNS category for an announcement title. Pure. 'other' if none match."""
    t = title or ""
    for name, rx in _COMPILED:
        if rx.search(t):
            return name
    return "other"


def _clean(title: str) -> tuple[str, str]:
    """Split the parser's 'Headline (EPIC)' back into (headline, epic)."""
    m = _EPIC_SUFFIX_RX.search(title or "")
    epic = m.group(1) if m else ""
    headline = _EPIC_SUFFIX_RX.sub("", title or "").strip()
    return headline, epic


def configure(cache_path) -> None:
    """Enable RNS archiving for this process (call once at UK loop start)."""
    global _db_path
    from switching import storage
    _db_path = storage.db_path_for(Path(cache_path))
    _ensure_table()
    _prune()
    _seen_urls.clear()


def _ensure_table() -> None:
    if _db_path is None:
        return
    from switching import storage
    conn = storage.connect(_db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS rns_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            epic TEXT,
            category TEXT,
            headline TEXT,
            url TEXT,
            published TEXT,
            recorded_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_rns_epic ON rns_archive(epic);
        CREATE INDEX IF NOT EXISTS ix_rns_category ON rns_archive(category);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_rns_url ON rns_archive(url);
        """
    )
    conn.commit()


def _prune() -> None:
    if _db_path is None:
        return
    from switching import storage
    conn = storage.connect(_db_path)
    conn.execute(
        "DELETE FROM rns_archive WHERE id NOT IN "
        "(SELECT id FROM rns_archive ORDER BY id DESC LIMIT ?)",
        (_MAX_ROWS,),
    )
    conn.commit()


def record(items) -> None:
    """Archive a batch of Investegate FeedItems. No-op until configure()d.

    Deduped by URL (unique per announcement) — the ON CONFLICT keeps the table
    idempotent even across restarts, and the in-memory set spares the DB the
    repeat writes within a process. Never raises (capture must not break a scan).
    """
    global _insert_count
    if _db_path is None or not items:
        return
    try:
        from switching import storage
        conn = storage.connect(_db_path)
        now = datetime.now(tz=timezone.utc).isoformat()
        for it in items:
            url = getattr(it, "url", "") or ""
            if not url or url in _seen_urls:
                continue
            if len(_seen_urls) >= _MAX_SEEN_IN_MEMORY:
                _seen_urls.clear()
            _seen_urls.add(url)
            title = getattr(it, "title", "") or ""
            headline, epic = _clean(title)
            pub = getattr(it, "published", None)
            pub_s = pub.isoformat() if hasattr(pub, "isoformat") else (str(pub) if pub else "")
            conn.execute(
                "INSERT OR IGNORE INTO rns_archive "
                "(epic, category, headline, url, published, recorded_at) VALUES (?,?,?,?,?,?)",
                (epic, rns_category(headline), headline, url, pub_s, now),
            )
            _insert_count += 1
        conn.commit()
        if _insert_count % _PRUNE_EVERY < len(items):
            _prune()
    except Exception as exc:  # never let capture break a scan
        log.warning("rns_archive.record failed: %s", exc)


def category_summary(cache_path) -> list[dict]:
    """Archived RNS counts by category (highest first)."""
    from switching import storage
    conn = storage.connect(storage.db_path_for(Path(cache_path)))
    try:
        cur = conn.execute(
            "SELECT category, COUNT(*) AS n FROM rns_archive GROUP BY category ORDER BY n DESC"
        )
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


def _reset() -> None:
    """Test helper — disable capture and reset counters."""
    global _db_path, _insert_count
    _db_path = None
    _insert_count = 0
    _seen_urls.clear()
