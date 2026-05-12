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
- **453 tests**, run with: `pytest tests/`

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
| earnings_surprise | RSS (earnings feeds) | first_green +2%, 3-day hold *(raised from +0.5%/2d — live data: SNEX left 14.6% on table)* |
| ai_pivot | RSS (default feeds) | first_green +2% (>$30) or +0% (<$30), 3-5 day |
| analyst_upgrade | RSS (default feeds) | first_green +1%, 3-day hold |
| fda_decision | RSS (default + earnings) | first_green +3%, 3-day hold |
| buyback | RSS (default + corporate) | NO first_green, 5-day hold |
| index_inclusion | RSS (default + corporate) | default (first_green +0%, 5-day) |
| spinoff | RSS (default + corporate) | default |
| mna_target | RSS (default + corporate) | first_green +3%, 5-day hold; **acquirer-direction signals are skipped** (live data: 100% hit stop-loss) |
| guidance_raise | RSS (default + earnings + corporate) | first_green +5%, 5-day hold *(raised from +2%/3d — CVS/GEN/TBLA all ran 10-38% post-exit)* |
| dividend_surprise | RSS (default + earnings + corporate) | first_green +1%, 4-day hold, +1% wider stop *(MKTW/SII false stops — recovered 18%/14%)* |
| contract_win | RSS (default + corporate) | first_green +2%, 5-day hold |
| activist_13d | SEC EDGAR (13D filings) | default |
| insider_cluster | SEC EDGAR (Form 4) | default |
| stock_split | RSS (default + corporate) | first_green +1.5%, 4-day hold |
| crypto_treasury | RSS (default + corporate) | first_green +3%, 3-day hold |

## Stop-Loss Tiers
- $30+ stocks: 2.6%
- $5-$30 stocks: 3.6%
- <$5 stocks: 4.6%

## Architecture
```
src/switching/
├── cli.py              — Typer CLI (scan, backtest, paper-trade, web, check-feeds)
├── paper_trader.py     — Core trading loop, position/exit management
├── web.py              — Flask dashboard (reads cached prices from portfolio JSON, no live yfinance polling)
├── registry.py         — @register decorator, load_builtin_detectors()
├── signal.py           — Signal dataclass, PriceReaction, dedup_key
├── pricing.py          — yfinance wrapper, PriceCache (SQLite)
├── backtest.py         — Historical replay engine
├── reporter.py         — rank, render_table, write_json/csv
├── trade_memory.py     — Per-detector/per-price-tier stats from closed trades
├── exit_tracker.py     — Post-exit price tracker (20 days) for detector refinement
├── skipped_tracker.py  — Tracks signals skipped due to max-positions / insufficient cash; runs same exit logic for "would-have-been" P&L
├── ai_filter.py        — Claude Haiku scoring (0-1), log-only mode
├── notifications.py    — Telegram push (buys batched 2h, sells/stops immediate, daily summary at close)
├── detectors/          — All detector modules (one per file)
│   └── base.py         — Detector ABC
├── sources/
│   ├── rss.py          — DEFAULT_FEEDS, EARNINGS_FEEDS, CORPORATE_FEEDS
│   ├── historical.py   — Seed CSV loader + live EDGAR augmentation
│   ├── sec_edgar.py    — EdgarClient (rate-limited, needs SWITCHING_EDGAR_UA)
│   └── ticker_lookup.py — SEC company-name→ticker fallback for extract_ticker()
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

## Notification Batching
`src/switching/notifications.py` queues buy notifications in `_NotificationQueue` and flushes
every 2 hours via a daemon timer. Sells, stop-losses, skips, and the end-of-day summary call
`_send()` directly. The paper trader calls `flush_buy_queue()` before sending the daily summary
so the digest is up-to-date. With unlimited positions this prevents Telegram spam (10-20 buys
per scan cycle would otherwise produce 10-20 separate messages).

## Dashboard Data Flow
The Flask dashboard (`src/switching/web.py`) reads everything from the portfolio JSON
state file — it never calls yfinance. The paper trader's scan loop populates
`portfolio.cached_prices[ticker] = current_price` for every held position each cycle, then
saves. Dashboard refreshes show prices stamped with `last_scan_dt`. Stale by at most one
scan interval (default 10 min). Stale tickers (no longer held) are pruned each cycle.

## Known Issues / Gotchas
- yfinance blocked in CI/sandbox — backtests show 0 trades but events load fine
- Telegram: duplicate notifications if old container still running alongside new one
- Terminal hyperlinks can mangle Python module names in Dockge console
- EDGAR rate limit: 8 req/s (conservative vs SEC's 10 req/s limit)
- SEC ticker map caches to `/tmp/sec_company_tickers.json` (override via `SWITCHING_CACHE_DIR`)

---

## Maintenance: Review & Update This File
At the start of each session, verify this file is still accurate. Update when:
- A new detector is added or removed
- Exit profiles change based on live performance data
- Infrastructure changes (new services, new env vars)
- Performance baselines shift (win rates after 50+ trades)
- Architecture decisions are revisited

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

### ADR-006: IBKR as broker (not Alpaca)
**Decision**: Interactive Brokers (IBKR) for live and paper trading. Alpaca removed from roadmap.
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
```bash
curl -sL "https://raw.githubusercontent.com/alivebe-a11y/switching/claude/add-ai-recommendations-ABZZX/docker-compose.yml" -o compose.yaml && docker compose build --no-cache paper-trade && docker compose down paper-trade && docker compose up paper-trade -d
```
For dashboard: `docker compose up dashboard -d`
For both: add `dashboard` to the up command.

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
- Mirror `broker_alpaca.py` interface: `buy_market`, `sell_all`, `get_quote`, `is_market_open`
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
- [x] `severity` stored on `ClosedTrade` — enables signal quality ↔ outcome correlation analysis
