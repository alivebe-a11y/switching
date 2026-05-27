"""Generate tests/fixtures/switching_v1.db — a frozen-schema golden snapshot.

The fixture is loaded by tests/test_storage_compat.py via the public Portfolio
/ ExitTracker / SkippedTracker / load_trade_memory APIs.  If a future code
change breaks backwards compatibility with old DBs, the compat tests fail
in CI BEFORE the change merges — so the 188 trades of history we've already
collected stay safe across refactors.

Re-run this script ONLY when a schema change is deliberate.  Commit the
regenerated fixture in the same PR as the schema change, and update the
fixture version in tests/test_storage_compat.py.

The fixture is deterministic — every run produces a byte-identical file
given the same code, so git diffs stay small.

    python scripts/build_test_fixture.py
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make `switching` importable when run from repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from switching import storage, detection_funnel    # noqa: E402

FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "switching_v1.db"

# Fixed timestamps so the generated fixture is byte-deterministic.
T0 = "2026-01-05T14:30:00+00:00"
T1 = "2026-01-06T14:30:00+00:00"
T2 = "2026-01-07T14:30:00+00:00"
T3 = "2026-01-08T14:30:00+00:00"


def _us_portfolio_state() -> dict:
    return {
        "cash": 18_432.55,
        "last_scan_dt": T2,
        "max_position_pct": 0.015,
        "max_positions": 50,
        "last_review_sent_dt": T1,
        "last_weekly_report_dt": "",
        "seen_signals": [
            "guidance_raise:ACME:https://example.com/acme",
            "mna_target:RNDX:https://example.com/rndx",
            "buyback:FOO:https://example.com/foo",
        ],
        "last_signals": [],
        "cached_prices": {"ACME": 42.1, "RNDX": 17.8},
        "recently_sold": {"OLD": T0},
        "pending_orders": {
            "guidance_raise:LATE:https://example.com/late": {
                "detector": "guidance_raise",
                "ticker": "LATE",
                "company": "Late Corp",
                "event_dt": T2,
                "headline": "LATE raises FY guidance",
                "url": "https://example.com/late",
                "evidence": "raises full-year guidance",
                "severity": 0.8,
                "price_reaction": None,
                "extra": {},
                "queued_at": T2,
            },
        },
        "positions": [
            {
                "ticker": "ACME", "detector": "guidance_raise",
                "entry_price": 40.0, "shares": 5.0, "entry_dt": T1,
                "headline": "ACME raises FY guidance",
                "severity": 0.85, "stop_loss": 0.026, "hold_days": 5,
                "days_held": 1, "first_green": True, "first_green_pct": 0.05,
                "peak_price": 42.5, "peak_tracking": False,
                "snapshots": [{"date": "2026-01-06", "day": 1, "open": 40.1,
                               "high": 42.5, "low": 39.8, "close": 42.1,
                               "pct_from_entry": 0.0525, "high_pct": 0.0625,
                               "low_pct": -0.005}],
            },
            {
                "ticker": "RNDX", "detector": "mna_target",
                "entry_price": 16.5, "shares": 18.0, "entry_dt": T2,
                "headline": "RNDX announces strategic review",
                "severity": 0.7, "stop_loss": 0.036, "hold_days": 8,
                "days_held": 0, "first_green": True, "first_green_pct": 0.0,
                "peak_price": 17.8, "peak_tracking": True,
                "snapshots": [],
            },
        ],
        "trades": [
            {
                "ticker": "WINNER", "detector": "guidance_raise",
                "entry_price": 100.0, "exit_price": 107.5, "shares": 2.0,
                "entry_dt": T0, "exit_dt": T1, "pnl": 15.0, "pct_return": 0.075,
                "exit_reason": "first_green", "headline": "WINNER raises FY",
                "peak_price": 108.0, "severity": 0.88,
            },
            {
                "ticker": "LOSER", "detector": "mna_target",
                "entry_price": 50.0, "exit_price": 48.7, "shares": 4.0,
                "entry_dt": T0, "exit_dt": T1, "pnl": -5.2, "pct_return": -0.026,
                "exit_reason": "stop_loss", "headline": "LOSER acquisition rumor",
                "peak_price": 50.5, "severity": 0.6,
            },
            {
                "ticker": "GHOST", "detector": "mna_target",
                "entry_price": 80.0, "exit_price": 80.0, "shares": 2.5,
                "entry_dt": T0, "exit_dt": T2, "pnl": 0.0, "pct_return": 0.0,
                "exit_reason": "corporate_action", "headline": "GHOST to be acquired",
                "peak_price": 82.0, "severity": 0.75,
            },
        ],
    }


def _uk_portfolio_state() -> dict:
    return {
        "cash": 19_876.10,
        "last_scan_dt": T2,
        "max_position_pct": 0.015,
        "max_positions": 50,
        "last_review_sent_dt": "",
        "last_weekly_report_dt": "",
        "seen_signals": ["uk_director_dealing:GAMA.L:https://www.investegate.co.uk/announcement/gama-2026-01-06"],
        "last_signals": [],
        "cached_prices": {"GAMA.L": 1.85},
        "recently_sold": {},
        "pending_orders": {},
        "positions": [{
            "ticker": "GAMA.L", "detector": "uk_director_dealing",
            "entry_price": 1.78, "shares": 100.0, "entry_dt": T2,
            "headline": "GAMA Director buys 25,000 shares",
            "severity": 0.65, "stop_loss": 0.046, "hold_days": 5,
            "days_held": 0, "first_green": True, "first_green_pct": 0.0,
            "peak_price": 1.85, "peak_tracking": False,
            "snapshots": [],
        }],
        "trades": [{
            "ticker": "VOD.L", "detector": "buyback",
            "entry_price": 0.75, "exit_price": 0.79, "shares": 200.0,
            "entry_dt": T0, "exit_dt": T1, "pnl": 8.0, "pct_return": 0.053,
            "exit_reason": "first_green", "headline": "VOD buyback expanded",
            "peak_price": 0.80, "severity": 0.70,
        }],
    }


def _t212_portfolio_state() -> dict:
    return {
        "cash": 4_532.18,
        "last_scan_dt": T2,
        "max_position_pct": 0.015,
        "max_positions": 50,
        "last_review_sent_dt": "",
        "last_weekly_report_dt": "",
        "seen_signals": ["guidance_raise:NVDA:https://example.com/nvda"],
        "last_signals": [],
        "cached_prices": {"NVDA": 158.0},
        "recently_sold": {"MSFT": T1},
        "pending_orders": {},
        "positions": [{
            "ticker": "NVDA", "detector": "guidance_raise",
            "entry_price": 155.0, "shares": 0.95, "entry_dt": T2,
            "headline": "NVDA raises FY revenue outlook",
            "severity": 0.9, "stop_loss": 0.026, "hold_days": 5,
            "days_held": 0, "first_green": True, "first_green_pct": 0.05,
            "peak_price": 158.0, "peak_tracking": False,
            "snapshots": [],
        }],
        "trades": [],
    }


def _exit_tracks_us() -> list[dict]:
    return [
        {  # completed
            "ticker": "DONE", "detector": "guidance_raise",
            "entry_price": 50.0, "exit_price": 53.0, "exit_dt": T0,
            "exit_reason": "first_green", "pct_return": 0.06,
            "headline": "DONE raises FY", "peak_price": 55.0,
            "tracking_complete": True,
            "snapshots": [{"day": 1, "close": 54.0, "pct_from_exit": 0.019}],
        },
        {  # active
            "ticker": "TRACK", "detector": "mna_target",
            "entry_price": 30.0, "exit_price": 32.5, "exit_dt": T1,
            "exit_reason": "stop_loss", "pct_return": -0.026,
            "headline": "TRACK in talks", "peak_price": 33.0,
            "tracking_complete": False,
            "snapshots": [],
        },
    ]


def _skipped_us() -> list[dict]:
    return [
        {
            "ticker": "SKIP1", "detector": "guidance_raise", "severity": 0.8,
            "headline": "SKIP1 raises", "skip_reason": "max_positions",
            "skipped_at": T1, "would_be_entry_price": 60.0, "hold_days": 5,
            "first_green": True, "first_green_pct": 0.05, "stop_loss_pct": 0.026,
            "snapshots": [], "tracking_complete": False,
            "simulated_exit_price": None, "simulated_exit_reason": None,
            "simulated_pct_return": None, "simulated_exit_dt": None,
        },
        {
            "ticker": "SKIP2", "detector": "buyback", "severity": 0.65,
            "headline": "SKIP2 buyback", "skip_reason": "insufficient_cash",
            "skipped_at": T2, "would_be_entry_price": 25.0, "hold_days": 5,
            "first_green": False, "first_green_pct": 0.0, "stop_loss_pct": 0.036,
            "snapshots": [], "tracking_complete": False,
            "simulated_exit_price": None, "simulated_exit_reason": None,
            "simulated_pct_return": None, "simulated_exit_dt": None,
        },
    ]


def _trade_memory_us() -> dict:
    return {
        "guidance_raise:premium": {"trades": 12, "wins": 8, "avg_return": 0.045},
        "mna_target:standard": {"trades": 5, "wins": 2, "avg_return": 0.012},
    }


def _dropped_signals() -> list[tuple]:
    """(service, detector, reason, ts, headline, url, summary)"""
    return [
        ("us",   "mna_target",       "no_ticker",          T0, "Private equity to acquire X", "https://example.com/d1", ""),
        ("us",   "guidance_raise",   "price_unavailable",  T1, "ACME2 raises FY guidance",     "https://example.com/d2", ""),
        ("t212", "guidance_raise",   "t212_rejected: T212OrderError: instrument not found",
                                                           T2, "[RAASY] RAASY review",         "https://example.com/d3", ""),
    ]


def build_fixture(out_path: Path) -> None:
    if out_path.exists():
        out_path.unlink()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Use a brand-new connection (not the cached one) so the fixture is
    # built from a known-clean state.
    storage._reset_connection_cache()

    # Trigger schema init + populate via the PUBLIC api (mirrors what a
    # real service would write). Pointing at the .db path directly tells
    # db_path_for to use it as-is.
    storage.save_portfolio_state(out_path, _us_portfolio_state() | {"_service_override": "us"})
    # storage doesn't have a service-override param — the service is derived
    # from the file stem.  Trick: write three different stems pointing at
    # the same DB so each save goes to the right service slot.
    storage._reset_connection_cache()

    # The clean way: open the connection ourselves and call the helpers.
    conn = storage.connect(out_path)

    # Clear anything left from the throwaway save above (it landed under
    # the .db file's stem service — purge it).
    cur = conn.execute("SELECT DISTINCT service FROM service_state")
    for row in cur.fetchall():
        s = row["service"]
        if s not in {"us", "uk", "t212"}:
            conn.execute("DELETE FROM service_state WHERE service = ?", (s,))
            for tbl in ("positions", "closed_trades", "exit_tracks", "skipped_signals"):
                conn.execute(f"DELETE FROM {tbl} WHERE service = ?", (s,))
    conn.commit()

    # Per-service writes using the same path object but explicit service ids.
    for svc, state in [
        ("us",   _us_portfolio_state()),
        ("uk",   _uk_portfolio_state()),
        ("t212", _t212_portfolio_state()),
    ]:
        for k in storage._PORTFOLIO_SCALARS:
            if k in state:
                storage._set_state(conn, svc, k, state[k])
        for k in storage._PORTFOLIO_BLOBS:
            if k in state:
                storage._set_state(conn, svc, k, state[k])
        storage._replace_rows(conn, "positions", svc, state.get("positions", []))
        storage._replace_rows(conn, "closed_trades", svc, state.get("trades", []))

    # Exit-tracks + skipped (US only, mirrors how the bot uses them today)
    storage._replace_rows(conn, "exit_tracks", "us", _exit_tracks_us())
    storage._replace_rows(conn, "skipped_signals", "us", _skipped_us())

    # Trade memory (US only)
    storage._set_state(conn, "us", "trade_memory", _trade_memory_us())

    # Dropped signals: create the table via detection_funnel's DDL, then insert.
    detection_funnel.configure("us", out_path)   # creates table
    for row in _dropped_signals():
        conn.execute(
            "INSERT INTO dropped_signals (service, detector, reason, ts, headline, url, summary) "
            "VALUES (?,?,?,?,?,?,?)", row,
        )

    conn.commit()
    storage._reset_connection_cache()
    detection_funnel._reset()

    print(f"fixture written: {out_path}  ({out_path.stat().st_size} bytes)")


if __name__ == "__main__":
    build_fixture(FIXTURE_PATH)
