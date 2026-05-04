# Switching — Project Memory

## Overview
Stock signal detection and paper-trading system. Scans RSS feeds and SEC EDGAR for
corporate events (upgrades, M&A, FDA, dividends, contracts, etc.), scores signals,
paper-trades at next-day open, manages exits via detector-specific profiles.

Goal: Turn profit (~£20K after tax) from £100K investment. UK-based, trading US markets.
Ltd company structure at 25% corp tax being evaluated vs 40% personal rate.

## Repository
- **GitHub**: `alivebe-a11y/switching` (PUBLIC repo — no secrets)
- **Branch**: `claude/add-ai-recommendations-ABZZX`
- **268 tests**, run with: `pytest tests/`

## Deployment (TrueNAS via Dockge)
- Stack path: `/Pool_1/Configs/dockge2/Stacks/stocks`
- **Dockge uses `compose.yaml`** (NOT `docker-compose.yml`)
- Docker build context pulls directly from GitHub — no local git clone on TrueNAS
- Deploy command:
  ```bash
  curl -sL "https://raw.githubusercontent.com/alivebe-a11y/switching/claude/add-ai-recommendations-ABZZX/docker-compose.yml" -o compose.yaml && docker compose build --no-cache paper-trade && docker compose down paper-trade && docker compose up paper-trade -d
  ```
- For dashboard too: `docker compose up dashboard -d`
- Dashboard port: 8080

## Services (docker-compose.yml)
| Service | Command | Notes |
|---------|---------|-------|
| paper-trade | `switching paper-trade --seed 1000 --interval 10 --stop-loss 0.026 --hold-days 5` | Main service, runs 24/7 |
| dashboard | `switching web --port 8080` | Flask web UI on port 8080 |
| trade | `switching trade ...` | Alpaca live trading (not yet active) |
| switching | `switching list-detectors` | One-shot utility |

## Environment Variables (set in Dockge .env)
- `SWITCHING_EDGAR_UA` — Required for EDGAR-based detectors (activist_13d, insider_cluster)
- `ANTHROPIC_API_KEY` — Claude Haiku for AI signal scoring (~$0.30/month)
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — Push notifications
- `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` — Not active yet

## Critical: Data Directory
Seed CSVs MUST go in `src/switching/data/historical_events/` (NOT top-level `data/`).
The `_find_data_root()` in `sources/historical.py` resolves relative to the package.
The top-level `data/` directory is a mirror/legacy — always put seeds in both.

## Detectors (13 registered)
| Detector | Source | Exit Profile |
|----------|--------|--------------|
| earnings_surprise | RSS (earnings feeds) | first_green +0%, 2-day hold |
| ai_pivot | RSS (default feeds) | first_green +2% (>$30) or +0% (<$30), 3-5 day |
| analyst_upgrade | RSS (default feeds) | first_green +1%, 3-day hold |
| fda_decision | RSS (default + earnings) | first_green +3%, 3-day hold |
| buyback | RSS (default + corporate) | NO first_green, 5-day hold |
| index_inclusion | RSS (default + corporate) | default (first_green +0%, 5-day) |
| spinoff | RSS (default + corporate) | default |
| mna_target | RSS (default + corporate) | first_green +3%, 5-day hold |
| guidance_raise | RSS (default + earnings + corporate) | first_green +2%, 3-day hold |
| dividend_surprise | RSS (default + earnings + corporate) | first_green +1%, 3-day hold |
| contract_win | RSS (default + corporate) | first_green +2%, 5-day hold |
| activist_13d | SEC EDGAR (13D filings) | default |
| insider_cluster | SEC EDGAR (Form 4) | default |

## Stop-Loss Tiers
- $30+ stocks: 2.6%
- $5-$30 stocks: 3.6%
- <$5 stocks: 4.6%

## Architecture
```
src/switching/
├── cli.py              — Typer CLI (scan, backtest, paper-trade, web, check-feeds)
├── paper_trader.py     — Core trading loop, position/exit management
├── web.py              — Flask dashboard (single HTML template with JS)
├── registry.py         — @register decorator, load_builtin_detectors()
├── signal.py           — Signal dataclass, PriceReaction, dedup_key
├── pricing.py          — yfinance wrapper, PriceCache (SQLite)
├── backtest.py         — Historical replay engine
├── reporter.py         — rank, render_table, write_json/csv
├── trade_memory.py     — Per-detector/per-price-tier stats from closed trades
├── ai_filter.py        — Claude Haiku scoring (0-1), log-only mode
├── notifications.py    — Telegram push (buy/sell/skip/daily summary/startup)
├── detectors/          — All detector modules (one per file)
│   └── base.py         — Detector ABC
├── sources/
│   ├── rss.py          — DEFAULT_FEEDS, EARNINGS_FEEDS, CORPORATE_FEEDS
│   ├── historical.py   — Seed CSV loader + live EDGAR augmentation
│   └── sec_edgar.py    — EdgarClient (rate-limited, needs SWITCHING_EDGAR_UA)
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

## Known Issues / Gotchas
- yfinance blocked in CI/sandbox — backtests show 0 trades but events load fine
- Telegram: duplicate notifications if old container still running alongside new one
- Terminal hyperlinks can mangle Python module names in Dockge console
- EDGAR rate limit: 8 req/s (conservative vs SEC's 10 req/s limit)

---

## Roadmap

### Phase 1 — Prove the Strategy (Now → Month 3)
- [ ] Collect 50+ live trades with AI scores attached
- [ ] Compare Haiku predictions vs actual outcomes
- [ ] Tune exit profiles based on live data (not just backtest seeds)
- [ ] Enable AI filter gating once score threshold is validated
- [ ] Improve buyback detector (36% win rate — needs work or disable)
- [ ] Build Form 4 XML parser for insider_cluster (currently stub)

### Phase 2 — Scale to Real Capital (Month 3-6)
- [ ] Set up UK Ltd company for tax efficiency (25% vs 40%)
- [ ] Fund with £5-10K own savings
- [ ] Alpaca live trading (paper mode first on real API)
- [ ] Add Polygon.io (~$30/month) for real-time price data
- [ ] Claim business expenses (internet, electricity, hardware, APIs)

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

### Detector Ideas
- [ ] stock_split — splits often run up beforehand
- [ ] crypto_treasury — Bitcoin treasury announcements (MicroStrategy pattern)
- [ ] geopolitical — oil/defence/shipping on geopolitical events (Strait of Hormuz etc.)
- [ ] day_trading — intraday momentum signals (separate project likely)

### Completed
- [x] 13 detectors live: ai_pivot, earnings_surprise, buyback, activist_13d, insider_cluster, index_inclusion, spinoff, analyst_upgrade, fda_decision, mna_target, guidance_raise, dividend_surprise, contract_win
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
