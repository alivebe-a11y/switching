"""Detection funnel — capture headlines a detector classified but then dropped.

When a detector's ``classify()`` matches a headline (a real catalyst) but
``extract_ticker()`` returns nothing, the signal is silently binned — we never
trade it and never see what we missed. Common causes: short/brand company names
the SEC lookup can't resolve, missing EPIC on a UK item, ticker only in the body.

This module records those drops so the loss becomes visible. It's deliberately
the SINGLE chokepoint at the drop point in every detector, so a future LLM
ticker-resolver (a local GPU model via Ollama/llama.cpp, or Haiku) can plug in
right here: on a drop, hand the headline to the model, validate the proposed
ticker against the SEC/known list, and recover the trade.

Per-process: call ``configure(service, cache_path)`` once at loop start. Until
then ``record_drop()`` is a no-op, so tests and backtests never write.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_MAX_PER_SERVICE = 1000     # keep the most recent N drops per service
_PRUNE_EVERY = 100          # prune cadence (inserts) to avoid per-write cost

_service = "us"
_db_path: Path | None = None
_insert_count = 0


def configure(service: str, cache_path) -> None:
    """Enable drop capture for this process, tagging rows with *service* and
    writing to the shared switching.db next to *cache_path*."""
    global _service, _db_path
    from switching import storage
    _service = service
    _db_path = storage.db_path_for(Path(cache_path))
    _ensure_table()
    _prune()


def _ensure_table() -> None:
    if _db_path is None:
        return
    from switching import storage
    conn = storage.connect(_db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS dropped_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service TEXT NOT NULL,
            detector TEXT,
            reason TEXT,
            ts TEXT,
            headline TEXT,
            url TEXT,
            summary TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_drops_service ON dropped_signals(service);
        """
    )
    conn.commit()


def _prune() -> None:
    if _db_path is None:
        return
    from switching import storage
    conn = storage.connect(_db_path)
    conn.execute(
        "DELETE FROM dropped_signals WHERE service = ? AND id NOT IN "
        "(SELECT id FROM dropped_signals WHERE service = ? ORDER BY id DESC LIMIT ?)",
        (_service, _service, _MAX_PER_SERVICE),
    )
    conn.commit()


def record_drop(detector: str, item, reason: str = "no_ticker") -> None:
    """Record a classified-but-dropped headline. No-op until configure()d."""
    global _insert_count
    if _db_path is None:
        return
    try:
        from switching import storage
        conn = storage.connect(_db_path)
        conn.execute(
            "INSERT INTO dropped_signals (service, detector, reason, ts, headline, url, summary) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                _service, detector, reason,
                datetime.now(tz=timezone.utc).isoformat(),
                getattr(item, "title", "") or "",
                getattr(item, "url", "") or "",
                (getattr(item, "summary", "") or "")[:500],
            ),
        )
        conn.commit()
        _insert_count += 1
        if _insert_count % _PRUNE_EVERY == 0:
            _prune()
    except Exception as exc:  # never let capture break a scan
        log.warning("detection_funnel.record_drop failed: %s", exc)


def record_signal_drop(detector: str, signal, reason: str) -> None:
    """Record a signal that survived classify+ticker but couldn't be acted on
    (yfinance has no price, broker rejected the order, etc.). Same table as
    ``record_drop``; the ``reason`` distinguishes them. The headline is
    prefixed with the ticker so the dashboard shows what we tried to buy.
    """
    global _insert_count
    if _db_path is None:
        return
    try:
        from switching import storage
        conn = storage.connect(_db_path)
        ticker = getattr(signal, "ticker", "") or "?"
        headline = getattr(signal, "headline", "") or ""
        conn.execute(
            "INSERT INTO dropped_signals (service, detector, reason, ts, headline, url, summary) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                _service, detector, reason,
                datetime.now(tz=timezone.utc).isoformat(),
                f"[{ticker}] {headline}",
                getattr(signal, "url", "") or "",
                "",
            ),
        )
        conn.commit()
        _insert_count += 1
        if _insert_count % _PRUNE_EVERY == 0:
            _prune()
    except Exception as exc:  # never let capture break the loop
        log.warning("detection_funnel.record_signal_drop failed: %s", exc)


def load_drops(cache_path, limit: int = 200) -> list[dict]:
    """Most-recent dropped headlines across all services (newest first)."""
    from switching import storage
    conn = storage.connect(storage.db_path_for(Path(cache_path)))
    try:
        cur = conn.execute(
            "SELECT service, detector, reason, ts, headline, url "
            "FROM dropped_signals ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


def drop_summary(cache_path) -> list[dict]:
    """Drop counts grouped by service + detector (highest first)."""
    from switching import storage
    conn = storage.connect(storage.db_path_for(Path(cache_path)))
    try:
        cur = conn.execute(
            "SELECT service, detector, COUNT(*) AS n FROM dropped_signals "
            "GROUP BY service, detector ORDER BY n DESC"
        )
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


def _reset() -> None:
    """Test helper — disable capture and reset counters."""
    global _service, _db_path, _insert_count
    _service = "us"
    _db_path = None
    _insert_count = 0
