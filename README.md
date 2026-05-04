# switching

A stock signal detection and paper-trading system that scans RSS feeds and
SEC EDGAR filings for corporate events, scores signals, and paper-trades at
next-day open with detector-specific exit profiles.

`switching` is built around a pluggable detector framework:

1. **Scans** public data sources (press-release RSS, SEC filings) for events
   matching registered **detectors** (13 live),
2. **Scores** each signal with regex classifiers + optional Claude Haiku AI scoring,
3. **Paper-trades** automatically with tiered stop-losses and first-green exits, and
4. **Backtests** detectors against curated historical events for win rate validation.

> **Disclaimer.** This is a research and paper-trading tool, not investment
> advice. Past patterns do not predict future returns.

## Detectors

13 detectors live, each plugging into the same scan/score/trade pipeline:

| Detector | Source | Thesis |
|---|---|---|
| `earnings_surprise` | RSS earnings feeds | EPS beats and misses; reports + raises |
| `analyst_upgrade` | RSS default + earnings | Analyst rating changes, price targets |
| `fda_decision` | RSS default + corporate | FDA approvals, CRLs, breakthrough/fast-track |
| `mna_target` | RSS default + corporate | Acquisition announcements (target gaps to offer) |
| `guidance_raise` | RSS default + earnings + corporate | Mid-quarter guidance raises / cuts |
| `dividend_surprise` | RSS default + earnings + corporate | Special dividends, initiations, increases, cuts |
| `contract_win` | RSS default + corporate | Government / DoD contract awards |
| `buyback` | RSS default + corporate | Board-authorized share repurchase programs |
| `index_inclusion` | RSS default + corporate | S&P 500 / Russell 1000 additions |
| `spinoff` | RSS default + corporate | Spinoffs, split-offs, carve-outs |
| `ai_pivot` | RSS default | AI rebrands and pivots |
| `activist_13d` | SEC EDGAR (13D) | Activist investor 5%+ stakes |
| `insider_cluster` | SEC EDGAR (Form 4) | ≥3 insiders buying within 30 days |

Each detector is a single file under `src/switching/detectors/`. The shared
framework handles RSS fetching, ticker resolution, price correlation,
ranking, paper-trading, and backtesting.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## CLI

```bash
# List all 13 registered detectors.
switching list-detectors

# Scan the last 7 days across all detectors; write JSON + CSV.
switching scan --since 7d --json out.json --csv out.csv

# Or scan one detector specifically.
switching scan --since 7d --detector earnings_surprise

# Backtest a detector against seeded historical events.
switching backtest --detector mna_target --from 2023-01-01 --to 2024-12-31 \
  --hold-days 5 --cost-bps 10 --csv trades.csv

# Run the paper trader (continuous loop, scans every 10 minutes).
switching paper-trade --seed 1000 --interval 10 --stop-loss 0.026 --hold-days 5

# Launch the Flask web dashboard.
switching web --port 8080

# Diagnostic: check which RSS feeds are returning items.
switching check-feeds
```

The backtest prints a summary table:

```
Backtest — ai_pivot (hold=5d, events=10, trades=10)
┌───────────────────────────┬──────────┐
│ Metric                    │    Value │
├───────────────────────────┼──────────┤
│ Win rate                  │    60.0% │
│ Avg return / trade        │   +2.14% │
│ Sharpe (approx)           │     1.42 │
│ Max drawdown              │   -4.80% │
└───────────────────────────┴──────────┘
```

## Architecture

```
┌─────────────┐   ┌──────────────┐   ┌──────────────────┐   ┌──────────────┐
│  Detectors  │ → │  Signal[]    │ → │ Price correlator │ → │ Reporter     │
│ (13 live)   │   │ (dataclass)  │   │   (yfinance)     │   │ CLI/JSON/CSV │
└─────────────┘   └──────────────┘   └──────────────────┘   └──────────────┘
       │                                    │
       │             ┌──────────────────────┘
       ▼             ▼                      ▼
┌───────────────────┐ ┌────────────────────┐ ┌────────────────────┐
│ Paper trader      │ │ Backtester         │ │ Flask dashboard    │
│ next-day-open buy │ │ historical replay  │ │ portfolio + trades │
│ first-green exits │ │ win rate, Sharpe   │ │ equity curve       │
│ tiered stop-loss  │ │                    │ │                    │
└───────────────────┘ └────────────────────┘ └────────────────────┘
```

- **`signal.py`** — `Signal` + `PriceReaction` dataclasses. Detectors emit
  these; price correlation, paper-trading, and reporting consume them.
- **`registry.py`** — `@register` decorator that makes new detectors drop-in.
- **`pricing.py`** — thin `yfinance` wrapper with a local SQLite cache so
  reruns are cheap. Computes same-day and N-day returns.
- **`paper_trader.py`** — continuous trading loop, position management,
  detector-specific exit profiles, Telegram notifications.
- **`web.py`** — Flask dashboard rendering portfolio, trades, signals, and
  equity curve.
- **`trade_memory.py`** — per-detector / per-price-tier / per-exit-reason
  performance stats from closed trades.
- **`ai_filter.py`** — Claude Haiku scoring (0-1) for signals (log-only mode).
- **`notifications.py`** — Telegram push alerts for buys, sells, skips, daily
  summary, and startup.
- **`sources/rss.py`** — RSS feed lists (`DEFAULT_FEEDS`, `EARNINGS_FEEDS`,
  `CORPORATE_FEEDS`); `FeedItem.extract_ticker()` with two-stage resolution.
- **`sources/sec_edgar.py`** — rate-limited SEC EDGAR client for 13D / Form 4.
- **`sources/ticker_lookup.py`** — SEC `company_tickers.json` (~13K mappings)
  for company-name → ticker fallback when headlines lack `NASDAQ:AAPL` prefixes.
- **`sources/historical.py`** — Seed CSV loader with optional live-EDGAR augmentation.
- **`backtest.py`** — replays signals through a next-day-open / N-day-close
  rule with configurable transaction-cost and stop-loss assumptions.
- **`reporter.py`** + **`cli.py`** — `rich` tables, JSON, CSV; Typer CLI.

## Adding a new detector

See `CLAUDE.md` for the full checklist, ADRs, runbook, and detector template.
Quick version:

1. Create `src/switching/detectors/<name>.py` with a `@register` class.
2. Add a seed CSV to `src/switching/data/historical_events/<name>.csv`.
3. Import + register in `registry.py` → `load_builtin_detectors()`.
4. Add an exit profile in `paper_trader.py` → `_exit_profile()`.
5. Add to `_DEFAULT_DETECTORS` in `cli.py`.
6. Create `tests/detectors/test_<name>.py`.
7. Run `pytest tests/` — must stay green.

## Deployment (TrueNAS via Dockge)

Docker build context pulls directly from GitHub — no local clone on TrueNAS:

```bash
curl -sL "https://raw.githubusercontent.com/alivebe-a11y/switching/claude/add-ai-recommendations-ABZZX/docker-compose.yml" -o compose.yaml \
  && docker compose build --no-cache paper-trade \
  && docker compose down paper-trade \
  && docker compose up paper-trade -d
```

Required environment variables in Dockge `.env`:

- `SWITCHING_EDGAR_UA` — descriptive User-Agent for SEC EDGAR
- `ANTHROPIC_API_KEY` — Claude Haiku for AI signal scoring (optional)
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — push notifications (optional)

## Development

```bash
pytest
```

304 tests, all offline — pricing, backtest, RSS, and EDGAR tests use in-memory
fixtures. Live yfinance / RSS / SEC calls only happen when running
`switching scan`, `switching backtest`, or `switching paper-trade` against
real data.

## Roadmap

See `ROADMAP.md` for the full phased plan. Highlights:

- **Phase 1** (Now → Month 3): Collect 50+ live trades, validate Haiku
  predictions, tune exit profiles, improve buyback win rate.
- **Phase 2** (Month 3-6): UK Ltd company, scale to £5-10K real capital,
  Alpaca live trading, Polygon.io real-time data.
- **Phase 3** (Month 6-9): Options trading on high-conviction detectors.
- **Phase 4** (Month 9-12): Scale to £30K, multi-container HA, UK markets.

## License

MIT.
