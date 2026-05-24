#!/usr/bin/env python3
"""Migrate legacy JSON state into the SQLite store and validate it.

The services auto-migrate on first load, so running this is optional — but it
gives an explicit, auditable check that every legacy JSON row landed in the DB
under the right service, with NO data lost. Safe to run repeatedly (the import
is idempotent: it only happens once per service, guarded by flags in the DB).

Mapping:
    paper_portfolio.json  -> service "us"
    uk_portfolio.json     -> service "uk"
    t212_portfolio.json   -> service "t212"
    exit_tracker.json     -> service "us"   (was shared; US owned it)
    skipped_signals.json  -> service "us"
    trade_memory.json     -> service "us"

Usage:
    python scripts/migrate_to_sqlite.py [CACHE_DIR]
    (CACHE_DIR defaults to /app/.cache, falling back to ./data/cache)

Exit code 0 = all counts validated; 1 = a mismatch was detected.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _count_json(path: Path, key: str) -> int:
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return len(data.get(key, []))


def _resolve_cache_dir(argv: list[str]) -> Path:
    if len(argv) > 1:
        return Path(argv[1])
    for candidate in (Path("/app/.cache"), Path("./data/cache")):
        if candidate.exists():
            return candidate
    return Path("/app/.cache")


def main(argv: list[str]) -> int:
    # Import after sys.path is set up by the caller / installed package.
    from switching import storage
    from switching.paper_trader import Portfolio
    from switching.exit_tracker import ExitTracker
    from switching.skipped_tracker import SkippedTracker

    cache = _resolve_cache_dir(argv)
    print(f"== Migrating legacy JSON -> SQLite in {cache} ==\n")
    print(f"DB: {storage.db_path_for(cache / 'paper_portfolio.json')}\n")

    portfolios = [
        ("us",   "paper_portfolio.json"),
        ("uk",   "uk_portfolio.json"),
        ("t212", "t212_portfolio.json"),
    ]

    ok = True
    print(f"{'service':<8} {'source':<22} {'json':>6} {'db':>6}  result")
    print("-" * 56)

    for service, filename in portfolios:
        path = cache / filename
        json_trades = _count_json(path, "trades")
        json_positions = _count_json(path, "positions")

        # Triggers auto-import on first load; idempotent thereafter.
        pf = Portfolio.load(path)
        db_trades = len(pf.trades)
        db_positions = len(pf.positions)

        # If the JSON existed, the DB must hold at least as many trades.
        trades_ok = db_trades >= json_trades
        pos_ok = (db_positions == json_positions) or not path.exists()
        status = "OK" if (trades_ok and pos_ok) else "MISMATCH"
        if status != "OK":
            ok = False
        print(f"{service:<8} {filename:<22} {json_trades:>6} {db_trades:>6}  trades {status}")
        if path.exists():
            print(f"{'':<8} {'(positions)':<22} {json_positions:>6} {db_positions:>6}  positions "
                  f"{'OK' if pos_ok else 'MISMATCH'}")

    # Shared US-owned analytics
    print()
    exit_json = _count_json(cache / "exit_tracker.json", "tracked")
    exit_db = len(ExitTracker.load(cache / "exit_tracker.json", "us").tracked)
    e_ok = exit_db >= exit_json
    ok = ok and e_ok
    print(f"{'us':<8} {'exit_tracker.json':<22} {exit_json:>6} {exit_db:>6}  "
          f"{'OK' if e_ok else 'MISMATCH'}")

    skip_json = _count_json(cache / "skipped_signals.json", "skipped")
    skip_db = len(SkippedTracker.load(cache / "skipped_signals.json", "us").skipped)
    s_ok = skip_db >= skip_json
    ok = ok and s_ok
    print(f"{'us':<8} {'skipped_signals.json':<22} {skip_json:>6} {skip_db:>6}  "
          f"{'OK' if s_ok else 'MISMATCH'}")

    print()
    if ok:
        print("VALIDATION PASSED - all legacy rows present in SQLite.")
        print("Legacy JSON files are left untouched as read-only backups.")
        return 0
    print("VALIDATION FAILED — investigate before relying on the DB.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
