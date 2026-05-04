# Switching — Roadmap

## Current Status
- 13 detectors live (ai_pivot, earnings_surprise, buyback, activist_13d, insider_cluster, index_inclusion, spinoff, analyst_upgrade, fda_decision, mna_target, guidance_raise, dividend_surprise, contract_win)
- Paper trading on TrueNAS via Docker (Dockge), 10-minute scan interval
- Trade memory + Haiku AI scoring (log-only)
- Telegram notifications (buy/sell/skip/daily summary/startup)
- 2.6% tiered stop-loss, detector-specific exit profiles
- Flask web dashboard (portfolio, trades, signals, equity curve)
- SEC EDGAR integration (13D filings, Form 4)
- 304 tests passing

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

## Detector Ideas
- [ ] stock_split — splits often run up beforehand
- [ ] crypto_treasury — Bitcoin treasury announcements (MicroStrategy pattern)
- [ ] geopolitical — oil/defence/shipping on geopolitical events (Strait of Hormuz etc.)
- [ ] day_trading — intraday momentum signals (separate project likely)

## Completed
- [x] 13 detectors built and registered with seed data and tests
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
- [x] 304 tests passing
- [x] Post-exit price tracker — 20-day post-close monitoring for detector refinement
- [x] Dashboard "Post-Exit Tracker" panel with per-detector insights and left-on-table metrics
