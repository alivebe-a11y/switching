# switching

A pluggable trend-detection framework for corporate-narrative pivots.

The original prompt: *Allbirds pivots to AI, stock surges — is this a trend
that can be tracked and profited from, and are there other trends like this?*

Yes, it's a recurring pattern. `switching` is a research tool that

1. **Scans** public data sources (press-release RSS, SEC filings) for events
   matching a registered **detector** (AI-pivot is the first),
2. **Correlates** each event with the issuer's stock reaction window
   (1-day and 5-day returns, volume ratio), and
3. **Backtests** a detector against a curated set of historical events so you
   can see the win rate and average return *before* trusting its output.

> **Disclaimer.** This is a research and observability tool, not investment
> advice. It reports signals and historical reactions; it does not recommend
> or execute trades. Past patterns do not predict future returns.

## Why a framework instead of one script

"Company X pivots to Y, stock reacts" is a family of patterns. Each is a
detector that plugs into the same pipeline:

| Detector | Status | Thesis |
|---|---|---|
| `ai_pivot` | v1 | Companies rebranding / launching around AI |
| `crypto_treasury` | roadmap | Companies adding BTC/ETH to their balance sheet (MSTR-style) |
| `activist_13d` | roadmap | Activist investor takes >5% stake (13D filing) |
| `buyback` | roadmap | Board authorizes share repurchase |
| `stock_split` | roadmap | Forward split (retail momentum catalyst) |
| `index_inclusion` | roadmap | Added to S&P/Russell index (forced buying) |
| `insider_cluster` | roadmap | Multiple insiders buying within a short window (Form 4) |

Each detector is a single file under `src/switching/detectors/`. The shared
framework handles price correlation, ranking, reporting, and backtesting.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## CLI

```bash
# List what's registered.
switching list-detectors

# Scan the last 7 days of press-release feeds; write JSON + CSV.
switching scan --since 7d --detector ai_pivot --json out.json --csv out.csv

# Backtest the AI-pivot thesis against seeded historical events.
switching backtest --detector ai_pivot --from 2023-01-01 --to 2024-12-31 \
  --hold-days 5 --cost-bps 10 --csv trades.csv
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
│ (pluggable) │   │ (dataclass)  │   │   (yfinance)     │   │ CLI/JSON/CSV │
└─────────────┘   └──────────────┘   └──────────────────┘   └──────────────┘
       │                                    │
       │             ┌──────────────────────┘
       ▼             ▼
┌───────────────────────────┐   ┌────────────────────────┐
│ Backtester                │ → │ Performance summary    │
│ replay historical events  │   │ win rate, avg, Sharpe  │
└───────────────────────────┘   └────────────────────────┘
```

- **`signal.py`** — `Signal` + `PriceReaction` dataclasses. Detectors emit
  these; price correlation and reporting consume them.
- **`registry.py`** — `@register` decorator that makes new detectors drop-in.
- **`pricing.py`** — thin `yfinance` wrapper with a local SQLite cache so
  reruns are cheap. Computes same-day and N-day returns against a trailing
  baseline.
- **`sources/`** — RSS feeds and curated historical event CSVs.
- **`detectors/ai_pivot.py`** — first concrete detector: regex over press-
  release text combining AI vocabulary with pivot verbs, plus a
  `(NASDAQ: TICKER)` extractor for issuer resolution.
- **`backtest.py`** — replays signals through a next-day-open / N-day-close
  rule with a configurable transaction-cost assumption.
- **`reporter.py`** + **`cli.py`** — `rich` tables, JSON, CSV; Typer CLI.

## Adding a new detector

1. Create `src/switching/detectors/<name>.py`.
2. Subclass `Detector`, set `name` and `description`, implement `scan()`.
3. Decorate the class with `@register`. Import it from
   `registry.load_builtin_detectors()` so it's available in the CLI.
4. Optionally add `data/historical_events/<name>.csv` so `backtest` has
   events to replay.

## Development

```bash
pytest
```

All tests are offline — pricing, backtest, and RSS tests use in-memory
fixtures. Live yfinance / RSS calls only happen when you run `switching scan`
or `switching backtest` against real data.

## Roadmap (future work, not v1)

- **Automation** — scheduled daemon (APScheduler/cron) that appends new
  signals to a rolling store and pushes to a Slack/Discord webhook.
- **Web UI** — small FastAPI + htmx front-end that renders the same `Signal`
  objects with per-ticker price charts.
- **Additional detectors** — see the table above.
- **LLM-based classification** — replace the hand-tuned regex in detectors
  with an LLM prompt that extracts "pivot vs. incremental" nuance from
  release text.
- **Paper-trading integration** — once a detector's backtested win rate
  justifies it, connect to a paper-trading API (e.g. Alpaca) before any real
  capital is involved.

## License

MIT.
