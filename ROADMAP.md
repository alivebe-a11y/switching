# Switching — Roadmap

## Current Status
- 16 detectors live (ai_pivot, earnings_surprise, buyback, activist_13d, insider_cluster,
  index_inclusion, spinoff, analyst_upgrade, fda_decision, mna_target, guidance_raise,
  dividend_surprise, contract_win, stock_split, crypto_treasury, uk_director_dealing)
- Five services on TrueNAS via Docker (Dockge), all on one shared image:
  US paper, UK (LSE) paper, US T212 demo, UK T212 demo, Flask dashboard
- SQLite storage (`switching.db`, WAL, service-tagged us/uk/t212/t212_uk) — services share
  one file without collision; auto-migrated from the legacy per-service JSON
- 10-minute scan interval (1-min fast-scan for the first 15 min after open); T212 exits
  poll every 60s
- Conviction-weighted position sizing (guidance_raise ×7, etc.); 1.5% base, 12% per-position cap
- Ride-mode (peak-trailing) exits for momentum detectors (ai_pivot, mna_target)
- Out-of-hours signal queue — drains back into the buy pipeline at next market open
- Trade memory + Haiku AI scoring (log-only)
- Telegram notifications: market-tagged (US / LSE / T212), buys batched 2h, sells/stops
  immediate, daily summary at close, Saturday weekly report
- 2.6% tiered stop-loss, detector-specific exit profiles
- Flask web dashboard reads cached prices from SQLite (no live yfinance polling)
- SEC EDGAR integration (13D filings, Form 4); UK ingestion via Investegate RNS scrape
  + Google News fallback
- Post-exit price tracker (20 days) + skipped-signal tracker + detection funnel
- 752 tests passing

## Phase 1 — Prove the Strategy (Now → Month 3)
- [ ] Collect 50+ live trades with AI scores attached
- [ ] Compare Haiku predictions vs actual outcomes
- [ ] Tune exit profiles based on live data (not just backtest seeds)
- [ ] Enable AI filter gating once score threshold is validated
- [ ] Improve buyback detector (36% win rate — needs work or disable)
- [ ] Build Form 4 XML parser for insider_cluster (currently stub)

## Phase 2 — Scale to Real Capital (Month 3-6)
- [ ] Set up UK Ltd company for tax efficiency (25% vs 40%)
- [ ] Fund with £5-10K own savings
- [ ] Alpaca live trading (paper mode first on real API)
- [ ] Add Polygon.io (~$30/month) for real-time price data
- [ ] Claim business expenses (internet, electricity, hardware, APIs)

## Phase 3 — Options Trading (Month 6-9)
- [ ] Historical options chain data (Polygon.io options add-on ~$200/month)
- [ ] Options backtester (Black-Scholes or historical chains)
- [ ] Strike/expiry selection logic
- [ ] Options only on high-conviction detectors (earnings_surprise, index_inclusion)
- [ ] Theta decay early-exit rules

## Phase 4 — Scale & Harden (Month 9-12)
- [ ] Scale to £30K capital
- [ ] Multi-container architecture (VPN for rate limit distribution)
- [ ] UK market support (LSE, RNS feeds, FCA filings)
- [ ] Other markets (EU, Asia — evaluate per-market)

## Infrastructure
- [ ] **Failover / high availability**: secondary machine (VPS or second NAS) that monitors primary heartbeat and takes over if primary goes offline. State file sync via shared volume or periodic rsync. Alert on failover via Telegram.
- [ ] VPS deployment for uptime (keep TrueNAS as primary, VPS as failover)
- [ ] Automated backup of trade state and memory files
- [ ] Circuit breaker: disable detector after N consecutive empty scans
- [ ] Health dashboard: feed status, scan counts, API latency
- [ ] Migrate state from JSON to SQLite (trigger: 1,000+ closed trades or cross-detector queries) — schema mapped in `docs/SQL_SCHEMA.md`
- [ ] Batch yfinance price fetches (`yf.download(tickers)`) instead of one call per held position

## AI Improvements
- [ ] Turn on Haiku filter gating (after 50+ scored trades)
- [ ] Upgrade to Sonnet for complex signals (earnings + guidance + sentiment)
- [ ] Memory palace: cross-detector learning (e.g. "NVDA responds well to AI pivot + earnings combo")
- [ ] Sentiment analysis on headline text beyond regex
- [ ] Claude API integration for adaptive strategy tuning

## Data Sources (when capital justifies cost)
- [ ] Polygon.io real-time + options ($30-200/month)
- [ ] Tiingo Pro for cleaner fundamentals ($30/month)
- [ ] S&P Capital IQ (£15-25K/year — only at £100K+ capital)
- [ ] Bloomberg Terminal (£24K/year — only if running a fund)

## Triggered / Deferred items
- [ ] **Corporate-actions feed ingestion** — proactively map ticker changes + pre-empt
      splits/delistings. DEFERRED; build only when running real capital AND holds extend
      past ~5 days AND a *structured* corporate-actions data source exists AND the reactive
      ghost-position reconciliation proves insufficient. Full trigger conditions and
      rationale in `CLAUDE.md → Roadmap → Triggered / Deferred items` (re-checked on each
      roadmap scan). Reactive fix already shipped (`_reconcile_t212_ghosts`).

## Detector Ideas
- [ ] stock_split — splits often run up beforehand
- [ ] crypto_treasury — Bitcoin treasury announcements (MicroStrategy pattern)
- [ ] geopolitical — oil/defence/shipping on geopolitical events (Strait of Hormuz etc.)
- [ ] day_trading — intraday momentum signals (separate project likely)

## Completed
- [x] 16 detectors built and registered with seed data and tests
- [x] Paper trading engine with $1K simulated portfolio
- [x] Trade memory (Phase 1) — per-detector/per-price-tier/per-exit-reason stats
- [x] Haiku AI signal scoring (log-only mode)
- [x] Telegram push notifications
- [x] 2.6% tiered stop-loss with detector-specific exit profiles
- [x] Flask web dashboard
- [x] SEC EDGAR client + CIK→ticker mapping
- [x] CORPORATE_FEEDS for corporate-action detectors
- [x] check-feeds diagnostic command
- [x] Security audit — public repo clean, .gitignore covers secrets
- [x] dividend_surprise detector (was idea — now built)
- [x] fda_decision detector (was FDA_approval idea — now built)
- [x] Diagnostic logging: all 11 RSS detectors log items/classified/with_ticker per scan
- [x] SEC company-name-to-ticker fallback — extract_ticker() now resolves company names via SEC data
- [x] 752 tests passing
- [x] Post-exit price tracker — 20-day post-close monitoring for detector refinement
- [x] Dashboard "Post-Exit Tracker" panel with per-detector insights and left-on-table metrics
- [x] Telegram buy notifications batched every 2 hours (digest format) — sells/stops still immediate
- [x] Dashboard reads cached prices from portfolio JSON — no live yfinance polling per page load
- [x] SQL schema mapping doc (`docs/SQL_SCHEMA.md`) — forward-looking, JSON stays for now
- [x] Skipped-signal tracker — captures signals skipped due to max-positions / insufficient cash and runs the same exit logic to surface would-have-been P&L
