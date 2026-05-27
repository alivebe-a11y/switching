"""Tests for the SQLite storage backend and per-service separation.

Covers the core guarantees of the JSON->SQLite migration:
  * US / UK / T212 state is isolated (the old shared-file collision is gone).
  * closed_trades are append-only.
  * Legacy JSON auto-migrates on first load with no data loss.
  * The shared legacy trackers import only for the 'us' service.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from switching import storage
from switching.paper_trader import Portfolio, Position, ClosedTrade
from switching.exit_tracker import ExitTracker, TrackedExit
from switching.skipped_tracker import SkippedTracker
from switching import trade_memory


@pytest.fixture(autouse=True)
def _reset_conn_cache():
    """Close cached connections after each test so tmp files can be removed."""
    yield
    storage._reset_connection_cache()


def _trade(ticker="AAA", detector="earnings_surprise", pnl=1.0):
    return ClosedTrade(
        ticker=ticker, detector=detector, entry_price=50.0, exit_price=51.0,
        shares=1.0, entry_dt="2026-05-18", exit_dt="2026-05-19", pnl=pnl,
        pct_return=0.02, exit_reason="first_green", headline="h", severity=0.7,
    )


def _position(ticker="NVDA"):
    return Position(
        ticker=ticker, detector="ai_pivot", entry_price=100.0, shares=2.0,
        entry_dt="2026-05-20", headline="h", severity=0.8, stop_loss=0.026,
        hold_days=5, first_green=True,
    )


# ---------------------------------------------------------------------------
# Path / service mapping
# ---------------------------------------------------------------------------

def test_service_from_path():
    assert storage.service_from_path(Path("/c/paper_portfolio.json")) == "us"
    assert storage.service_from_path(Path("/c/uk_portfolio.json")) == "uk"
    assert storage.service_from_path(Path("/c/t212_portfolio.json")) == "t212"
    assert storage.service_from_path(Path("/c/alpaca_state.json")) == "alpaca"


def test_db_path_is_shared(tmp_path):
    us = storage.db_path_for(tmp_path / "paper_portfolio.json")
    uk = storage.db_path_for(tmp_path / "uk_portfolio.json")
    assert us == uk == tmp_path / "switching.db"


# ---------------------------------------------------------------------------
# Portfolio round-trip + isolation
# ---------------------------------------------------------------------------

def test_portfolio_roundtrip(tmp_path):
    p = Portfolio(cash=18000.0)
    p.positions.append(_position())
    p.trades.append(_trade())
    p.seen_signals.append("k1")
    p.recently_sold["TSLA"] = "2026-05-20T09:00:00+00:00"
    p.save(tmp_path / "paper_portfolio.json")

    loaded = Portfolio.load(tmp_path / "paper_portfolio.json")
    assert loaded.cash == 18000.0
    assert len(loaded.positions) == 1 and loaded.positions[0].ticker == "NVDA"
    assert loaded.positions[0].first_green is True
    assert len(loaded.trades) == 1
    assert loaded.seen_signals == ["k1"]
    assert loaded.recently_sold == {"TSLA": "2026-05-20T09:00:00+00:00"}


def test_us_uk_t212_isolated(tmp_path):
    us = Portfolio(cash=18000.0); us.trades.append(_trade("US"))
    uk = Portfolio(cash=20000.0); uk.trades.append(_trade("UK"))
    t212 = Portfolio(cash=50000.0); t212.trades.append(_trade("T212"))
    us.save(tmp_path / "paper_portfolio.json")
    uk.save(tmp_path / "uk_portfolio.json")
    t212.save(tmp_path / "t212_portfolio.json")

    us2 = Portfolio.load(tmp_path / "paper_portfolio.json")
    uk2 = Portfolio.load(tmp_path / "uk_portfolio.json")
    t2 = Portfolio.load(tmp_path / "t212_portfolio.json")
    assert us2.cash == 18000.0 and [t.ticker for t in us2.trades] == ["US"]
    assert uk2.cash == 20000.0 and [t.ticker for t in uk2.trades] == ["UK"]
    assert t2.cash == 50000.0 and [t.ticker for t in t2.trades] == ["T212"]


def test_trades_are_append_only(tmp_path):
    p = Portfolio(cash=1000.0)
    p.trades.append(_trade("A"))
    p.save(tmp_path / "paper_portfolio.json")
    p2 = Portfolio.load(tmp_path / "paper_portfolio.json")
    p2.trades.append(_trade("B"))
    p2.save(tmp_path / "paper_portfolio.json")
    p3 = Portfolio.load(tmp_path / "paper_portfolio.json")
    assert [t.ticker for t in p3.trades] == ["A", "B"]


def test_positions_replaced_not_appended(tmp_path):
    p = Portfolio(cash=1000.0)
    p.positions.append(_position("AAA"))
    p.save(tmp_path / "paper_portfolio.json")
    p2 = Portfolio.load(tmp_path / "paper_portfolio.json")
    p2.positions = [_position("BBB")]   # AAA closed, BBB opened
    p2.save(tmp_path / "paper_portfolio.json")
    p3 = Portfolio.load(tmp_path / "paper_portfolio.json")
    assert [pos.ticker for pos in p3.positions] == ["BBB"]


def test_unknown_service_returns_default(tmp_path):
    # No data anywhere -> default portfolio so seeding logic still works.
    p = Portfolio.load(tmp_path / "paper_portfolio.json")
    assert p.cash == 1000.0 and p.trades == [] and p.positions == []


# ---------------------------------------------------------------------------
# Tracker isolation
# ---------------------------------------------------------------------------

def test_exit_tracker_isolated_by_service(tmp_path):
    us = ExitTracker()
    us.tracked.append(TrackedExit(
        ticker="USX", detector="ai_pivot", entry_price=10.0, exit_price=11.0,
        exit_dt="2026-05-10", exit_reason="first_green", pct_return=0.1, headline="h",
    ))
    us.save(tmp_path / "exit_tracker.json", "us")

    # UK service reads its own (empty) exit tracker, NOT the US one.
    uk = ExitTracker.load(tmp_path / "exit_tracker.json", "uk")
    assert uk.tracked == []
    us2 = ExitTracker.load(tmp_path / "exit_tracker.json", "us")
    assert len(us2.tracked) == 1 and us2.tracked[0].ticker == "USX"


def test_trade_memory_isolated_by_service(tmp_path):
    trade_memory.update_memory([_trade("X", pnl=5.0)], tmp_path / "trade_memory.json", "us")
    trade_memory.update_memory([], tmp_path / "trade_memory.json", "t212")
    us_mem = trade_memory.load_memory(tmp_path / "trade_memory.json", "us")
    t212_mem = trade_memory.load_memory(tmp_path / "trade_memory.json", "t212")
    assert us_mem["total_trades"] == 1
    assert t212_mem.get("total_trades", 0) == 0


# ---------------------------------------------------------------------------
# Auto-migration of legacy JSON
# ---------------------------------------------------------------------------

def test_legacy_portfolio_auto_migrates(tmp_path):
    legacy = tmp_path / "paper_portfolio.json"
    legacy.write_text(json.dumps({
        "cash": 12345.0,
        "positions": [],
        "trades": [{"ticker": "OLD", "detector": "buyback", "entry_price": 1.0,
                    "exit_price": 1.1, "shares": 1.0, "entry_dt": "2026-01-01",
                    "exit_dt": "2026-01-02", "pnl": 0.1, "pct_return": 0.1,
                    "exit_reason": "hold_expiry", "headline": "h",
                    "peak_price": 0.0, "severity": 0.5}],
        "seen_signals": ["s1"],
    }), encoding="utf-8")

    p = Portfolio.load(legacy)
    assert p.cash == 12345.0
    assert [t.ticker for t in p.trades] == ["OLD"]
    assert p.seen_signals == ["s1"]


def test_shared_tracker_migrates_only_for_us(tmp_path):
    legacy = tmp_path / "exit_tracker.json"
    legacy.write_text(json.dumps({"tracked": [{
        "ticker": "SH", "detector": "ai_pivot", "entry_price": 10.0,
        "exit_price": 11.0, "exit_dt": "2026-01-02", "exit_reason": "first_green",
        "pct_return": 0.1, "headline": "h", "peak_price": 0.0,
        "snapshots": [], "tracking_complete": True,
    }]}), encoding="utf-8")

    # UK must NOT pick up the US-owned shared file.
    uk = ExitTracker.load(legacy, "uk")
    assert uk.tracked == []
    # US imports it.
    us = ExitTracker.load(legacy, "us")
    assert len(us.tracked) == 1 and us.tracked[0].ticker == "SH"


def test_migration_script_validates(tmp_path):
    (tmp_path / "paper_portfolio.json").write_text(json.dumps({
        "cash": 9000.0, "positions": [],
        "trades": [{"ticker": f"T{i}", "detector": "earnings_surprise",
                    "entry_price": 1.0, "exit_price": 1.1, "shares": 1.0,
                    "entry_dt": "2026-01-01", "exit_dt": "2026-01-02", "pnl": 0.1,
                    "pct_return": 0.1, "exit_reason": "first_green", "headline": "h",
                    "peak_price": 0.0, "severity": 0.5} for i in range(4)],
    }), encoding="utf-8")

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "migrate_to_sqlite",
        Path(__file__).resolve().parents[1] / "scripts" / "migrate_to_sqlite.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = mod.main(["migrate_to_sqlite", str(tmp_path)])
    assert rc == 0
    # And the data is actually queryable from the DB afterwards.
    assert len(Portfolio.load(tmp_path / "paper_portfolio.json").trades) == 4


# ---------------------------------------------------------------------------
# Schema invariants
# ---------------------------------------------------------------------------

class TestSchemaInvariants:
    """At connect() time we sanity-check the DB has every table + critical
    column we need. Failure logs WARNING but does NOT crash — the bot
    must keep trying in degraded mode."""

    def test_freshly_created_db_passes_invariants(self, tmp_path):
        from switching import storage
        storage._reset_connection_cache()
        conn = storage.connect(tmp_path / "paper_portfolio.json")
        failures = storage.assert_schema_invariants(conn)
        assert failures == []

    def test_invariants_detect_missing_table(self, tmp_path):
        from switching import storage
        storage._reset_connection_cache()
        conn = storage.connect(tmp_path / "paper_portfolio.json")
        # Drop a table the bot relies on
        conn.execute("DROP TABLE positions")
        conn.commit()
        failures = storage.assert_schema_invariants(conn)
        assert any("positions" in f for f in failures)

    def test_invariants_detect_missing_column(self, tmp_path):
        from switching import storage
        storage._reset_connection_cache()
        conn = storage.connect(tmp_path / "paper_portfolio.json")
        # Rebuild closed_trades without the `severity` column to simulate
        # an old DB that pre-dates the severity addition. Note: invariants
        # don't check `severity` (it's optional), so this should still
        # PASS — but if we drop `service`, it MUST fail.
        conn.execute("DROP TABLE closed_trades")
        conn.execute("""
            CREATE TABLE closed_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT, detector TEXT, exit_reason TEXT,
                pnl REAL, pct_return REAL
            )
        """)
        conn.commit()
        failures = storage.assert_schema_invariants(conn)
        assert any("closed_trades.service" in f for f in failures)

    def test_invariants_run_once_per_db_path(self, tmp_path, caplog):
        # The whole point of the cache is one alert per process, not log spam.
        from switching import storage
        import logging
        storage._reset_connection_cache()

        # Create a broken DB
        conn = storage.connect(tmp_path / "paper_portfolio.json")
        conn.execute("DROP TABLE positions")
        conn.commit()
        # Drop from the connection cache so next connect re-runs init
        # (which won't recreate the dropped table at this point since we
        # ALREADY ran _init_schema, and CREATE IF NOT EXISTS won't fire
        # again... but we DO want to verify the invariants-cache stops the
        # second check from running)
        storage._INVARIANTS_CHECKED.clear()
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            storage._check_invariants_once(conn, tmp_path / "switching.db")
            # Second call: should be a no-op (key already in _INVARIANTS_CHECKED)
            storage._check_invariants_once(conn, tmp_path / "switching.db")
        warnings = [r for r in caplog.records if "invariants" in r.message.lower()]
        # First call should warn; second should be silent
        assert len(warnings) == 1
