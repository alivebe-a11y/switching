# Future SQL Schema Mapping

Forward-looking schema design. The system currently persists state in JSON
files (`paper_portfolio.json`, `exit_tracker.json`, `trade_memory.json`) which
works fine at the current scale (<500 trades). When trade volume grows or we
need cross-detector queries, we migrate to SQLite. This doc maps the JSON
shape onto target tables so the migration is mechanical, not architectural.

## Migration trigger

Migrate when **any** of:
- Closed trades exceed 1,000
- A query needs joins across detectors / time windows / price tiers
- Concurrent writers appear (e.g. live trader + paper trader on shared state)
- JSON load time exceeds 500ms on the dashboard

Until then, JSON wins on simplicity, hand-editability, and `git diff`-ability.

## Target: SQLite (single file at `/app/.cache/switching.db`)

SQLite chosen over Postgres because:
- Single-file backup mirrors current JSON ergonomics
- Bind-mounted to ZFS pool, snapshotted with the rest
- No daemon to manage in Docker
- Adequate for tens of thousands of rows

If we ever need real concurrency (multi-process writers, replication), revisit
Postgres at that point — the schema below works on either.

## Tables

### `positions`
Open positions. One row per ticker held.

| Column | Type | Source (JSON) | Notes |
|---|---|---|---|
| `id` | INTEGER PK | — | Auto-increment |
| `ticker` | TEXT NOT NULL | `positions[].ticker` | Indexed |
| `detector` | TEXT NOT NULL | `positions[].detector` | Indexed |
| `entry_price` | REAL NOT NULL | `positions[].entry_price` | |
| `shares` | REAL NOT NULL | `positions[].shares` | |
| `entry_dt` | TEXT NOT NULL | `positions[].entry_dt` | ISO8601 UTC |
| `headline` | TEXT | `positions[].headline` | |
| `severity` | REAL | `positions[].severity` | |
| `stop_loss` | REAL | `positions[].stop_loss` | |
| `hold_days` | INTEGER | `positions[].hold_days` | |
| `days_held` | INTEGER | `positions[].days_held` | Updated each scan |
| `first_green` | INTEGER | `positions[].first_green` | 0 or 1 |
| `first_green_pct` | REAL | `positions[].first_green_pct` | |

Index: `(ticker)`, `(detector, entry_dt)`.

### `closed_trades`
Append-only history of closed trades. The core of all analytics.

| Column | Type | Source (JSON) | Notes |
|---|---|---|---|
| `id` | INTEGER PK | — | |
| `ticker` | TEXT NOT NULL | `trades[].ticker` | Indexed |
| `detector` | TEXT NOT NULL | `trades[].detector` | Indexed |
| `entry_price` | REAL NOT NULL | `trades[].entry_price` | |
| `exit_price` | REAL NOT NULL | `trades[].exit_price` | |
| `shares` | REAL NOT NULL | `trades[].shares` | |
| `entry_dt` | TEXT NOT NULL | `trades[].entry_dt` | |
| `exit_dt` | TEXT NOT NULL | `trades[].exit_dt` | Indexed |
| `pnl` | REAL NOT NULL | `trades[].pnl` | |
| `pct_return` | REAL NOT NULL | `trades[].pct_return` | |
| `exit_reason` | TEXT NOT NULL | `trades[].exit_reason` | first_green / stop_loss / hold_expiry |
| `headline` | TEXT | `trades[].headline` | |

Indexes: `(detector, exit_dt)`, `(exit_reason)`, `(ticker, exit_dt)`.

### `signals`
Recent signals from each scan. Currently stored in `last_signals[]` (ring
buffer of 50). After migration: keep N days of full history.

| Column | Type | Source (JSON) | Notes |
|---|---|---|---|
| `id` | INTEGER PK | — | |
| `detector` | TEXT NOT NULL | `last_signals[].detector` | Indexed |
| `ticker` | TEXT NOT NULL | `last_signals[].ticker` | |
| `company` | TEXT | `last_signals[].company` | |
| `event_dt` | TEXT NOT NULL | `last_signals[].event_dt` | Indexed |
| `headline` | TEXT | `last_signals[].headline` | |
| `url` | TEXT | `last_signals[].url` | |
| `severity` | REAL NOT NULL | `last_signals[].severity` | |
| `evidence` | TEXT | `last_signals[].evidence` | |
| `ai_score` | REAL | `last_signals[].extra.ai_score` | Nullable |
| `traded` | INTEGER | derived | 0 or 1 — did we open a position? |
| `seen_at` | TEXT NOT NULL | `last_scan_dt` | When the scan that produced this ran |

Indexes: `(detector, event_dt)`, `(ticker)`.

### `exit_tracker_snapshots`
20-day post-exit price path per closed trade. Currently stored in
`exit_tracker.json`. One row per (trade, day_offset).

| Column | Type | Source (JSON) | Notes |
|---|---|---|---|
| `id` | INTEGER PK | — | |
| `trade_id` | INTEGER NOT NULL | FK to closed_trades | |
| `day_offset` | INTEGER NOT NULL | `tracked[].snapshots[].day` | 0..20 |
| `snapshot_dt` | TEXT NOT NULL | `tracked[].snapshots[].dt` | |
| `price` | REAL NOT NULL | `tracked[].snapshots[].price` | |
| `pct_from_exit` | REAL NOT NULL | derived | (price/exit - 1) |

Index: `(trade_id, day_offset)`. Unique on `(trade_id, day_offset)`.

### `trade_memory`
Aggregated per-detector / per-price-tier / per-exit-reason stats. Currently
recomputed from `closed_trades` on every scan and persisted to
`trade_memory.json`. Post-migration: materialised view, refreshed nightly.

| Column | Type | Notes |
|---|---|---|
| `detector` | TEXT NOT NULL | |
| `price_tier` | TEXT NOT NULL | low / mid / high |
| `exit_reason` | TEXT NOT NULL | |
| `n_trades` | INTEGER NOT NULL | |
| `n_wins` | INTEGER NOT NULL | |
| `avg_return` | REAL NOT NULL | |
| `avg_hold_days` | REAL NOT NULL | |
| `updated_at` | TEXT NOT NULL | |

Primary key: `(detector, price_tier, exit_reason)`.

### `cached_prices`
Last-seen price per held ticker. Currently a flat dict in
`paper_portfolio.json` (`cached_prices[ticker] = price`). Post-migration:
small table refreshed every scan cycle.

| Column | Type | Notes |
|---|---|---|
| `ticker` | TEXT PK | |
| `price` | REAL NOT NULL | |
| `updated_at` | TEXT NOT NULL | |

### `meta`
Singleton key-value store. Replaces top-level fields in
`paper_portfolio.json`.

| Key | Source |
|---|---|
| `cash` | `cash` |
| `last_scan_dt` | `last_scan_dt` |
| `max_position_pct` | `max_position_pct` |
| `max_positions` | `max_positions` |
| `seed_cash` | (CLI arg, persisted on first run) |

## Migration plan (when triggered)

1. Add `src/switching/storage.py` with a `Storage` ABC and two impls:
   `JsonStorage` (current) and `SqliteStorage` (new).
2. `Portfolio.load()` / `Portfolio.save()` becomes a thin wrapper that
   delegates. Behaviour identical.
3. Write a one-shot migration script `scripts/migrate_to_sqlite.py` that
   reads the JSON files and inserts into the SQLite tables.
4. Flip a config flag to switch storage backends. Keep JSON read-path for
   one release as a safety net.
5. Drop JSON support after a week of clean SQLite operation.

## What this is NOT

- Not a justification to migrate now. JSON is fine.
- Not a Postgres-vs-SQLite debate. SQLite first; revisit if needed.
- Not a normalised academic schema. `headline` is duplicated across
  `signals`, `positions`, `closed_trades` on purpose — it's
  human-readable provenance and the cost of duplicating ~200 chars per
  row is negligible.

## Open questions for migration day

- Do we keep JSON exports for `git diff`-ability? Probably yes — an
  hourly dump of `closed_trades` to a versioned CSV gives the same
  forensic value at minimal cost.
- Index strategy for `signals` if we keep all history vs. last N days?
  Lean toward last 90 days hot, archive older to a separate file.
