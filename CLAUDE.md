# Switching — Project Memory

## Overview
Stock signal detection and paper-trading system. Scans RSS feeds and SEC EDGAR for
corporate events (upgrades, M&A, FDA, dividends, contracts, etc.), scores signals,
paper-trades at next-day open, manages exits via detector-specific profiles.

Goal: Turn profit (~£20K after tax) from £100K investment. UK-based, trading US markets.
Ltd company structure at 25% corp tax being evaluated vs 40% personal rate.

## Repository
- **GitHub**: `alivebe-a11y/switching` (PUBLIC repo — no secrets)
- **Branch**: `main`
- **766 tests**, run with: `pytest tests/`

> ⚠️ **WORKING DIRECTORY GOTCHA (read before any `git`/`pytest`).** The git repo
> lives in the **`switching/` subfolder**, NOT the shell's default cwd. The
> default cwd is the parent `…\switch` folder (it holds only the `deploy.ps1`
> launcher + the `switching/` checkout) and is **not a git repository** — running
> `git add/commit/log` there fails with *"fatal: not a git repository"*.
> **Always operate inside the repo**: prefix shell calls with
> `cd /c/Users/berna/Desktop/switch/switching && …` (or use `git -C
> /c/Users/berna/Desktop/switch/switching …`). This bit a commit once; don't
> repeat it.

## Deployment (TrueNAS via Dockge)
- Stack path (Dockge UI): `/Pool_1/Configs/dockge2/Stacks/stocks`
  Host path over SSH (what scripts use): `/mnt/Pool_1/Configs/dockge2/Stacks/stocks`
- **Dockge uses `compose.yaml`** (NOT `docker-compose.yml`)
- Docker build context pulls directly from GitHub — no local git clone on TrueNAS
- **All services share ONE image tag** (`ghcr.io/alivebe-a11y/switching:latest`) and the
  same build context. So you BUILD once, but must `up -d` EVERY active service to put it
  on the new image — a running container keeps its old image until recreated.
- Active services that run the shared code: `paper-trade`, `paper-trade-uk`, `trade-t212`,
  `trade-t212-uk` (all use `paper_trader.py`) and `dashboard` (uses `web.py` +
  `weekly_report.py`).

### Deploy — one-click from Windows (preferred)
From the `switch` folder on Windows:
```powershell
.\deploy.ps1
```
Four steps, in order: **(1) backup live state** (invokes `backup.ps1`, aborts the deploy
if the backup fails — release-it rule "rollback path before forward path"), **(2) push
committed code to GitHub**, **(3) SSH to TrueNAS, run `scripts/deploy.sh`** (build once +
recreate all four services), **(4) tail logs**. No Dockge shell needed. Flags:
`-Services dashboard` (subset), `-SkipPush`, `-SkipBackup` (emergency hotfix when the
backup itself is broken — prints loud warning), `-Snapshot Pool_1/Configs` (also take a
ZFS snapshot in the backup step), `-Force`, `-NoLogs`. Requires one-time
`ssh-copy-id root@<truenas-ip>`. **Single source of truth: `scripts/deploy.ps1`** (under
git). The launcher at `switch\deploy.ps1` is a **thin shim** that just forwards its args
to `switching\scripts\deploy.ps1`, so it can never go stale — there is no copy to keep in
sync. Put ALL deploy logic in `scripts/deploy.ps1`; never add logic to the launcher shim.

### Deploy — on TrueNAS directly (fallback)
From the Dockge stack dir:
```bash
curl -sL https://raw.githubusercontent.com/alivebe-a11y/switching/main/scripts/deploy.sh | bash
```
Subset: `... | bash -s -- dashboard trade-t212`

### Deploy — manual one-liner (last resort)
```bash
curl -sL "https://raw.githubusercontent.com/alivebe-a11y/switching/main/docker-compose.yml" -o compose.yaml && docker builder prune -af && docker compose build paper-trade && docker compose up -d paper-trade paper-trade-uk trade-t212 dashboard
```
- Dashboard port: 8080
- ⚠️ Do NOT deploy only `paper-trade` when a change touches `paper_trader.py` — that file
  is shared by `paper-trade-uk` and `trade-t212` too, and they'd run stale code.
- ⚠️ The old scp-based `deploy.ps1` (synced source + built `switching:local`) is retired —
  it diverged from GitHub and only restarted 2 services. The new `deploy.ps1` is a thin
  GitHub trigger so both paths build the same image.
- ℹ️ First deploy on the SQLite build auto-migrates the JSON state on startup (no manual
  step). To confirm nothing was lost, exec into any service and run
  `python scripts/migrate_to_sqlite.py /app/.cache` — expect "VALIDATION PASSED".

### Backup
`deploy.ps1` now runs `backup.ps1` automatically as Step 1 and aborts the deploy on
backup failure — so you can't accidentally ship code on top of an un-backed-up state.
You only need to invoke `backup.ps1` directly for **ad-hoc** backups outside a deploy:
```powershell
.\backup.ps1                          # tar cache on NAS + pull copy to .\backups
.\backup.ps1 -Dataset Pool_1/Configs  # also take a ZFS snapshot (gold standard)
```
Backs up `data/cache` (portfolio JSON + `switching.db` + trackers) to a timestamped
tar.gz on the NAS, prunes to the last `-Keep` (default 10), and downloads an off-box
copy. Secrets (`.env`) are deliberately excluded. Version-controlled copy at
`scripts/backup.ps1`.

## Services (docker-compose.yml)
| Service | Command | Notes |
|---------|---------|-------|
| paper-trade | `switching paper-trade --seed 20000 --interval 10 --stop-loss 0.026 --hold-days 5` | US paper trading, runs 24/7 |
| paper-trade-uk | `switching paper-trade --market uk --seed 20000 --interval 10 --stop-loss 0.026 --hold-days 5 --state /app/.cache/uk_portfolio.json` | LSE paper trading, runs 24/7 |
| trade-t212 | `switching trade-t212 --market us ...` | US T212 demo trading via REST API |
| trade-t212-uk | `switching trade-t212 --market uk ...` | LSE T212 demo trading via REST API. Shares ONE T212 account with `trade-t212` but isolated via broker-level position filter (US sees `*_US_EQ` only, UK sees `*L_EQ` only). State in `t212_uk_portfolio.json`. |
| dashboard | `switching web --port 8080` | Flask web UI on port 8080 |
| switching | `switching list-detectors` | One-shot utility |

(Alpaca `trade` service removed 2026-06 — UK-based, can't use Alpaca; IBKR is the
chosen live broker, see ADR-006. `broker_alpaca.py` + the `trade` CLI command +
`run_loop_alpaca` are gone.)

## Environment Variables (set in Dockge .env)
- `SWITCHING_EDGAR_UA` — Required for EDGAR-based detectors (activist_13d, insider_cluster)
- `ANTHROPIC_API_KEY` — Claude Haiku for AI signal scoring (~$0.30/month)
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — Push notifications (routine trade alerts)
- `TELEGRAM_ALERT_BOT_TOKEN` (+ optional `TELEGRAM_ALERT_CHAT_ID`) — SEPARATE **ops bot** for
  "something has gone wrong" alerts (different sender, survives the main bot's token failing).
  Optional: if unset, those alerts fall back to the main bot. `TELEGRAM_ALERT_CHAT_ID` defaults
  to `TELEGRAM_CHAT_ID`.
- `T212_API_KEY` — Trading 212 demo API key (US + UK T212 services share it)

## Critical: Data Directory
Seed CSVs MUST go in `src/switching/data/historical_events/` (NOT top-level `data/`).
The `_find_data_root()` in `sources/historical.py` resolves relative to the package.
The top-level `data/` directory is a mirror/legacy — always put seeds in both.

## Storage: SQLite (`switching.db`), service-scoped
All runtime state + analytics live in ONE SQLite DB (`<cache>/switching.db`, WAL mode),
NOT in per-service JSON files. Every row carries a `service` column (`us` / `uk` / `t212`),
so the three services share one file without colliding and can be compared with one query.
- `src/switching/storage.py` is the engine. Tables: `closed_trades` (append-only),
  `positions`, `exit_tracks`, `skipped_signals` (each relational + `service`), and
  `service_state` (key/value per service for cash/scalars + JSON blobs: seen_signals,
  last_signals, cached_prices, recently_sold, trade_memory). OHLC snapshots ride as a
  JSON column on their row.
- `Portfolio`, `ExitTracker`, `SkippedTracker`, `trade_memory` keep their dataclass APIs;
  only persistence changed. Trackers/memory take an explicit `service` arg (default `"us"`);
  `Portfolio` derives service from the state-file name via `storage.service_from_path`.
- **Auto-migration**: first time a service loads with an empty DB, it imports the legacy
  JSON sitting next to it — zero data-loss, no manual step. The old SHARED trackers
  (`exit_tracker.json` / `skipped_signals.json` / `trade_memory.json`) import only for
  `us` (who owned them); `uk`/`t212` start clean.
- **Validate the migration**: `python scripts/migrate_to_sqlite.py [CACHE_DIR]` — imports
  (idempotent) and prints a json-vs-db count table; exit 0 = no rows lost.
- Why: the old JSON files were SHARED between US and UK (same filenames in one dir),
  so the two services clobbered each other's exit/skipped/memory data, and T212 collected
  none of it. SQLite + service column fixes all three, and WAL gives atomic, concurrent-safe
  writes (no more half-written-file reads by the dashboard).
- Legacy JSON files are kept as read-only backups (gitignored, never re-read after migration).

## Data-protection guarantees
Three layers stop the trade history (now 188+ trades + 600+ funnel drops + post-exit
trackers) from being silently corrupted by a future schema/code change:

1. **Backup is the first step of every deploy** (`deploy.ps1` invokes `backup.ps1`
   and aborts on backup failure). `-SkipBackup` is the emergency lever for when the
   backup script itself breaks — prints a loud warning. Release-it rule: "rollback
   path before forward path".
2. **Frozen v1 fixture + compat tests** (`tests/fixtures/switching_v1.db`,
   `tests/test_storage_compat.py`). CI loads a representative golden DB via the
   public `Portfolio.load` / `ExitTracker.load` / etc. APIs and a load→save→reload
   roundtrip every run. A change that drops a column, renames a key, or zeroes
   rows on save fails CI before merge. Regenerate the fixture intentionally
   (`python scripts/build_test_fixture.py`) when a schema change is deliberate.
3. **Schema invariants at `connect()`** — `storage.assert_schema_invariants` runs
   once per process and checks every required table + critical column (notably
   `service` everywhere) is present. Mismatch → WARNING log + optional Telegram
   alert. Never crashes (release-it: "preserve core service in degraded mode").

## Detectors (16 registered)
| Detector | Source | Exit Profile | Markets |
|----------|--------|--------------|---------|
| earnings_surprise | RSS (earnings feeds + UK_FEEDS) | first_green +2%, 3-day hold | US + UK |
| ai_pivot | RSS (default feeds) | **ride mode**: first-green flips into peak-trailing (3% band), 6-8 day backstop | US + UK (enabled 2026-05-27, data-collection) |
| analyst_upgrade | RSS (default feeds) | first_green +1%, 3-day hold | US + UK |
| fda_decision | RSS (default + earnings) | first_green +3%, 3-day hold | US + UK (enabled 2026-05-27, data-collection) |
| buyback | RSS (default + corporate) | NO first_green, 5-day hold | US + UK |
| index_inclusion | RSS (default + corporate) | default (first_green +0%, 5-day) | US + UK (FTSE) |
| spinoff | RSS (default + corporate) | default | US + UK |
| mna_target | RSS (default + corporate + UK_FEEDS) | **ride mode**: first-green flips into peak-trailing (3% band), 8-day backstop; **acquirer-direction signals are skipped** | US + UK |
| guidance_raise | RSS (default + earnings + corporate + UK_FEEDS) | **ride mode** (2026-06): +5% green → peak-trailing 3% band, 10-day backstop (post-exit data: durable +8.9%@day20) | US + UK |
| dividend_surprise | RSS (default + earnings + corporate) | first_green +1%, 4-day hold, +1% wider stop | US + UK |
| contract_win | RSS (default + corporate) | **ride mode** (2026-06): +2% green → peak-trailing 3% band, 8-day backstop (post-exit data: durable +4.1%@day20, small n) | US + UK |
| activist_13d | SEC EDGAR (13D filings) | default | US only |
| insider_cluster | SEC EDGAR (Form 4) | default | US only |
| stock_split | RSS (default + corporate) | first_green +1.5%, 4-day hold | US + UK |
| crypto_treasury | RSS (default + corporate) | first_green +3%, 3-day hold | US + UK |
| uk_director_dealing | RSS (UK_FEEDS) | **ride mode** (2026-06: +1.5% green → flips to peak-trailing 4% band, 6-day backstop) | UK only |

## UK Service (_UK_DEFAULT_DETECTORS)
The `paper-trade-uk` service (and `--market uk` flag) uses `_UK_DEFAULT_DETECTORS` in cli.py:
`earnings_surprise, analyst_upgrade, mna_target, guidance_raise, dividend_surprise, buyback,
index_inclusion, spinoff, contract_win, stock_split, crypto_treasury, uk_director_dealing,
ai_pivot, fda_decision`

**Excluded from UK** (no UK data source — would require new detectors):
- `activist_13d` (SEC 13D filings only). UK equivalent on roadmap:
  `uk_activist_holdings` parsing RNS TR-1/DTR5 disclosures (3% threshold, 2-day filing —
  faster than US 13D). Trigger built when UK paper-trade produces enough flow.
- `insider_cluster` (SEC Form 4 only). UK equivalent on roadmap: `uk_insider_cluster`
  aggregating PDMR notifications (MAR Article 19) on top of `uk_director_dealing` — same
  Investegate RNS source, windowed cluster logic.

**Re-enabled 2026-05-27 (data-collection)**: `ai_pivot`, `fda_decision`. Both are
RSS/regex (not SEC-tied), so they can fire on UK headlines. UK has plenty of
AI-pivot stories (Darktrace, ARM, AIM tech) and UK-listed biotechs (Hutchmed,
GSK) announcing FDA decisions. Whether the edge holds at LSE liquidity is an
open question — live data will tell us.

**UK price floor REMOVED 2026-05-27**: was £1 in normalised units (rejected AIM
penny stocks). AIM has legitimate sub-£1 names (small caps, recovery plays); the
old floor was a US assumption transplanted. US $1 floor stays (sub-$1 NYSE/NASDAQ
names are usually failing reverse-splits / OTC-grade). The +2% tiered stop-loss
for <£5 stocks (`_tiered_stop_loss`) stays as the volatility safeguard, so
penny-stock churn is at least bounded.

## Stop-Loss Tiers (normalised price)
Tiers apply to normalised price (GBP for UK = pence/100, USD for US):
- £30+/\$30+ stocks: base stop
- £5-30/\$5-30 stocks: base stop + 1%
- <£5/<\$5 stocks: base stop + 2%

## Ride Mode (momentum exit, `_exit_profile`)
Live data showed momentum detectors leave money on the table (ai_pivot +22%/peak
day ~8, mna_target +11%/peak day ~7) under a fixed first-green exit. "Ride mode"
(profile keys `ride: True`, `trail_pct: 0.03`) makes a position **flip into
peak-tracking when it goes green on day ≥1** instead of taking the small win — it
then rides with a 3% trailing band until a 3% drop from peak or the hold-days
backstop (ai_pivot 6–8d, mna_target 8d). Derived per-cycle in `check_exits`; no
persisted Position fields beyond `peak_tracking`/`peak_price` (which already save).
The pre-existing day-0 **+8% spike** peak-tracking still applies to all detectors.

### ⏱️ Exit-profile change marker — 2026-06-11 14:00 BST (13:00 UTC)
**Cohort cutoff for reviewing these changes.** Trades that CLOSE after this timestamp run
the new exit rules; before it, the old ones. When evaluating "did it work?", split
`exit_dt` on this date (recent-vs-historical) — the per-detector aggregates blend old+new
until enough fresh trades accumulate, and win-rate may FALL even if avg-return/P&L rise
(ride trades certainty for size). The post-exit tracker's `avg_left_on_table` shrinking is
the cleanest success signal. Changed on this date (from post-exit-tracker data, US n shown):
- **guidance_raise** → ride mode, +5% green → 3% trail, hold 5→**10d** (durable +8.9%@day20, n=17)
- **contract_win** → ride mode, +2% green → 3% trail, hold 5→**8d** (durable +4.1%@day20, n=9, low-confidence)
- **uk_director_dealing** → ride mode, +1.5% green → 4% trail, 6d (was scratching at break-even)
- **t212_orphan** (manual/adopted T212 buys) → ride mode, +2% green → 3% trail, hold **10d**, stop unchanged
Expect: fewer `first_green` exits, more `peak_trailing`/`hold_expiry`, longer holds.

## Position Sizing — conviction weighting (`_position_weight`)
Replaces the old fixed-$2k guidance_raise override with a fund-relative multiplier
on the base `max_position_pct`:
- guidance_raise ×7 (63% WR, ~95% of live P&L), dividend_surprise/contract_win ×2,
  buyback ×1.5, everything else ×1.0.
- Weak/fat-tail detectors are **not** sized down — tail winners come from
  low-win-rate detectors (mna_target produced a +56% trade), so shrinking them
  would clip the tail.
- Hard cap `_MAX_SINGLE_POSITION_PCT = 0.12` per position; still bounded by cash.
- Base `--max-position` raised 1.0% → 1.5% (US + UK paper) to lift capital use
  (was deploying only ~£6k of £20k). Applied in both `open_position` (paper) and
  the T212 buy loop.

## Market calendar — half-days & fast-scan window
`market_calendar.py` knows:
- **NYSE half-days** (1:00 pm ET early close): Black Friday, Christmas Eve,
  day-before-July-4 (when applicable). Hard-coded in `_NYSE_HALF_DAYS` through
  2027 — update annually from https://www.nyse.com/markets/hours-calendars.
- **LSE half-days** (12:30 London early close): Christmas Eve, New Year's Eve.
  In `_LSE_HALF_DAYS`.
- `is_market_hours()` / `is_lse_hours()` honor the early close automatically.
- `minutes_since_us_open()` / `minutes_since_lse_open()` return float minutes
  since today's open, or None when the market is closed.

**Fast-scan window** (`_FAST_SCAN_AFTER_OPEN_MINUTES = 15`,
`_FAST_SCAN_INTERVAL_SECONDS = 60`): for the first 15 min after market open,
both `run_loop` and `run_loop_t212` tighten their feed-scan cadence to 1 min
(capped at the user's base `--interval` so a faster user setting isn't slowed).
This is when news premium is hottest — overnight catalysts gap on the open
and fresh headlines drop at the bell, so we want them detected within ~1 min,
not the regular 10-min cycle. Outside the window we revert to base interval
to spare RSS feeds.

## Out-of-hours signal queue (`pending_orders`)
News breaks 24/7 but US markets are open ~6.5h/day and LSE ~8.5h/day, so the bot
used to (a) silently fail or (b) submit market orders the broker had to reject
when a catalyst arrived after-hours. Both meant the trade was lost. Fix:
- When the relevant market is closed and a fresh signal classifies + tickers,
  it's serialised into `Portfolio.pending_orders` (persisted SQLite blob) and
  marked `seen` so the next scan doesn't re-queue the same article. No
  yfinance call, no broker call.
- At the top of every loop tick when the market is open, `_drain_pending_orders`
  pops the whole queue and feeds those signals back into the buy pipeline as
  if fresh — so they run through the normal acquirer/price/size/cooldown gates
  using *the open-time price*, not a stale after-hours quote.
- Stale entries (`>_PENDING_ORDER_MAX_AGE_HOURS = 48h` old) are dropped on every
  drain/expiry pass so a multi-day outage doesn't replay dead news. 48h covers
  the Fri-close → Mon-open weekend; anything older has decayed past "news trade".
- Applied to both `run_loop` (US + UK paper) and `run_loop_t212` (gated on US
  hours since the T212 service trades US tickers; LSE is open during NYSE
  morning so a stray UK ticker still resolves promptly).
- Survives container restarts: the queue is in the same SQLite blob list as
  `seen_signals` / `recently_sold`. Verified by `test_queue_persists_through_save_and_load`.

## UK news sources (Investegate primary + Google News fallback)
The old UK RSS feeds all died. UK ingestion now (`rss._fetch_uk`, triggered when
`market=="uk"`):
- **Investegate** (`sources/investegate.py`) is the **primary** source — HTML scrape
  of the RNS table (server-rendered, every row carries the EPIC), TTL-cached 240s so
  the ~13 UK detectors don't re-scrape per cycle. Near-primary RNS, far more complete
  than Google News. Parsed with **BeautifulSoup**, anchor-centric (finds `/announcement/`
  links by URL path, robust to CSS-class/attribute changes); EPIC taken from the URL
  slug's last dash-token (digit-leading depositary lines like `0HAF` skipped);
  **fails loud** (0 items ⇒ failover).
- **Google News RSS** (`UK_FEEDS`) runs in **parallel** as fallback/supplement;
  merged with Investegate, deduping Google items whose normalised title Investegate
  already covers (Investegate wins).
- **Failover**: if Investegate errors or parses 0 items, use Google News only and
  send a Telegram alert (`_alert_uk_failover`, 30-min cooldown so the per-detector
  calls don't spam). The proper low-latency fix is the roadmap "primary-source
  ingestion" item (LSE RNS API direct + a UK ticker resolver).
- Caveat: Investegate scraping is more fragile than RSS (breaks on a site redesign) —
  the loud failover + Google fallback is the safety net.

## Telegram notification market tags
Every message is prefixed with a per-process market label (`notifications.set_market`,
called once at loop start): `🇺🇸 US` / `🇬🇧 LSE` / `🇺🇸 T212`, so the three services'
alerts are distinguishable in one chat. Default is no prefix (unconfigured/tests).

**Two bots: trades vs ops.** Routine notifications (`notify_text`, buys/sells/summaries) go
via the main bot (`TELEGRAM_BOT_TOKEN`). **Failure / "gone wrong" alerts** go via
`notify_alert()` → a SEPARATE ops bot (`TELEGRAM_ALERT_BOT_TOKEN`) so they stand out from the
chatty trade feed and still arrive if the *main* bot's token is the thing that broke. Routed
to the ops bot: AI-filter circuit breaker, bad-price guard (`data_error`), UK RNS failover,
EDGAR 429 warning, schema-invariant failure. `notify_alert` falls back to the main bot if the
ops bot isn't configured, and is a silent no-op if neither is (callers log the failure anyway).

## Stability rules (Release It!)
@docs/release-it.mini.md

This bot is almost entirely flaky-external-dependency plumbing (yfinance, Trading 212,
RNS scraping, Telegram, SEC EDGAR), so the Release It! ruleset above is imported and
**applies whenever you add an outbound call, queue, cache, scheduled job, or deploy/ops
path**. How it maps onto our code today:

| Release It! pattern | Where we already do it | Gaps / TODO |
|---|---|---|
| Explicit timeouts | `requests(..., timeout=15)` in brokers + Investegate; Telegram `urlopen(timeout=10)` | yfinance calls rely on its defaults — audit |
| Bounded retries + backoff | T212 429 `Retry-After`/escalating backoff (`_MAX_RETRIES_429`) | only T212; others fail-then-skip |
| Fallback / degraded mode | UK Investegate→Google failover; T212 fill estimate falls back to yfinance | — |
| Fail fast / fail loud | Investegate 0-items ⇒ failover + Telegram alert; migration validation | — |
| Bounded buffers | `seen_signals[-500:]`, `last_signals[-50:]`, skipped tracker cap 500 | — |
| Validate external responses | EPIC/ticker validation, `_safe_float`, market-aware price normalise | RSS/scrape shape only loosely checked |
| Don't hold resources across slow calls | SQLite WAL + short txns; per-endpoint throttle | — |
| **Circuit breaker** | **AI filter** (`ai_filter.py`): trips after 3 consecutive Anthropic failures → stops calling, fires ONE Telegram alert + error log, half-open probe every 30 min, alerts on recovery | Still TODO: per-detector/per-feed breaker ("disable detector after N empty scans") — the AI-filter breaker is the first instance of the pattern to copy |
| Observability at boundaries | per-detector items/classified/with_ticker logs; `UK sources: …` line | no metrics/correlation IDs yet |

When touching any external boundary, run the file's **Final checklist** — especially the
circuit-breaker gap, which is the biggest missing defense.

## Architecture
```
src/switching/
├── cli.py              — Typer CLI (scan, backtest, paper-trade, paper-trade-uk, web, check-feeds)
├── paper_trader.py     — Core trading loop, position/exit management (market-aware)
├── web.py              — Flask dashboard (reads cached prices from portfolio JSON, no live yfinance polling)
├── registry.py         — @register decorator, load_builtin_detectors()
├── signal.py           — Signal dataclass, PriceReaction, dedup_key
├── pricing.py          — yfinance wrapper, PriceCache (SQLite)
├── backtest.py         — Historical replay engine
├── reporter.py         — rank, render_table, write_json/csv
├── trade_memory.py     — Per-detector/per-price-tier stats from closed trades
├── exit_tracker.py     — Post-exit price tracker (20 days) for detector refinement
├── skipped_tracker.py  — Tracks signals skipped due to max-positions / insufficient cash; runs same exit logic for "would-have-been" P&L
├── weekly_report.py    — Saturday weekly report: detector rankings, T212 vs paper, suggestions, Telegram delivery
├── ai_filter.py        — Claude Haiku scoring (0-1), log-only mode
├── notifications.py    — Telegram push (buys batched 2h, sells/stops immediate, daily summary at close, weekly report Saturdays)
├── detectors/          — All detector modules (one per file)
│   └── base.py         — Detector ABC
├── sources/
│   ├── rss.py          — DEFAULT_FEEDS, EARNINGS_FEEDS, CORPORATE_FEEDS, UK_FEEDS; UK source orchestration (_fetch_uk)
│   ├── investegate.py  — Investegate RNS HTML scraper (primary UK source), TTL-cached
│   ├── historical.py   — Seed CSV loader + live EDGAR augmentation
│   ├── sec_edgar.py    — EdgarClient (rate-limited, needs SWITCHING_EDGAR_UA)
│   └── ticker_lookup.py — SEC company-name→ticker fallback for extract_ticker()
├── detection_funnel.py — captures classified-but-no-ticker drops (the silent loss)
├── movers.py           — movers researcher: audit why top movers were/weren't caught
└── data/historical_events/*.csv  — Seed data for backtests
```

## Adding a New Detector (checklist)
1. Create `src/switching/detectors/<name>.py` with `@register` class
2. Add seed CSV to `src/switching/data/historical_events/<name>.csv` (AND `data/historical_events/`)
3. Import + register in `registry.py` → `load_builtin_detectors()`
4. Add exit profile in `paper_trader.py` → `_exit_profile()`
5. Add to `_DEFAULT_DETECTORS` list in `cli.py`
6. Create `tests/detectors/test_<name>.py`
7. Run `pytest tests/` — must stay green

## Key Patterns
- EDGAR-based detectors need `client=edgar_client` in constructor
- RSS-based detectors take optional `feeds: tuple[str, ...] | None`
- Every detector has a standalone `classify(title, summary)` function for unit testing
- Severity always capped at 0.95
- All 11 RSS detectors log `items/classified/with_ticker` counters per scan cycle
- `extract_ticker()` has two-stage pipeline: exchange-prefix regex → SEC company-name lookup fallback
- Dashboard reads `cached_prices` dict from portfolio JSON — no per-request yfinance calls
- Buy notifications batch every 2 hours via `_NotificationQueue`; sells/stops fire immediately

## Ticker Extraction
`FeedItem.extract_ticker()` in `sources/rss.py` resolves tickers in two stages:
1. **Regex**: matches `NASDAQ:AAPL`, `NYSE:XYZ` etc. (original, works for ~10% of headlines)
2. **SEC fallback**: `sources/ticker_lookup.py` parses parenthesized tickers like `(AAPL)`
   and matches SEC-registered company names (≥5 chars) in the first 120 chars of the headline.
   Bare uppercase words are NOT matched (too many false positives). Cached to disk for 7 days.

Known recall holes (candidates for the LLM resolver): short/brand company names
(Meta, Visa, Ford; "Google"≠"Alphabet Inc."), tickers only in the body, UK items without
an EPIC. When `classify()` matches but extraction fails, the signal is dropped — now
captured by the detection funnel below.

## Detection Funnel (drop capture) — `detection_funnel.py`
Every RSS detector's `scan()` has one drop point: `if not ticker: continue`. That bins a
real catalyst we classified but couldn't ticker — silently, with no record. `detection_funnel`
makes that loss visible: each detector calls `detection_funnel.record_drop(self.name, item)`
at that point, writing to the `dropped_signals` SQLite table (service-tagged, pruned to 1000).
- `configure(service, state_path)` is called once at loop start (run_loop / run_loop_t212);
  `record_drop` is a no-op until configured (tests/backtests don't write).
- Dashboard: `/api/drops` + the "Detection Funnel — Dropped Headlines" panel (Analytics tab)
  show counts by detector and the recent dropped headlines.
- **This is the single chokepoint a future LLM ticker-resolver plugs into** — on a drop,
  hand the headline to a model (a **local GPU model** via Ollama/llama.cpp, or Haiku),
  validate the proposed ticker against the SEC/known list (kills hallucination), and recover
  the trade. Measure first (a week of drops) to confirm ticker-loss is the bottleneck vs
  classify misfires, then point the resolver at it.

## Movers Researcher (detector scoreboard) — `movers.py`
The detection funnel catches drops *we classified*. The movers researcher catches the
moves we **never saw at all**. Each market day pull the top movers (yfinance screener:
US predefined `day_gainers`/`most_actives`; UK `region=gb` EquityQuery filtered to real
LSE stocks — depositary lines like `0LC7.L` dropped), and for every mover we didn't
trade, attribute WHY into buckets (the pure, tested `attribute()`):
- `caught` — in our records (signal/trade); not a miss.
- `ticker_drop` — a detector classifies the mover's news, and we ingested that story
  (it's in the funnel/last_signals) → recall hole (ticker resolution).
- `feed_gap` — a detector *would* classify it, but the story isn't in our records →
  the gap is the **news source**, not the detector. (The big one for UK.)
- `no_detector` — has news but nothing classifies → uncovered catalyst type (new-detector
  candidate).
- `no_news` — no headline → flow/squeeze, not our game.
Run: `switching movers-audit --market us|uk` (cron weekdays 22:00 NAS-local = ~1h after
US close, or ad-hoc). Writes one file PER DAY: `<cache>/movers_audit/<market>/<YYYY-MM-DD>.json`
(kept 90 days). Dashboard **🔎 Movers tab** (with a day picker) + `/api/movers?market=&date=`
render it — newest by default. **Purpose is measurement, NOT trading** — by the time something is a top mover
the move has happened; this grades detector recall and ranks what to build next.
**Attribution is heuristic** (yfinance `.news` ≠ our exact feeds). US is clean; UK movers
are noisier (yfinance UK news is weak) — tune US first, then UK.

## Notification Batching
`src/switching/notifications.py` queues buy notifications in `_NotificationQueue` and flushes
every 2 hours via a daemon timer. Sells, stop-losses, skips, and the end-of-day summary call
`_send()` directly. The paper trader calls `flush_buy_queue()` before sending the daily summary
so the digest is up-to-date. With unlimited positions this prevents Telegram spam (10-20 buys
per scan cycle would otherwise produce 10-20 separate messages).

## Dashboard Data Flow
The Flask dashboard (`src/switching/web.py`) reads everything from `switching.db` (via the
same `Portfolio`/`ExitTracker`/`SkippedTracker` loaders, service-scoped) — it never calls
yfinance. The paper trader's scan loop populates `portfolio.cached_prices[ticker]` for every
held position each cycle, then saves. Dashboard refreshes show prices stamped with
`last_scan_dt`. Stale by at most one scan interval (default 10 min). Stale tickers (no longer
held) are pruned each cycle. The `/api/review`, `/api/skipped-signals`
endpoints are US-scoped; `/api/exit-tracker` accepts `?service=uk` (default US) and
the UK dashboard tab has its own "LSE Post-Exit Tracker" panel; `/api/uk`
(incl. per-LSE-detector `detector_stats`) and `/api/t212` read their own services.

## Known Issues / Gotchas
- yfinance blocked in CI/sandbox — backtests show 0 trades but events load fine
- Telegram: duplicate notifications if old container still running alongside new one
- Terminal hyperlinks can mangle Python module names in Dockge console
- EDGAR rate limit: 8 req/s (conservative vs SEC's 10 req/s limit)
- SEC ticker map caches to `/tmp/sec_company_tickers.json` (override via `SWITCHING_CACHE_DIR`)
- **Optimistic stop fills**: `check_exits` snaps a stop-loss exit to `entry × (1 − stop_loss)`.
  Real fills gap *through* the stop in a fast drop, so paper-trade understates crash losses —
  T212's real fills are the truth. Biggest divergence is during sharp sell-offs. (See
  pre-live safety gate item #4.)
- **No market-regime / drawdown halt yet** — the bot trades each signal independently and
  has no "risk-off, stop buying" switch. Fine for demo; a hard blocker before real money
  (pre-live gate #1).
- **FX drift on cross-currency T212 holdings** — a UK-base T212 account (GBP) holding USD
  stocks shows P&L moves *even on US market holidays*: stock prices are static but
  GBP/USD keeps trading, so the GBP value of USD positions drifts. T212 returns position
  prices in the instrument's currency (USD) but unrealised P&L in the account's currency
  (GBP). Loop logs now use the actual `acct.currency` symbol for account/pnl values
  (per-position prices stay `$` since they're USD stock quotes). When going to real money:
  this FX *is* a real exposure — even when stocks don't move, your portfolio value floats
  with GBP/USD until you close.

---

## Maintenance: Review & Update This File
At the start of each session, verify this file is still accurate. Update when:
- A new detector is added or removed
- Exit profiles change based on live performance data
- Infrastructure changes (new services, new env vars)
- Performance baselines shift (win rates after 50+ trades)
- Architecture decisions are revisited

**Triggered backlog — check on every roadmap scan:** review the trigger conditions for
deferred items in `## Roadmap → Triggered / Deferred items`. If a deferred item's
conditions are now ALL met, surface it to the user as ready to build. (Currently:
corporate-actions feed ingestion — gated on real capital + a structured data source.)

---

## Architecture Decision Records (ADRs)

### ADR-001: Regex classifiers over ML/LLM classification
**Decision**: All detectors use compiled regex patterns, not ML models.
**Why**: (1) Zero latency — regex runs in <1ms vs 500ms+ for API call. (2) No training
data needed — financial headlines follow predictable templates. (3) Interpretable —
can debug exactly why a headline matched or didn't. (4) Free — no API costs per scan.
**When to revisit**: If win rate across detectors drops below 50% consistently, or if
headline formats diversify beyond regex capability. Phase 2 may add Sonnet for complex
multi-factor signals.

### ADR-002: yfinance over Polygon.io / paid data
**Decision**: Use yfinance (free) for price data.
**Why**: (1) Zero cost during paper-trading phase. (2) Good enough for daily OHLC.
(3) No API key management. (4) Acceptable latency for 10-minute scan interval.
**Tradeoffs**: Rate-limited, occasionally unreliable, no real-time quotes, no options
chains. Blocks in some environments (CI, sandboxes).
**When to revisit**: When scaling to real capital (Phase 2). Polygon.io at $30/month
gives real-time + options.

### ADR-003: First-green exit strategy
**Decision**: Most detectors exit on the first day that closes above entry price (+ a
percentage threshold per detector).
**Why**: Momentum catalysts (upgrades, FDA, M&A) tend to gap up then fade. Taking
profit on the first green close captures the initial pop without holding through the
pullback. Backtests showed higher Sharpe ratios vs fixed hold-period exits.
**Exceptions**: Buyback uses NO first-green (slow grind, not a pop). MNA targets hold
longer (deal spread takes time to close).

### ADR-004: Detector-specific exit profiles over one-size-fits-all
**Decision**: Each detector has its own first_green_pct, hold_days, and first_green flag.
**Why**: Different catalysts move differently. FDA approvals gap 10-30% (take profit at
+3%). Analyst upgrades drift 1-3% (take at +1%). Buybacks are slow grinds (hold 5 days,
no first-green). One exit rule can't serve all.
**Data**: Tuned from backtest seeds. Will refine with live trade data (Phase 1 goal).

### ADR-005: Public GitHub repo
**Decision**: Repo is public. No secrets in code.
**Why**: (1) Docker build context uses GitHub URL — needs public access for TrueNAS
builds without SSH keys. (2) Demonstrates transparency for potential investors/partners.
(3) No competitive moat in the code itself — edge comes from execution and tuning.
**Mitigations**: .gitignore covers .env, keys, state files, portfolio JSON. All secrets
live in Dockge .env only.

### ADR-008: Trading 212 demo as parallel execution layer
**Decision**: Add a `trade-t212` service that runs alongside the internal paper trader.
**Why**: The internal paper trader uses yfinance theoretical next-open prices. T212 demo
uses their own simulated fills. Running both in parallel on the same signals reveals real
execution slippage before committing real capital.
**Architecture**: Separate state file (`t212_portfolio.json`), same exit profiles as
internal paper trader, acquirer filter applied. Compare P&L after 50+ trades.
**When to go live**: Flip `T212_DEMO=false` in Dockge .env — no code changes needed.

**T212 loop cadence & settlement (added after live demo bugs)**:
- **Exits poll every 60s** (`_T212_EXIT_POLL_SECONDS`), not every scan interval —
  T212's REST API has NO arbitrary quote endpoint, but `/equity/positions` returns
  live `currentPrice`/`unrealized_pnl_pct` for HELD positions. Polling that every
  60s gives tight price tracking and fast stop-loss / first-green execution.
  New-signal scanning still runs every `scan_interval_minutes` to avoid hammering feeds.
- **Buys still use yfinance** for the quote (T212 can't price a ticker you don't hold).
- **Settlement guard** (`_T212_SETTLE_MINUTES = 15`): after a sell, the position is
  removed locally but T212 may still report it briefly while the order settles.
  `recently_sold[symbol]` records the sell time; orphan-reconciliation skips symbols
  sold within the settle window so we don't issue a SECOND sell + duplicate trade.
- **Re-buy cooldown** (`_T212_REBUY_COOLDOWN_HOURS = 4`): don't churn back into a
  ticker just exited (the same story arrives via PRNewswire/BusinessWire/GlobeNewswire
  with different URLs → different signal keys → would otherwise re-buy).
- `recently_sold` persists with the portfolio (SQLite) and is pruned past the cooldown.
- **Ghost reconciliation** (`_reconcile_t212_ghosts`): each cycle, local positions that T212
  no longer reports — and that we didn't just sell (settlement window) — are treated as
  externally closed (corporate action: M&A cash-out, delisting, liquidation, ticker change),
  recorded as `ClosedTrade(exit_reason="corporate_action")` at the last cached price, removed,
  and a Telegram alert fires (check T212 for the exact realized P&L). Gated on a SUCCESSFUL
  positions fetch + sane account state so a transient API glitch can't close everything.

**T212 US + UK split (`trade-t212-uk` service, added 2026-05-28)**:
- `Trading212Client(market="us"|"uk")` — same client class drives both markets.
  US uses `_US_EQ` suffix (AAPL → AAPL_US_EQ), UK uses `L_EQ` (MKS.L → MKSL_EQ).
- Both services use ONE T212 API key + ONE T212 account + ONE cash pool. Demo
  balance ~£50k so cash starvation between services is irrelevant for now.
  When going live: revisit with explicit per-service allocation.
- **Bulkhead**: `Trading212Client.get_positions()` filters /equity/positions
  to this instance's market only. So US ghost-recon never sees (and never
  closes) UK positions, and vice versa. The match is tightened to require
  no underscore between ticker and suffix — necessary because foreign codes
  like `ASML_NL_EQ` end with `L_EQ` and would otherwise falsely match UK.
- **State isolation**: US state in `t212_portfolio.json` (service tag `t212`),
  UK state in `t212_uk_portfolio.json` (service tag `t212_uk`). Analytics,
  weekly report, and dashboard panels separate cleanly.
- **Telegram tags**: 🇺🇸 T212 (US), 🇬🇧 T212 (UK).
- **Dual-listing safety (VOD)**: VOD lists on both NASDAQ (VOD_US_EQ, USD ADR)
  and LSE (VODL_EQ, GBX primary). UK client strictly maps `VOD.L → VODL_EQ`
  and never resolves to the US ADR. Tested explicitly.

## Trading 212 API reference (cheat sheet)

Source of truth: `docs/vendor/t212-api.md` — verbatim copy of T212's official
skill manifest (`trading212-labs/agent-skills`). Refresh with
`python scripts/refresh_t212_docs.py`. Below is the cheat-sheet view of the
facts I keep needing when touching `broker_trading212.py` — it loads
automatically every session so I stop guessing rate-limit numbers and error codes.

### Base URLs
- DEMO: `https://demo.trading212.com/api/v0`
- LIVE: `https://live.trading212.com/api/v0`
- **API keys are environment-scoped** — a DEMO key only works on demo, LIVE only on live. A 401 usually means env mismatch.
- Only **Invest** and **Stocks ISA** account types are supported. **CFD is NOT exposed via this API** — any instrument that's CFD-only on T212 will refuse to trade through `/equity/orders/*` even though it appears in the catalogue.

### Per-endpoint rate limits (PER ACCOUNT, not per key/IP)
Both T212 services share one account, so the limit budget is shared too.

| Endpoint | Limit |
|---|---|
| `GET /equity/account/summary` | 1 req / 5s |
| `GET /equity/positions` | 1 req / 1s |
| `POST /equity/orders/market` | 50 req / min  (≈1.2s) |
| `POST /equity/orders/limit` | 1 req / 2s |
| `POST /equity/orders/stop` | 1 req / 2s |
| `POST /equity/orders/stop_limit` | 1 req / 2s |
| `GET /equity/orders` (by id) | 1 req / 1s |
| `DELETE /equity/orders/{id}` | 50 req / min |
| `GET /equity/metadata/instruments` | 1 req / 50s (~5 MB response) |
| `GET /equity/metadata/exchanges` | 1 req / 30s |
| `GET /equity/history/orders` | 50 req / min |
| `GET /equity/history/dividends` | 50 req / min |
| `GET /equity/history/transactions` | 50 req / min |
| `POST /equity/history/exports` | 1 req / 30s |
| `GET /equity/history/exports` | 1 req / min |

### Ticker / instrument format
Catalogue ticker = `{SYMBOL}_{EXCHANGE}_{TYPE}` — but in practice T212 uses these conventional suffixes:

| Market | Suffix | Example |
|---|---|---|
| US (NYSE / NASDAQ) | `_US_EQ` | `AAPL_US_EQ` |
| UK (LSE) | `L_EQ`  (NO underscore before L) | `MKSL_EQ`, `VODL_EQ`, `BARCL_EQ` |
| Germany (Xetra) | `_DE_EQ` | `SAP_DE_EQ` |
| France (Euronext Paris) | `_FR_EQ` | `MC_FR_EQ` |
| Netherlands (Euronext Amsterdam) | `_NL_EQ` | `ASML_NL_EQ` |

⚠️ **`ASML_NL_EQ` ends with `L_EQ`** — UK detection must require *no underscore* in the ticker portion (the bulkhead in `_matches_market` does this; tested).

### Instrument metadata (catalogue) — useful fields
`GET /equity/metadata/instruments` returns each instrument as:
- `ticker` — the ID above
- `name`, `shortName`, `isin`
- `currencyCode`
- `type` — **`STOCK | ETF | CRYPTOCURRENCY | FUTURES | INDEX | WARRANT | CVR | CORPACT`**
- `maxOpenQuantity`, `extendedHours`, `workingScheduleId`, `addedOn`

⚠️ **No `tradeable` / `disabled` flag exists.** The catalogue lists *known-to-T212* instruments, not *orderable* ones. The `type` field is our best preflight filter (`STOCK`/`ETF` are normally orderable; `CORPACT`/`WARRANT` may not be).

### Documented order errors
Only these are in the spec — anything else (e.g. `404 InstrumentNotFound` we saw for `CPI.L`) is undocumented and usually means "CFD-only on T212" or "instrument suspended":

| Code | Meaning |
|---|---|
| `InsufficientFreeForStocksBuy` | Not enough cash |
| `SellingEquityNotOwned` | Selling more than owned |
| `MarketClosed` | Outside trading hours |

### Order semantics
- `POST /equity/orders/market` — positive `quantity` = BUY, negative = SELL. Optional `extendedHours` for pre/after-hours.
- `POST /equity/orders/limit` — needs `limitPrice` + optional `timeValidity` (`DAY` / `GOOD_TILL_CANCEL`).
- `POST /equity/orders/stop` — needs `stopPrice`; fires market order on trigger.
- `POST /equity/orders/stop_limit` — needs both `stopPrice` and `limitPrice`.

---

**T212 client rate limiting (`broker_trading212.py`)**:
- T212's API is rate-limited PER ENDPOINT (not a flat req/s). The client throttles
  each endpoint to a conservative min interval (`_ENDPOINT_MIN_INTERVAL`: positions 1s,
  account/summary 5s, orders/market 1.3s, limit/stop/stop_limit 2s, instruments 50s,
  exchanges 30s; everything else `_DEFAULT_MIN_INTERVAL` 1s) via `_throttle()` — bursts
  get staggered automatically, so the loop doesn't have to manage spacing. (These match
  the cheat-sheet table above and are the source of truth in the code.)
- HTTP 429 is retried with `Retry-After` (or escalating 5/10/15/20s backoff), up to
  `_MAX_RETRIES_429 = 4`, then raises `T212RateLimitError` (a `T212OrderError` subclass,
  so existing handlers still catch it). Previously a 429 silently dropped that cycle's
  exit checks.
- Buy loop fetches `/equity/positions` ONCE after placing all orders (was one call per
  buy) to resolve fill prices — big reduction in burst calls on busy cycles.
- Practical poll floor: ~2s (positions endpoint is 1/1s and the loop makes 2 calls/poll).
  `_T212_EXIT_POLL_SECONDS = 60` has huge headroom; 15–30s is safe if tighter exits wanted.

### ADR-006: IBKR as broker (not Alpaca)
**Decision**: Interactive Brokers (IBKR) for live and paper trading. Alpaca removed from roadmap.
**Update 2026-06**: the Alpaca code + `trade` service were deleted entirely (operator is
UK-based and cannot use Alpaca). `broker_alpaca.py`, the `trade` CLI command, and
`run_loop_alpaca` are gone. When IBKR is built it gets a fresh `broker_ibkr.py`.
**Why**: (1) Alpaca is US-focused — UK residents face regulatory friction and limited support.
(2) IBKR has a UK entity (IBKR UK Ltd, FCA regulated), straightforward account opening for UK Ltd companies.
(3) IBKR supports API trading of US stocks from UK accounts natively.
(4) IBKR paper account uses live market data — best pre-live validation available.
(5) IBKR has no PDT rule issue for UK entities trading via a UK-registered broker.
**Tradeoff**: More complex integration than Alpaca (IB Gateway socket API vs REST). `ib_insync`
library mitigates this. IB Gateway needs to run as a sidecar container.
**When to revisit**: If IBKR API stability becomes a problem or a simpler UK-compatible REST broker emerges.

### ADR-007: 10-minute scan interval
**Decision**: Paper trader scans every 10 minutes (was 30 minutes initially).
**Why**: RSS feeds update frequently. Financial catalysts (upgrades, FDA, M&A) can move
stocks within minutes. 10 min is a balance between catching signals early and not
hammering yfinance/EDGAR rate limits.
**Tradeoff**: More scans = more API calls = higher chance of rate-limit hits. Acceptable
at current scale (13 detectors, ~20 RSS feeds).

---

## Performance Baselines (from backtest seeds — update with live data)

These are expected ranges. If a detector consistently falls below its floor, investigate
or disable. Update as live trades accumulate.

| Detector | Expected Win Rate | Avg Return | Notes |
|----------|-------------------|------------|-------|
| earnings_surprise | 60-70% | +1.5-3% | Strongest historical signal |
| analyst_upgrade | 55-65% | +1-2% | Top-tier firms score better |
| fda_decision | 60-75% | +3-8% | High variance — approvals vs rejections |
| mna_target | 70-85% | +5-15% | Targets gap to offer price; acquirers flat/down |
| guidance_raise | 55-65% | +1-3% | Full-year raises stronger than quarterly |
| dividend_surprise | 50-60% | +1-2% | Special dividends strongest; cuts are bearish |
| contract_win | 55-65% | +2-5% | Billion-dollar DoD contracts move most |
| index_inclusion | 65-75% | +3-8% | Passive flow forces buying over days |
| activist_13d | 60-70% | +3-7% | Icahn/Elliott best; small caps move more |
| ai_pivot | 50-60% | +1-3% | Noisy — many false positives in AI hype |
| buyback | 35-45% | +0-1% | WEAK — needs work or disable |
| insider_cluster | 55-65% | +2-4% | C-suite clusters strongest signal |
| spinoff | 55-65% | +2-5% | Announcement vs completion matters |

**Key metrics to track (Phase 1)**:
- Win rate per detector (target: >55% to keep enabled)
- Average return per trade vs stop-loss hit rate
- AI score correlation with actual outcome (does Haiku >0.7 = better trades?)
- Time-to-exit: are hold periods optimal or leaving money on table?

---

## Runbook

### Deploy new code to TrueNAS
From the Dockge stack dir, run the deploy script — it fetches compose.yaml, prunes the
build cache, builds the shared image once, recreates all four active services, and prints
a verification summary:
```bash
curl -sL https://raw.githubusercontent.com/alivebe-a11y/switching/main/scripts/deploy.sh | bash
```
Deploy a subset: `curl -sL .../scripts/deploy.sh | bash -s -- dashboard`
Deploy from a branch: `BRANCH=my-branch curl -sL .../scripts/deploy.sh | bash`

Manual equivalent (if not using the script):
```bash
curl -sL "https://raw.githubusercontent.com/alivebe-a11y/switching/main/docker-compose.yml" -o compose.yaml && docker builder prune -af && docker compose build paper-trade && docker compose up -d paper-trade paper-trade-uk trade-t212 dashboard
```
Why all four: `paper_trader.py` is shared by `paper-trade`, `paper-trade-uk`, and
`trade-t212`; `dashboard` runs `web.py` + `weekly_report.py`. Building only `paper-trade`
and restarting only it leaves the other three on the old image.

**Verify after deploy** — confirm every service is on the new image and freshly started:
```bash
docker compose ps                              # all Up, recent "Created" times
docker compose images                          # all show the SAME image ID
docker compose logs trade-t212 --tail 20       # expect "Poll at ..." + 60s exit polling
docker compose logs paper-trade-uk --tail 20   # expect LSE scan activity
```
If a service still shows an old "Created" time, it wasn't recreated — re-run its `up -d`.

### Rollback a broken deploy
```bash
docker compose down paper-trade
docker compose build --build-arg CACHEBUST=$(date +%s) paper-trade
# Or pin to a known-good commit by editing compose.yaml build context:
# context: https://github.com/alivebe-a11y/switching.git#<commit-sha>
docker compose up paper-trade -d
```

### Debug a detector not firing
1. **Check feeds**: `switching check-feeds` — are RSS feeds returning items?
2. **Check classify**: Run the classify function directly with a known-good headline:
   ```python
   from switching.detectors.<name> import classify
   print(classify("Known headline that should match", ""))
   ```
3. **Check ticker extraction**: RSS items need a ticker in the title or body.
4. **Check severity filter**: Is `min_severity` filtering it out?
5. **Check dedup**: Has this signal already been seen? (check `seen_signals` in portfolio JSON)
6. **For EDGAR detectors**: Is `SWITCHING_EDGAR_UA` set? Check with `echo $SWITCHING_EDGAR_UA`

### Run a backtest
```bash
switching backtest -d <detector> --from 2022-01-01 --to 2024-12-31 --hold-days 5
```
Add `--first-green` to test first-green exit. Add `--stop-loss 0.026` for stop-loss.
Events=12, Trades=0 means yfinance is blocked (expected in sandbox).

### Check container health
```bash
docker compose ps                    # Which services are running?
docker compose logs paper-trade --tail 50  # Recent scan output
docker compose logs dashboard --tail 20    # Dashboard errors
```

### Handle duplicate Telegram notifications
Old container still running alongside new one. Fix:
```bash
docker compose down paper-trade && docker compose up paper-trade -d
```

### Add a new RSS feed
1. Add URL to appropriate tuple in `src/switching/sources/rss.py`:
   - `DEFAULT_FEEDS` — general financial news
   - `EARNINGS_FEEDS` — earnings-specific
   - `CORPORATE_FEEDS` — corporate actions (M&A, buybacks, dividends)
2. Test: `switching check-feeds` — verify it returns items
3. No restart needed for live system (feeds re-fetched every scan cycle)

---

## Detector Template

Use this skeleton when building a new detector. Copy, rename, fill in regexes.

```python
"""<Name> detector.

<One paragraph explaining what this detects and why it moves stocks.>

Source: <RSS feeds / EDGAR forms / etc.>
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Iterable

from switching.detectors.base import Detector
from switching.registry import register
from switching.signal import Signal
from switching.sources import rss

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Core regexes
# ---------------------------------------------------------------------------

_PRIMARY_RX = re.compile(
    r"(?i)(?:"
    r"pattern_one"
    r"|pattern_two"
    r")"
)

# ---------------------------------------------------------------------------
# Detector class
# ---------------------------------------------------------------------------

@register
class <Name>Detector(Detector):
    name = "<snake_name>"
    description = "<One-line description for list-detectors output.>"

    def __init__(self, feeds: tuple[str, ...] | None = None) -> None:
        self._feeds = feeds

    def scan(self, since: datetime) -> Iterable[Signal]:
        feeds = self._feeds or (rss.DEFAULT_FEEDS + rss.CORPORATE_FEEDS)
        items = rss.fetch(feeds, since=since)
        classified = 0
        with_ticker = 0
        for item in items:
            match = classify(item.title, item.summary)
            if match is None:
                continue
            classified += 1
            ticker = item.extract_ticker()
            if not ticker:
                continue
            with_ticker += 1
            yield Signal(
                detector=self.name,
                ticker=ticker,
                company=_company_from_headline(item.title),
                event_dt=item.published,
                headline=item.title,
                url=item.url,
                evidence=match["evidence"],
                severity=match["severity"],
                extra={},  # Add detector-specific fields here
            )
        log.info(
            "%s: %d items, %d classified, %d with ticker",
            self.name, len(items), classified, with_ticker,
        )


def classify(title: str, summary: str = "") -> dict | None:
    """Return match metadata or None if no match."""
    text = f"{title}\n{summary}"

    primary_m = _PRIMARY_RX.search(text)
    if not primary_m:
        return None

    severity = 0.60  # Base severity
    # Add bonuses/penalties here
    severity = min(severity, 0.95)

    return {
        "severity": round(severity, 3),
        "evidence": _evidence_snippet(text, primary_m),
    }


def _evidence_snippet(text: str, *matches: re.Match | None) -> str:
    spans = sorted(m.span() for m in matches if m is not None)
    if not spans:
        return text[:160].strip()
    start = max(0, spans[0][0] - 40)
    end = min(len(text), spans[-1][1] + 60)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _company_from_headline(title: str) -> str:
    """Best-effort company name extraction."""
    return title.split(" ")[0]  # Replace with proper extraction
```

### Test template
```python
"""Tests for the <name> detector."""

from switching.detectors.<name> import classify


def test_positive_match():
    m = classify("Headline that should match", "")
    assert m is not None
    assert m["severity"] >= 0.60


def test_rejects_unrelated():
    assert classify("Apple launches new MacBook Pro lineup", "") is None


def test_severity_capped():
    m = classify("Strong match with all bonuses", "Extra context.")
    assert m is not None
    assert m["severity"] <= 0.95
```

### Seed CSV template
```csv
event_dt,ticker,company,headline,url,evidence,severity
2022-01-15,AAPL,Apple,Headline text here,,Evidence snippet,0.70
```

---

## Roadmap

### Phase 1 — Prove the Strategy (Now → Month 3)
- [ ] Collect 50+ live trades with AI scores attached
- [ ] Compare Haiku predictions vs actual outcomes
- [ ] Tune exit profiles based on live data (not just backtest seeds)
- [ ] Enable AI filter gating once score threshold is validated
- [ ] Improve buyback detector (36% win rate — needs work or disable)
- [ ] Build Form 4 XML parser for insider_cluster (currently stub)

### ⛔ Pre-live safety gate (BLOCKERS — must ship before ANY real money)
Do **not** flip `T212_DEMO=false` / fund a live account until ALL of these exist.
These are crash/failure protections; the demo can run without them, real money cannot.

- [ ] **1. Market kill-switch / drawdown halt (MIN).** Auto-pause *opening new positions*
  when (a) daily/portfolio P&L drops below a threshold, or (b) a risk-off regime is
  detected (e.g. VIX or index % move). Exits ALWAYS remain enabled — you must always be
  able to get out. Without this the bot can churn capital into a falling market.
- [ ] **2. Per-detector / per-feed circuit breaker (MIN).** Trip a detector/feed/API after
  N consecutive empty-or-failed scans → stop calling it, fire ONE Telegram alert, retry
  after a cooldown (half-open), auto-recover + alert on success. Catches dead feeds loudly
  (the UK feeds were silently dead for a week). Closes the Release It! gap (see "Stability
  rules"). Already half-noted as "disable detector after N empty scans" under Infrastructure.
- [ ] **3. Manual Telegram STOP command (MIN).** Operator kill-switch: send `/stop` (and
  `/resume`) to the bot to halt new buys immediately — for "RSS failing AND market tanking"
  moments. Requires the bot to *read* Telegram (it currently only sends): poll `getUpdates`
  (or a flag file the dashboard/Telegram writes), loops check a `trading_enabled` flag
  before buying; exits always allowed. Persist the flag so a restart doesn't silently resume.
- [ ] **4. Stop-fill realism check.** The paper trader snaps stop-loss fills to the stop
  price (optimistic); real fills gap through in a crash. Add paper-vs-T212 stop-loss
  slippage to the weekly report so the true crash cost is visible before trusting it live.

### Phase 2 — Scale to Real Capital (Month 3-6)
- [ ] Set up UK Ltd company for tax efficiency (25% vs 40%)
- [ ] Fund with £5-10K own savings
- [ ] **IBKR paper trading integration** (see below — do this 4-6 weeks before going live)
- [ ] Add Polygon.io (~$30/month) for real-time price data (or use IBKR market data subscription)
- [ ] Claim business expenses (internet, electricity, hardware, APIs)

#### IBKR Paper Trading — Implementation Plan
Decided against Alpaca (US-only, regulatory friction for UK). IBKR is the chosen broker.
IBKR paper account uses live market data with simulated fills — best pre-live validation.

**Architecture**:
```
[paper-trade container]
        ↕ TCP :4002 (paper) / :4001 (live)
[IB Gateway container + ibc auto-login]  ←→  IBKR servers
```

**What to build** (`src/switching/broker_ibkr.py`):
- Implement a broker interface: `buy_market`, `sell_all`, `get_quote`, `is_market_open`
  (the same shape the removed `broker_alpaca.py` had — mirror `broker_trading212.py`)
- Use `ib_insync` Python library (cleaner than official `ibapi`)
- Controlled by env var `IBKR_PAPER=true` (port 4002) vs `IBKR_PAPER=false` (port 4001)
- Paper trader falls back to internal simulation if IB Gateway unreachable

**Two-phase upgrade**:
1. **Order execution only** — submit orders to IBKR paper, keep yfinance for prices
   - Validates fills, spreads, partial fills on small caps
2. **Live price quotes too** — replace yfinance in `check_exits()` with IBKR L1 streaming
   - Matters most for peak_trailing (1-second polling — IBKR ticks are more reliable than yfinance)

**IB Gateway Docker**:
- Use `ghcr.io/gnzsnz/ib-gateway` image (maintained, includes `ibc` auto-login)
- Add to `compose.yaml` as a new service alongside `paper-trade`
- Env vars needed: `IBKR_USERNAME`, `IBKR_PASSWORD`, `TRADING_MODE=paper`
- Session auto-renews daily via `ibc` (avoids the 24-hour expiry problem)

**New env vars to add to Dockge .env when ready**:
- `IBKR_USERNAME` — IBKR account username
- `IBKR_PASSWORD` — IBKR account password
- `IBKR_PAPER` — `true` for paper, `false` for live (default: `true`)
- `IBKR_HOST` — IB Gateway hostname (default: `ib-gateway`)
- `IBKR_PORT` — 4002 (paper) or 4001 (live)

**W-8BEN-E**: UK Ltd company needs to file W-8BEN-E with IBKR to claim UK-US tax treaty
rate on US dividends (15% vs 30% default withholding). Do this at account opening.

### Phase 3 — Options Trading (Month 6-9)
- [ ] Historical options chain data (Polygon.io options add-on ~$200/month)
- [ ] Options backtester (Black-Scholes or historical chains)
- [ ] Strike/expiry selection logic
- [ ] Options only on high-conviction detectors (earnings_surprise, index_inclusion)
- [ ] Theta decay early-exit rules

### Phase 4 — Scale & Harden (Month 9-12)
- [ ] Scale to £30K capital
- [ ] Multi-container architecture (VPN for rate limit distribution)
- [ ] UK market support (LSE, RNS feeds, FCA filings)
- [ ] Other markets (EU, Asia — evaluate per-market)

### Infrastructure
- [ ] Failover / HA: secondary machine (VPS or second NAS) monitors primary heartbeat, takes over if offline. State sync via shared volume or rsync. Alert on failover via Telegram.
- [ ] VPS deployment for uptime (keep TrueNAS as primary, VPS as failover)
- [ ] Automated backup of trade state and memory files
- [ ] Circuit breaker: disable detector after N consecutive empty scans
- [ ] Health dashboard: feed status, scan counts, API latency

### AI Improvements
- [ ] Turn on Haiku filter gating (after 50+ scored trades)
- [ ] Upgrade to Sonnet for complex signals (earnings + guidance + sentiment)
- [ ] Memory palace: cross-detector learning (e.g. "NVDA responds well to AI pivot + earnings combo")
- [ ] Sentiment analysis on headline text beyond regex
- [ ] Claude API integration for adaptive strategy tuning

### Data Sources (when capital justifies cost)
- [ ] Polygon.io real-time + options ($30-200/month)
- [ ] Tiingo Pro for cleaner fundamentals ($30/month)
- [ ] S&P Capital IQ (£15-25K/year — only at £100K+ capital)
- [ ] Bloomberg Terminal (£24K/year — only if running a fund)

### Triggered / Deferred items
Items intentionally NOT built yet. **On every roadmap scan, re-check each item's trigger
conditions; if ALL are met, raise it to the user as ready to build.**

#### Corporate-actions feed ingestion  (DEFERRED — discussed 2026-05-25)
Proactively map ticker changes and pre-empt splits/delistings/M&A from a corporate-actions
source, instead of only reacting after the fact.

**Build ONLY when ALL of these are true:**
- [ ] Running **real capital** (T212_DEMO=false or live IBKR) — exact realized price and
      holding-through-a-rename actually move the needle then; on demo they don't.
- [ ] Hold periods materially **longer than ~5 days** — positions then span more corporate
      actions, so tracking through them matters.
- [ ] A **structured** corporate-actions data source is available (vendor feed/API with
      lead time). The T212 monthly community post is free-text + retrospective — NOT
      suitable to auto-act on (parse errors could corrupt good positions).
- [ ] Ghost-position reconciliation proves **insufficient** — i.e. you observe material
      P&L misattribution or missed re-entries that the reactive fix doesn't cover.

**Why deferred** (see discussion 2026-05-25): with a 3-5 day demo strategy fed by a monthly
forum scrape, ROI is poor and auto-mutating positions on scraped text adds real risk. T212
already split-adjusts `unrealized_pnl_pct`/`avg_entry_price`, and M&A/delistings END the
trade anyway (the cash-out IS the exit). The reactive **ghost-position reconciliation**
(DONE — `_reconcile_t212_ghosts` in paper_trader.py) already handles the painful 80%:
positions T212 closes externally are recorded as `corporate_action` and removed so they
don't ghost, block re-buys, or skew analytics.

#### Primary-source / low-latency news ingestion — UK + US  (DEFERRED — discussed 2026-05-25)
**The edge question: "who gets the news first?"** Today's pipeline is *downstream* of the
primary source on both markets, which costs latency (and on UK, completeness):
- **UK**: all the old RNS feeds died (Investegate dropped RSS, Reuters killed RSS, Proactive
  404). Current UK source is **Google News RSS** (`UK_FEEDS`) — the *laggiest* tier (indexes
  journalist write-ups of RNS, not raw RNS) with patchy ticker coverage. The PRIMARY source is
  the **LSE RNS service** (what companies file to first), surfaced by `londonstockexchange.com`
  via its `api.londonstockexchange.com` JSON API (the page is a JS SPA — no HTML/RSS).
- **US**: PR Newswire / BusinessWire / GlobeNewswire are close to primary (companies issue
  releases through them) + SEC EDGAR for filings — decent, but worth assessing tighter latency
  / direct exchange feeds when capital justifies it.

**Two parts:** (1) a **UK ticker resolver** — an FTSE 350 company-name → `.L` EPIC map (the LSE
equivalent of the US SEC `ticker_lookup.py`), so ANY feed resolves tickers without relying on
parenthesised codes; (2) wire a **primary/low-latency source** per market (UK = LSE RNS API
direct or a real RNS aggregator with a feed/API; US = evaluate direct exchange/wire latency).

**Build when ANY of:**
- [ ] UK shows real signal volume worth investing in (i.e. the Google-News probe produces
      enough tradeable UK signals/week to bother — check the Saturday report).
- [ ] Moving to **real capital** on either market — latency and completeness start to pay.
- [ ] News-latency is identified as a measurable edge loss (signals firing materially later
      than price has already moved).

**Risks / notes:** the LSE API is undocumented + reverse-engineered (fragile, ToS-grey) — treat
as a real integration, not a quick scrape. Until triggered, the Google-News probe (DONE) keeps
UK flow alive so we can judge whether UK is worth the deeper build at all.

**Probe findings (2026-06-09) — viable but DELIBERATELY NOT PURSUED:**
- Confirmed `londonstockexchange.com/news` is a pure **JS SPA** — a plain HTTP fetch returns an
  empty shell (no server-rendered content), so the Investegate-style BeautifulSoup approach
  gets nothing. The content is loaded client-side from `api.londonstockexchange.com`.
- Probed that API host directly: `GET /` → 404 from plain **nginx** (no Cloudflare/Akamai/PoW
  anti-bot wall, unlike Stooq), and `POST /api/v1/components/refresh` → **200 `application/json`**
  (returned `[]` only because the `componentId`/`parameters` payload was a guess). So a *free
  interim* UK RNS reader is **technically viable** — the API answers JSON without a fight.
- The exact working payload (GUID `componentId` + params) is not guessable; capturing it needs
  a real-browser network-tab read (e.g. Claude-for-Chrome), then replay headlessly with plain
  `requests.post` (no browser at runtime).
- **DECISION: not building this now — respect LSE ToS, don't hammer/reverse-engineer their
  endpoint.** Two acceptable future paths when UK earns the spend: (a) the **official paid RNS
  Data Feed** — REST Announcement API (pull) + WebSocket (real-time push), TLS 1.2, authenticated
  (`docs.londonstockexchange.com` RNS Data Feed spec) — the robust, ToS-clean answer; or (b) a
  licensed RNS aggregator. The reverse-engineered route stays a documented *last resort*, not the
  plan. Net: the deferred trigger is unchanged, but we now KNOW a free path exists if ever needed.

#### Social-sentiment signal — Reddit / StockTwits  (DEFERRED — discussed 2026-05-30)
Use retail social chatter (r/wallstreetbets, r/stocks, r/smallstreetbets, StockTwits) as a
trading input. Cashtags (`$TSLA`) give clean tickers, sidestepping the news ticker-resolution
hole. The two directions are NOT mutually exclusive — they converge on ONE rolling per-ticker
`{mention_volume, sentiment, velocity}` store that the buy pipeline consults two ways:

- **Detector → Reddit (confirmation / conviction) — BUILD THIS FIRST.** A news catalyst stays
  the trigger; the Reddit score only sizes the position up/down via the existing
  `_position_weight` model. Safe by construction — Reddit can never *initiate* a trade, only
  modulate one a catalyst already justified. Ship it LOG-ONLY first (like the Haiku filter):
  record each signal's Reddit score, measure whether high-sentiment catalysts actually
  outperform, THEN gate. Lowest risk, reuses the whole pipeline. Caveat: positive sentiment on
  an already-public catalyst can mean "already crowded / priced in" — sometimes a fade, not a
  confirm. Measure before trusting.
- **Reddit → detector (discovery / priming) — PHASE 2, only if the data earns it.** A Reddit
  mention-volume / sentiment spike puts a ticker on a short-lived watchlist and raises its
  conviction if a catalyst then fires (the operator's "watch the trend, then react when the
  detector goes off"). This is the higher-*information* direction — it can surface names with
  no press release and occasionally leads the wire (small-cap / biotech / meme). BUT it's the
  noisiest, most-manipulated entry (pumps, bots, coordinated posts), "trending" usually means
  it ALREADY moved (you enter mid-move), and a *pure* social trigger is a different strategy
  (crowd reflexivity) from the bot's proven catalyst-momentum thesis. Build only after the
  confirmation direction proves Reddit sentiment correlates with outcomes.

**Credibility layer (what makes this viable vs pure noise).** Capture per post: `author`,
ticker(s), stance+confidence, engagement (score/comments), subreddit, post_type, timestamp;
and per author: account age/karma, cadence, and a running CALL HISTORY (ticker, stance, time)
joined to forward returns. Derive a per-author **trust score** — the `trade_memory.py` pattern
applied to people ("user X: +4% avg over 26 bullish calls" vs "user Y: noise over 40") — and
weight each post's sentiment by its author's trust BEFORE it touches conviction sizing.
**Bot/manipulation flags** collapse trust toward zero: young low-karma accounts, burst cadence,
duplicate text across accounts, clusters of fresh accounts pushing one ticker. Caveats: thin
samples for the long tail (only frequent posters score reliably), the reputation score becomes
a gaming target (patient aged accounts), and storing usernames has ToS/data considerations
(public data, but don't publish dossiers).

**Validate by BACKTEST first (kills the cold-start).** Don't blind-collect for months —
reconstruct history. Reddit posts are timestamped (`created_utc`), so (author, ticker, stance,
time) + price history = retrospective forward returns AND pre-seeded trust scores. Reuses the
existing `backtest.py` + `sources/historical.py` scaffold.
- **Data:** the live Reddit API can't reach deep history (shallow, recency-biased) and the live
  Pushshift endpoint was cut off post-2023; use the **Pushshift data DUMPS** (downloadable
  monthly r/wsb etc. archives, with timestamps). Confirm current availability before committing.
- **Backtest traps (social backtests run OPTIMISTIC — discount results):** survivorship/deletion
  bias (removed pump posts + banned accounts are gone — exactly the noise you need to study);
  point-in-time engagement (final upvote counts are look-ahead — use the score at decision time);
  regime dependence (2021 meme mania ≠ now — test out-of-sample); delisted-ticker price
  survivorship (dead microcaps missing from yfinance flatter returns); same-code rule (extract/
  score history with the EXACT live pipeline, or the backtest is fiction).
- **Payoff:** a weekend backtest answers "is there ANY credibility-weighted edge?" before any
  live infra. No edge ⇒ walk away (months saved). Edge ⇒ launch with trust scores **seeded from
  history** (no cold start), then run live LOG-ONLY as the true out-of-sample confirmation.

**Architecture:** one `sources/social.py` client (PRAW / StockTwits API) for live + a Pushshift-
dump loader for the backtest, TTL-cached, keeping the rolling per-ticker store; scored by the
already-wired Haiku (or VADER/FinBERT). New source integration = days (auth, rate limits, NLP),
not a half-day regex detector — a real sub-project.

**⛔ BACKTEST GATE RESULT (2026-06 — FAILED, do not build the long/discovery side):**
Ran the mention-momentum backtest over 29.8M WSB items (2024-01→2026-04), 8,063 spike-days,
**6,325 usable spike events** priced via yfinance (`scripts/reddit_momentum_backtest.py`).
Buying the next open after a WSB mention spike is **negative at every horizon** (+1d −0.5%,
+5d −2.8%, +10d −3.4%; win rate ~35%) and **−12.5% excess vs the same-tickers baseline**
(+9.7%). Mean 10-day peak +19% / trough −16% = violently volatile, drifts DOWN. **Verdict:
WSB spikes are a FADE, not a buy — by the time it trends you're late.** This kills the
"Reddit → discovery (buy trending)" direction and dents the "confirmation overlay" (buzz =
crowded = priced in). The credibility-weighted variant is untested, but it'd start from a
−12.5% base-rate hole. Gate did its job: months of infra saved.

**USABLE NOW (long-only, no new infra):** invert it into a **defensive filter** — if a detector
fires on a stock that's *also* a current WSB mention-spike, treat it as crowded/late → skip or
downsize. Negative-confirmation overlay; the safe payoff of this whole exercise.

**PHASE-3 LEAD (keep, re-validate — defined-risk PUTS only, NEVER naked shorts):** the fade is a
short candidate, but shorting meme names = squeeze/unlimited risk, and the backtest HIDES the
worst squeezes (survivorship: deleted/banned/delisted gone). Before trusting it at Phase 3:
re-test with put premiums (brutal meme IV) + borrow/slippage modelled, a liquid-optionable-only
filter, and fresh out-of-sample data. Most names won't clear the option premium. See "crash alpha".

**Original build gate (kept for reference — the FIRST item is now answered FAILED above):**
- [ ] **Backtest gate (cheapest, do first).** A historical backtest off the Pushshift dumps
      shows credibility-weighted sentiment predicts forward returns OUT-OF-SAMPLE. No edge here
      ⇒ stop, months saved.
- [ ] Core catalyst strategy is PROVEN (50+ live trades/detector; today ~95% of P&L rests on
      ~3 trades) — don't layer a noisy input on an unproven base.
- [ ] Live LOG-ONLY run reproduces the backtest edge forward (true out-of-sample) before any
      sentiment weight touches sizing.
- [ ] Real capital, or an identified edge-loss reason, justifies the source-integration cost.

**Risks:** social sentiment is the most manipulated signal there is and is arguably the OPPOSITE
of this bot's "who-gets-the-news-first" edge (by the time it trends, it's priced in). Frame as a
conviction *overlay*, not a core feature. Supersedes the vaguer "sentiment analysis" AI-improvements bullet.

### Detector Ideas
- [ ] stock_split — splits often run up beforehand
- [ ] crypto_treasury — Bitcoin treasury announcements (MicroStrategy pattern)
- [ ] geopolitical — oil/defence/shipping on geopolitical events (Strait of Hormuz etc.)
- [ ] day_trading — intraday momentum signals (separate project likely)
- [ ] **uk_activist_holdings** — UK equivalent of `activist_13d`. Source: RNS
  "Holding(s) in Company" announcements (TR-1 / DTR5 disclosures, already in
  the Investegate scraper). Trigger fires when a known-activist name (Elliott,
  ValueAct, Cevian, Pelham Capital, Crystal Amber, Gatemore, Asset Value
  Investors, Boaz Weinstein/Saba) crosses 3% / 5% / 10% ownership. FCA
  threshold is **3%** (vs SEC's 5%) and notification is within **2 trading days**
  (vs SEC's 10) — meaningfully faster signal than US 13D. Build when UK
  paper-trade produces enough flow to evaluate the edge.
- [ ] **uk_insider_cluster** — UK equivalent of `insider_cluster`. Source: same
  RNS "Director/PDMR Shareholding" stream that `uk_director_dealing` already
  reads (MAR Article 19 — PDMRs must notify of transactions ≥€5,000 within 3
  business days). Cluster logic: multiple PDMRs buying same ticker within an
  N-day window. Sits ON TOP of `uk_director_dealing` (which already fires on
  single dealings). Build when UK flow merits it.

### Exploration: profit from market falls ("crash alpha") — paper only
The engine is **long-only momentum on bullish catalysts**; it can't currently profit from
a drop. Options, cleanest-fit first for THIS architecture:
- [ ] **Inverse ETFs on a risk-off regime** (best fit) — when the market-regime signal
      (same one as pre-live gate #1) flips risk-off, *buy* an inverse ETF (-1x, e.g. SH /
      a UK equivalent). Mechanically a "buy", so it slots into the existing long-only loop —
      no shorting/margin. CAVEAT: only -1x for holds >1 day; leveraged inverse (SQQQ -3x)
      decays from daily rebalancing — never hold those.
- [ ] **Bearish-catalyst detector** — mirror of the bullish detectors (profit warnings,
      guidance cuts, going-concern, fraud, mass downgrades) → feeds shorts or puts.
- [ ] **CFD leverage on the best-performing detector** (long-horizon, hard-gated) — NOT
      for shorting/crash plays; the idea is to *amplify* the proven winner (e.g.
      guidance_raise) with modest leverage. ⚠️ Only after ALL of: (a) the detector's edge
      is proven over 50-100+ trades across conditions (today the whole P&L rests on ~3
      trades — SIBN alone is 57%, so it's NOT proven); (b) live on real money UNLEVERAGED
      first; (c) modest leverage only (≤2x), leverage-aware stops, gap-risk accounted.
      Note: **conviction sizing (`_position_weight`) is the UNLEVERAGED version of this and
      already exists** — use it to lean into the winner until leverage is justified.
      Costs/risks: daily financing erodes a +4%-avg edge, leverage amplifies the 37%
      losers and gap-downs identically, different account + tax treatment (no ISA). Puts
      are Phase 3 (options). Defer — a long way off.
- [ ] **Shorting / puts for downside** — true short exposure (CFDs/puts): margin, borrow
      cost, large/unlimited risk, regulatory friction. Phase 3+. Defer.
**Honest framing:** crash-timing is hard (sharp, brief, whipsaw-prone) and is a *different
edge* from bullish-catalyst momentum. Worth trying on the paper trader, NOT a core feature.
Naturally pairs with the regime filter we need for the kill-switch anyway.

### Completed
- [x] 16 detectors live: ai_pivot, earnings_surprise, buyback, activist_13d, insider_cluster, index_inclusion, spinoff, analyst_upgrade, fda_decision, mna_target, guidance_raise, dividend_surprise, contract_win, stock_split, crypto_treasury, uk_director_dealing
- [x] Paper trading on TrueNAS via Docker (Dockge), 10-minute scan interval
- [x] Trade memory — per-detector/per-price-tier/per-exit-reason stats
- [x] Haiku AI scoring (log-only mode, $0.30/month)
- [x] Telegram notifications (buy/sell/skip/daily summary/startup)
- [x] 2.6% tiered stop-loss with detector-specific exit profiles
- [x] Flask web dashboard (portfolio, trades, signals, equity curve)
- [x] SEC EDGAR integration (13D filings, Form 4, CIK→ticker mapping)
- [x] CORPORATE_FEEDS added for buyback/spinoff/index/mna/guidance/dividend/contract detectors
- [x] Seed CSVs for all 13 detectors (12 events each for backtesting)
- [x] Security audit — no secrets in public repo, .gitignore covers .env/keys/state
- [x] check-feeds diagnostic command
- [x] dividend_surprise detector (was on roadmap as idea — now built)
- [x] fda_decision detector (was on roadmap as FDA_approval idea — now built)
- [x] Diagnostic logging: all 11 RSS detectors log items/classified/with_ticker per scan
- [x] SEC company-name-to-ticker fallback (sources/ticker_lookup.py) — fixes empty dashboard signals
- [x] Post-exit price tracker (exit_tracker.py) — 20-day post-close monitoring for detector refinement
- [x] Dashboard "Post-Exit Tracker" panel with per-detector insights
- [x] Telegram buy notifications batched every 2 hours (digest format) — sells/stops still immediate
- [x] Dashboard reads cached prices from portfolio JSON — no live yfinance polling per page load
- [x] SQL schema mapping doc (`docs/SQL_SCHEMA.md`) — forward plan, JSON stays for now
- [x] Skipped-signal tracker (`skipped_tracker.py`) + dashboard panel — when a signal is skipped (max-positions / insufficient-cash), record it and run same exit logic for would-have-been P&L
- [x] stock_split detector — forward split announcements, +1.5% first-green, 4-day hold
- [x] crypto_treasury detector — Bitcoin treasury adoption (MicroStrategy-style), +3% first-green, 3-day hold
- [x] Analytics tab in dashboard — Exit Profile Tuning, Signal Severity correlation, Peak Trailing summary
- [x] Weekly Saturday report (`weekly_report.py`) — auto-fires every Saturday at 09:00 UTC; covers detector rankings, T212 vs paper slippage, skipped-signal opportunity cost, data-driven improvement suggestions. Manual trigger: `switching weekly-report`
- [x] Signal dedup bug fixed — `_signal_key` now URL-based (not date-based), so undated RSS articles can't re-fire daily
- [x] T212 orphan position fix — positions in T212 with no local tracker (e.g. manually
  bought in the app, or pre-existing) get a synthetic record on reconciliation (no longer
  auto-sold on first profitable cycle). Exit profile `t212_orphan` (2026-06): keeps the
  generic tiered stop, but **rides** winners (flip to peak-trailing at +2%, 3% trail) with
  a **10-day** backstop — treats intentional buys as managed holds, not break-even scratches.
  (NOTE: still *managed* — the bot will exit them on stop/trail/10-day. A true never-touch
  hold would need a separate exclusion list, not built.)
- [x] SQLite storage (`storage.py`) — one `switching.db`, `service` column (us/uk/t212); US/UK no longer share/clobber JSON; T212 now collects full analytics; auto-migration of legacy JSON with `scripts/migrate_to_sqlite.py` validation
- [x] T212 rate limiting — per-endpoint throttle + 429 Retry-After backoff; batched post-buy position fetch
- [x] T212 ghost-position reconciliation (`_reconcile_t212_ghosts`) — positions T212 closes externally (corporate action: M&A cash-out, delisting, liquidation, ticker change) are recorded as `corporate_action` and removed, so they don't ghost forever, block re-buys, or skew analytics
- [x] `severity` stored on `ClosedTrade` — enables signal quality ↔ outcome correlation analysis
