"""SQLite storage backend for per-service trading state and analytics.

All services (US paper-trade, UK paper-trade, T212) share ONE database file
(`switching.db`) but every row is tagged with a ``service`` column
(``us`` / ``uk`` / ``t212`` / ...). This:

  * Stops the old bug where US and UK wrote the same JSON files and clobbered
    each other (they now write disjoint rows in the same DB).
  * Lets T212 collect the same analytics (exit tracking, skipped signals,
    trade memory) as the paper traders.
  * Keeps everything queryable together for cross-service comparison.

Concurrency: WAL mode + a busy timeout let the three service processes and the
dashboard read/write the same file safely. Writes are wrapped in transactions
so a reader (dashboard) never sees a half-written state — fixing the
non-atomic ``write_text`` corruption risk of the JSON era.

Design: the queryable entities (closed_trades, positions, exit_tracks,
skipped_signals) are relational tables. Portfolio scalars and small bounded
collections (seen_signals, last_signals, cached_prices, recently_sold,
trade_memory) live as JSON values in a ``service_state`` key/value table.
Per-row nested data (OHLC snapshots) rides along as a JSON column.

Backward compatibility: the first time a service loads and finds no rows, it
imports the legacy JSON file sitting next to it (auto-migration), so the
switch-over loses no history and needs no manual step. The shared legacy
trackers (exit_tracker.json / skipped_signals.json / trade_memory.json) are
imported only for the ``us`` service, since that is whose data they held.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DB_FILENAME = "switching.db"

# Map a portfolio state-file stem to a service id.
_SERVICE_BY_STEM: dict[str, str] = {
    "paper_portfolio": "us",
    "uk_portfolio": "uk",
    "t212_portfolio": "t212",
    "alpaca_state": "alpaca",
}

# Per-connection cache keyed by resolved db path (one connection per process per db).
_CONNECTION_LOCK = threading.Lock()
_CONNECTIONS: dict[str, sqlite3.Connection] = {}


# ---------------------------------------------------------------------------
# Table metadata — columns, which are JSON-encoded, which are booleans
# ---------------------------------------------------------------------------

_POSITION_COLS = [
    "ticker", "detector", "entry_price", "shares", "entry_dt", "headline",
    "severity", "stop_loss", "hold_days", "days_held", "first_green",
    "first_green_pct", "peak_price", "peak_tracking", "snapshots",
]
_CLOSED_TRADE_COLS = [
    "ticker", "detector", "entry_price", "exit_price", "shares", "entry_dt",
    "exit_dt", "pnl", "pct_return", "exit_reason", "headline", "peak_price",
    "severity",
]
_EXIT_TRACK_COLS = [
    "ticker", "detector", "entry_price", "exit_price", "exit_dt", "exit_reason",
    "pct_return", "headline", "peak_price", "tracking_complete", "snapshots",
]
_SKIPPED_COLS = [
    "ticker", "detector", "severity", "headline", "skip_reason", "skipped_at",
    "would_be_entry_price", "hold_days", "first_green", "first_green_pct",
    "stop_loss_pct", "snapshots", "tracking_complete", "simulated_exit_price",
    "simulated_exit_reason", "simulated_pct_return", "simulated_exit_dt",
]

# Columns holding JSON-serialised structures.
_JSON_COLS: dict[str, set[str]] = {
    "positions": {"snapshots"},
    "exit_tracks": {"snapshots"},
    "skipped_signals": {"snapshots"},
    "closed_trades": set(),
}
# Columns holding booleans (stored as INTEGER 0/1, restored to bool on read).
_BOOL_COLS: dict[str, set[str]] = {
    "positions": {"first_green", "peak_tracking"},
    "exit_tracks": {"tracking_complete"},
    "skipped_signals": {"first_green", "tracking_complete"},
    "closed_trades": set(),
}
_TABLE_COLS = {
    "positions": _POSITION_COLS,
    "closed_trades": _CLOSED_TRADE_COLS,
    "exit_tracks": _EXIT_TRACK_COLS,
    "skipped_signals": _SKIPPED_COLS,
}


# ---------------------------------------------------------------------------
# Path / service resolution
# ---------------------------------------------------------------------------

def service_from_path(path: Path) -> str:
    """Map a portfolio state-file path to its service id (us/uk/t212/...)."""
    return _SERVICE_BY_STEM.get(Path(path).stem, Path(path).stem)


def db_path_for(path: Path) -> Path:
    """Resolve the shared SQLite db path that sits next to *path*.

    If *path* already points at a .db file, use it directly (handy for tests).
    """
    p = Path(path)
    if p.suffix == ".db":
        return p
    return p.parent / DB_FILENAME


# ---------------------------------------------------------------------------
# Connection + schema
# ---------------------------------------------------------------------------

def connect(path: Path) -> sqlite3.Connection:
    """Return a WAL-mode connection to the shared db for *path*, initialising
    the schema on first use. Connections are cached per process per db file."""
    db_path = db_path_for(path)
    key = str(db_path.resolve())
    with _CONNECTION_LOCK:
        conn = _CONNECTIONS.get(key)
        if conn is not None:
            return conn
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        _init_schema(conn)
        _CONNECTIONS[key] = conn
        return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS service_state (
            service    TEXT NOT NULL,
            key        TEXT NOT NULL,
            value      TEXT,
            updated_at TEXT,
            PRIMARY KEY (service, key)
        );

        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service TEXT NOT NULL,
            ticker TEXT, detector TEXT, entry_price REAL, shares REAL,
            entry_dt TEXT, headline TEXT, severity REAL, stop_loss REAL,
            hold_days INTEGER, days_held INTEGER, first_green INTEGER,
            first_green_pct REAL, peak_price REAL, peak_tracking INTEGER,
            snapshots TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_positions_service ON positions(service);

        CREATE TABLE IF NOT EXISTS closed_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service TEXT NOT NULL,
            ticker TEXT, detector TEXT, entry_price REAL, exit_price REAL,
            shares REAL, entry_dt TEXT, exit_dt TEXT, pnl REAL, pct_return REAL,
            exit_reason TEXT, headline TEXT, peak_price REAL, severity REAL
        );
        CREATE INDEX IF NOT EXISTS ix_trades_service ON closed_trades(service);
        CREATE INDEX IF NOT EXISTS ix_trades_service_detector ON closed_trades(service, detector);
        CREATE INDEX IF NOT EXISTS ix_trades_service_exitdt ON closed_trades(service, exit_dt);

        CREATE TABLE IF NOT EXISTS exit_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service TEXT NOT NULL,
            ticker TEXT, detector TEXT, entry_price REAL, exit_price REAL,
            exit_dt TEXT, exit_reason TEXT, pct_return REAL, headline TEXT,
            peak_price REAL, tracking_complete INTEGER, snapshots TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_exit_service ON exit_tracks(service);

        CREATE TABLE IF NOT EXISTS skipped_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service TEXT NOT NULL,
            ticker TEXT, detector TEXT, severity REAL, headline TEXT,
            skip_reason TEXT, skipped_at TEXT, would_be_entry_price REAL,
            hold_days INTEGER, first_green INTEGER, first_green_pct REAL,
            stop_loss_pct REAL, snapshots TEXT, tracking_complete INTEGER,
            simulated_exit_price REAL, simulated_exit_reason TEXT,
            simulated_pct_return REAL, simulated_exit_dt TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_skipped_service ON skipped_signals(service);
        """
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Row <-> dict mapping helpers
# ---------------------------------------------------------------------------

def _to_row(table: str, item: dict) -> list[Any]:
    json_cols = _JSON_COLS[table]
    bool_cols = _BOOL_COLS[table]
    row: list[Any] = []
    for col in _TABLE_COLS[table]:
        v = item.get(col)
        if col in json_cols:
            v = json.dumps(v if v is not None else [])
        elif col in bool_cols:
            v = 1 if v else 0
        row.append(v)
    return row


def _from_row(table: str, row: sqlite3.Row) -> dict:
    json_cols = _JSON_COLS[table]
    bool_cols = _BOOL_COLS[table]
    out: dict[str, Any] = {}
    for col in _TABLE_COLS[table]:
        v = row[col]
        if col in json_cols:
            try:
                v = json.loads(v) if v else []
            except (TypeError, json.JSONDecodeError):
                v = []
        elif col in bool_cols:
            v = bool(v)
        out[col] = v
    return out


def _replace_rows(conn: sqlite3.Connection, table: str, service: str, items: list[dict]) -> None:
    """Atomically replace all rows for *service* in *table*."""
    cols = _TABLE_COLS[table]
    placeholders = ", ".join(["?"] * (len(cols) + 1))  # +1 for service
    sql = f"INSERT INTO {table} (service, {', '.join(cols)}) VALUES ({placeholders})"
    conn.execute(f"DELETE FROM {table} WHERE service = ?", (service,))
    conn.executemany(sql, [[service, *_to_row(table, it)] for it in items])


def _fetch_rows(conn: sqlite3.Connection, table: str, service: str, order: str = "id") -> list[dict]:
    cur = conn.execute(f"SELECT * FROM {table} WHERE service = ? ORDER BY {order}", (service,))
    return [_from_row(table, r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# service_state key/value helpers
# ---------------------------------------------------------------------------

def _get_state(conn: sqlite3.Connection, service: str, key: str, default: Any = None) -> Any:
    cur = conn.execute(
        "SELECT value FROM service_state WHERE service = ? AND key = ?", (service, key)
    )
    row = cur.fetchone()
    if row is None or row["value"] is None:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, json.JSONDecodeError):
        return default


def _set_state(conn: sqlite3.Connection, service: str, key: str, value: Any) -> None:
    from datetime import datetime, timezone
    conn.execute(
        "INSERT INTO service_state (service, key, value, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(service, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (service, key, json.dumps(value), datetime.now(tz=timezone.utc).isoformat()),
    )


def _service_initialised(conn: sqlite3.Connection, service: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM service_state WHERE service = ? LIMIT 1", (service,)
    )
    return cur.fetchone() is not None


# Portfolio scalar fields stored individually in service_state.
_PORTFOLIO_SCALARS = (
    "cash", "last_scan_dt", "max_position_pct", "max_positions",
    "last_review_sent_dt", "last_weekly_report_dt",
)
_PORTFOLIO_BLOBS = ("seen_signals", "last_signals", "cached_prices", "recently_sold")


# ---------------------------------------------------------------------------
# Portfolio state
# ---------------------------------------------------------------------------

def load_portfolio_state(path: Path) -> dict | None:
    """Return the portfolio state dict for the service at *path*, or None if no
    data exists anywhere (so the caller can fall back to defaults + seeding)."""
    conn = connect(path)
    service = service_from_path(path)

    if not _service_initialised(conn, service):
        legacy = Path(path)
        if legacy.exists():
            _import_legacy_portfolio(conn, service, legacy)
        else:
            return None

    state: dict[str, Any] = {
        "cash": _get_state(conn, service, "cash", 1000.0),
        "last_scan_dt": _get_state(conn, service, "last_scan_dt", ""),
        "max_position_pct": _get_state(conn, service, "max_position_pct", 0.20),
        "max_positions": _get_state(conn, service, "max_positions", 5),
        "last_review_sent_dt": _get_state(conn, service, "last_review_sent_dt", ""),
        "last_weekly_report_dt": _get_state(conn, service, "last_weekly_report_dt", ""),
        "seen_signals": _get_state(conn, service, "seen_signals", []),
        "last_signals": _get_state(conn, service, "last_signals", []),
        "cached_prices": _get_state(conn, service, "cached_prices", {}),
        "recently_sold": _get_state(conn, service, "recently_sold", {}),
        "positions": _fetch_rows(conn, "positions", service),
        "trades": _fetch_rows(conn, "closed_trades", service),
    }
    return state


def save_portfolio_state(path: Path, state: dict) -> None:
    """Persist a portfolio state dict for the service at *path*.

    closed_trades are append-only (only rows beyond what's already stored are
    inserted); positions are replaced wholesale; scalars/blobs upserted.
    """
    conn = connect(path)
    service = service_from_path(path)
    try:
        for k in _PORTFOLIO_SCALARS:
            if k in state:
                _set_state(conn, service, k, state[k])
        for k in _PORTFOLIO_BLOBS:
            if k in state:
                _set_state(conn, service, k, state[k])

        _replace_rows(conn, "positions", service, state.get("positions", []))

        # Append-only closed_trades: insert only the new tail.
        trades = state.get("trades", [])
        cur = conn.execute("SELECT COUNT(*) AS n FROM closed_trades WHERE service = ?", (service,))
        already = cur.fetchone()["n"]
        new_trades = trades[already:]
        if new_trades:
            cols = _TABLE_COLS["closed_trades"]
            placeholders = ", ".join(["?"] * (len(cols) + 1))
            sql = f"INSERT INTO closed_trades (service, {', '.join(cols)}) VALUES ({placeholders})"
            conn.executemany(sql, [[service, *_to_row("closed_trades", t)] for t in new_trades])
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _import_legacy_portfolio(conn: sqlite3.Connection, service: str, legacy: Path) -> None:
    """One-shot import of a legacy portfolio JSON into the DB for *service*."""
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    log.info("storage: importing legacy portfolio %s -> service=%s", legacy, service)
    for k in _PORTFOLIO_SCALARS:
        if k in data:
            _set_state(conn, service, k, data[k])
    _set_state(conn, service, "seen_signals", data.get("seen_signals", []))
    _set_state(conn, service, "last_signals", data.get("last_signals", []))
    _set_state(conn, service, "cached_prices", data.get("cached_prices", {}))
    _set_state(conn, service, "recently_sold", data.get("recently_sold", {}))
    _replace_rows(conn, "positions", service, data.get("positions", []))
    _replace_rows(conn, "closed_trades", service, data.get("trades", []))
    conn.commit()


# ---------------------------------------------------------------------------
# Trackers (exit / skipped) — kind selects the table + legacy filename
# ---------------------------------------------------------------------------

_TRACKER_TABLE = {"exit": "exit_tracks", "skipped": "skipped_signals"}
_TRACKER_LEGACY = {"exit": "exit_tracker.json", "skipped": "skipped_signals.json"}
_TRACKER_JSON_KEY = {"exit": "tracked", "skipped": "skipped"}


def load_tracker(path: Path, service: str, kind: str) -> list[dict]:
    """Return the list of tracked-item dicts for *service*, auto-importing the
    legacy JSON on first use (only for the ``us`` service, which owned the
    shared trackers)."""
    conn = connect(path)
    table = _TRACKER_TABLE[kind]
    legacy_key = f"_migrated_{table}"
    if not _get_state(conn, service, legacy_key, False):
        # Auto-import only the original owner's shared file.
        if service == "us":
            legacy = Path(path).parent / _TRACKER_LEGACY[kind]
            if legacy.exists():
                _import_legacy_tracker(conn, service, legacy, kind)
        _set_state(conn, service, legacy_key, True)
        conn.commit()
    return _fetch_rows(conn, table, service)


def save_tracker(path: Path, service: str, kind: str, items: list[dict]) -> None:
    conn = connect(path)
    table = _TRACKER_TABLE[kind]
    try:
        _replace_rows(conn, table, service, items)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _import_legacy_tracker(conn: sqlite3.Connection, service: str, legacy: Path, kind: str) -> None:
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    items = data.get(_TRACKER_JSON_KEY[kind], [])
    log.info("storage: importing legacy %s (%d items) -> service=%s", legacy, len(items), service)
    _replace_rows(conn, _TRACKER_TABLE[kind], service, items)


# ---------------------------------------------------------------------------
# Trade memory (derived blob)
# ---------------------------------------------------------------------------

def load_trade_memory(path: Path, service: str) -> dict:
    conn = connect(path)
    if not _get_state(conn, service, "_migrated_trade_memory", False):
        if service == "us":
            legacy = Path(path).parent / "trade_memory.json"
            if legacy.exists():
                try:
                    mem = json.loads(legacy.read_text(encoding="utf-8"))
                    _set_state(conn, service, "trade_memory", mem)
                except (OSError, json.JSONDecodeError):
                    pass
        _set_state(conn, service, "_migrated_trade_memory", True)
        conn.commit()
    return _get_state(conn, service, "trade_memory", {})


def save_trade_memory(path: Path, service: str, memory: dict) -> None:
    conn = connect(path)
    _set_state(conn, service, "trade_memory", memory)
    conn.commit()


# ---------------------------------------------------------------------------
# Test / maintenance helper
# ---------------------------------------------------------------------------

def _reset_connection_cache() -> None:
    """Close and forget all cached connections (used by tests)."""
    with _CONNECTION_LOCK:
        for c in _CONNECTIONS.values():
            try:
                c.close()
            except Exception:
                pass
        _CONNECTIONS.clear()
