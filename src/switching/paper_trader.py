"""Paper-trading engine.

Runs a continuous loop: scan for signals, open positions at market price,
monitor exits (first-green / stop-loss / hold expiry), track P&L against
a simulated cash balance, and monitor post-exit price paths for detector
refinement. All state persists to JSON files so the process can restart
without losing history."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from switching.market_calendar import (
    is_market_hours,
    is_lse_hours,
    trading_days_since,
    trading_days_since_lse,
)
from switching.signal import Signal

log = logging.getLogger(__name__)


@dataclass
class Position:
    ticker: str
    detector: str
    entry_price: float
    shares: float
    entry_dt: str
    headline: str
    severity: float
    stop_loss: float
    hold_days: int
    days_held: int = 0
    first_green: bool = True
    first_green_pct: float = 0.0
    peak_price: float = 0.0
    peak_tracking: bool = False
    # Daily OHLC snapshots recorded while the position is held.
    # Each entry: {date, day, open, high, low, close, pct_from_entry, high_pct, low_pct}
    snapshots: list = field(default_factory=list)

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.shares


@dataclass
class ClosedTrade:
    ticker: str
    detector: str
    entry_price: float
    exit_price: float
    shares: float
    entry_dt: str
    exit_dt: str
    pnl: float
    pct_return: float
    exit_reason: str
    headline: str
    peak_price: float = 0.0
    severity: float = 0.0  # signal severity — used for quality correlation in analytics


@dataclass
class Portfolio:
    cash: float = 1000.0
    positions: list[Position] = field(default_factory=list)
    trades: list[ClosedTrade] = field(default_factory=list)
    seen_signals: list[str] = field(default_factory=list)
    last_signals: list[dict] = field(default_factory=list)
    last_scan_dt: str = ""
    max_position_pct: float = 0.20
    max_positions: int = 5
    cached_prices: dict[str, float] = field(default_factory=dict)
    last_review_sent_dt: str = ""
    last_weekly_report_dt: str = ""   # ISO timestamp of last Saturday report
    # T212-only: symbol -> ISO timestamp of last sell. Used to (a) ignore a
    # just-sold position while the broker settles the order (prevents double
    # sells) and (b) apply a short re-buy cooldown (prevents same-story churn).
    recently_sold: dict[str, str] = field(default_factory=dict)

    @property
    def total_value(self) -> float:
        return self.cash + sum(p.cost_basis for p in self.positions)

    def save(self, path: Path) -> None:
        from switching import storage
        state = {
            "cash": self.cash,
            "positions": [asdict(p) for p in self.positions],
            "trades": [asdict(t) for t in self.trades],
            "seen_signals": self.seen_signals[-500:],
            "last_signals": self.last_signals[-50:],
            "last_scan_dt": self.last_scan_dt,
            "max_position_pct": self.max_position_pct,
            "max_positions": self.max_positions,
            "cached_prices": self.cached_prices,
            "last_review_sent_dt": self.last_review_sent_dt,
            "last_weekly_report_dt": self.last_weekly_report_dt,
            "recently_sold": self.recently_sold,
        }
        storage.save_portfolio_state(path, state)

    @classmethod
    def load(cls, path: Path) -> Portfolio:
        from switching import storage
        state = storage.load_portfolio_state(path)
        if state is None:
            return cls()
        return cls(
            cash=state["cash"],
            positions=[Position(**p) for p in state.get("positions", [])],
            trades=[ClosedTrade(**t) for t in state.get("trades", [])],
            seen_signals=state.get("seen_signals", []),
            last_signals=state.get("last_signals", []),
            last_scan_dt=state.get("last_scan_dt", ""),
            max_position_pct=state.get("max_position_pct", 0.20),
            max_positions=state.get("max_positions", 5),
            cached_prices=state.get("cached_prices", {}),
            last_review_sent_dt=state.get("last_review_sent_dt", ""),
            last_weekly_report_dt=state.get("last_weekly_report_dt", ""),
            recently_sold=state.get("recently_sold", {}),
        )


def get_current_price(ticker: str) -> float | None:
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        hist = t.history(period="1d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as exc:
        log.warning("price fetch failed for %s: %s", ticker, exc)
        return None


def get_intraday_data(ticker: str) -> dict | None:
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        hist = t.history(period="2d")
        if hist.empty:
            return None
        row = hist.iloc[-1]
        return {
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
        }
    except Exception as exc:
        log.warning("intraday fetch failed for %s: %s", ticker, exc)
        return None


def _signal_key(sig: Signal) -> str:
    """Stable per-article dedup key for the ``seen_signals`` list.

    Uses the article URL when available — it is a stable identifier that
    doesn't change between scan cycles even when the RSS feed item has no
    ``published_parsed`` date (which would otherwise cause ``datetime.now()``
    to produce a new date each scan and bypass the dedup check).

    Falls back to a 16-char MD5 hex of the normalised headline when there
    is no URL (e.g. EDGAR-sourced signals).
    """
    if sig.url:
        article_id = sig.url[:120]
    else:
        article_id = hashlib.md5(sig.headline.lower().encode()).hexdigest()[:16]
    return f"{sig.detector}:{sig.ticker}:{article_id}"


# --- T212 settlement / churn guards ---------------------------------------
# After a sell, T212 takes time to settle the order. During this window the
# position can still appear in get_positions() with no local tracker. We must
# NOT re-adopt and re-sell it (double sell), so we ignore symbols sold within
# the settlement window. A longer cooldown also prevents same-day re-buys of a
# ticker we just exited (curbs churn when the same story arrives via multiple
# wire services with different URLs).
_T212_SETTLE_MINUTES = 15
_T212_REBUY_COOLDOWN_HOURS = 4
_T212_EXIT_POLL_SECONDS = 60   # how often to re-check live exits between scans


def _minutes_since(iso_ts: str, now: datetime) -> float | None:
    """Minutes elapsed since an ISO timestamp, or None if unparseable."""
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.rstrip("Z"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 60.0


def _prune_recently_sold(recently_sold: dict[str, str], now: datetime) -> None:
    """Drop recently-sold entries older than the re-buy cooldown (in place)."""
    cutoff_min = _T212_REBUY_COOLDOWN_HOURS * 60
    stale = [
        sym for sym, ts in recently_sold.items()
        if (m := _minutes_since(ts, now)) is None or m > cutoff_min
    ]
    for sym in stale:
        del recently_sold[sym]


def _record_t212_skip(skipped_tracker, sig, reason: str, base_stop: float, price: float | None = None) -> None:
    """Record a skipped T212 signal for would-have-been P&L analysis.

    Mirrors the internal paper trader's skipped-signal tracking so T212's
    opportunity cost (max-positions / low-cash skips) is measured too.
    """
    if price is None:
        price = get_current_price(sig.ticker)
    if not price or price <= 0:
        return
    profile = _exit_profile(sig.detector, price)
    skipped_tracker.add(
        ticker=sig.ticker,
        detector=sig.detector,
        severity=sig.severity,
        headline=sig.headline,
        skip_reason=reason,
        price=price,
        hold_days=profile["hold_days"],
        first_green=profile.get("first_green", True),
        first_green_pct=profile.get("first_green_pct", 0.0),
        stop_loss_pct=_tiered_stop_loss(base_stop, price) + profile.get("stop_loss_extra", 0.0),
    )


def _reconcile_t212_ghosts(portfolio, held_symbols_t212, now) -> list:
    """Close out local positions that T212 no longer reports.

    When T212 closes a position via a corporate action (M&A cash-out, delisting,
    liquidation) or a ticker change, it disappears from ``get_positions()`` but
    lingers in our local ``portfolio.positions`` — never recorded, blocking
    re-buys and showing as a phantom open position. This records each as a
    ``ClosedTrade(exit_reason="corporate_action")`` (best-effort exit price from
    the last cached price) and removes it.

    Skips positions still inside the post-sell settlement window — those are our
    OWN sells settling, not external closes. Returns the ClosedTrades created so
    the caller can notify.

    The caller must only invoke this after a SUCCESSFUL positions fetch, so a
    transient API glitch can't wipe out every tracked position.
    """
    closed_out: list = []
    for pos in list(portfolio.positions):
        if pos.ticker in held_symbols_t212:
            continue
        sold_mins = _minutes_since(portfolio.recently_sold.get(pos.ticker, ""), now)
        if sold_mins is not None and sold_mins < _T212_SETTLE_MINUTES:
            continue   # our own sell, still settling — not an external close
        exit_price = portfolio.cached_prices.get(pos.ticker) or pos.entry_price
        ret = (exit_price / pos.entry_price - 1.0) if pos.entry_price else 0.0
        pnl = round((exit_price - pos.entry_price) * pos.shares, 2)
        closed = ClosedTrade(
            ticker=pos.ticker,
            detector=pos.detector,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            shares=pos.shares,
            entry_dt=pos.entry_dt,
            exit_dt=now.isoformat(),
            pnl=pnl,
            pct_return=round(ret, 4),
            exit_reason="corporate_action",
            headline=pos.headline,
            severity=pos.severity,
        )
        portfolio.trades.append(closed)
        portfolio.recently_sold[pos.ticker] = now.isoformat()
        closed_out.append(closed)
    if closed_out:
        gone = {c.ticker for c in closed_out}
        portfolio.positions = [p for p in portfolio.positions if p.ticker not in gone]
    return closed_out


def _normalise_price(price: float, market: str) -> float:
    """Return price in major currency units. UK stocks trade in pence (GBX)."""
    return price / 100.0 if market == "uk" else price


def _is_market_open(market: str) -> bool:
    """Dispatch to the correct calendar function based on market."""
    if market == "uk":
        return is_lse_hours()
    return is_market_hours()


def _tiered_stop_loss(base_stop: float, price: float, market: str = "us") -> float:
    """Widen stop-loss for volatile low-price stocks, tighten for large caps.

    Tiers are evaluated on normalised price (major currency units).
    UK stocks quoted in pence are normalised to pounds before comparison.
    The returned stop-loss fraction is applied to the raw price as-is.
    """
    normalised = _normalise_price(price, market)
    if normalised >= 30.0:
        return base_stop
    if normalised >= 5.0:
        return base_stop + 0.01
    return base_stop + 0.02


# Never put more than this fraction of the fund into a single position, no
# matter how high its conviction weight — caps idiosyncratic risk.
_MAX_SINGLE_POSITION_PCT = 0.12


def _position_weight(detector: str) -> float:
    """Conviction multiplier on the base position size, by proven performance.

    Sizes UP detectors that consistently earn it (from live data), leaving the
    rest at baseline (1.0). Deliberately does NOT size weak detectors *down*:
    the strategy's profit is fat-tailed and the tail winners arrive from
    low-win-rate detectors (e.g. mna_target produced a +56% trade), so shrinking
    them would clip the tail. Allocation is still bounded by available cash and
    by ``_MAX_SINGLE_POSITION_PCT``.

    Replaces the old fixed-dollar guidance_raise override with a fund-relative
    weight so sizing scales as the fund grows.
    """
    _WEIGHTS: dict[str, float] = {
        "guidance_raise": 7.0,     # 63% WR, +4.2% avg, ~95% of live P&L — the engine
        "dividend_surprise": 2.0,  # 60% WR, consistent small wins
        "contract_win": 2.0,       # 60% WR
        "buyback": 1.5,            # 50% WR, slow grind
    }
    return _WEIGHTS.get(detector, 1.0)


def _exit_profile(detector: str, price: float) -> dict:
    """Return detector-specific exit parameters based on observed performance."""
    if detector == "buyback":
        return {"first_green": False, "first_green_pct": 0.0, "hold_days": 5}
    if detector == "earnings_surprise":
        # Live data: first_green_pct 0% was leaving 5-15% on table (e.g. SNEX +14.6%).
        # Raised to 2%; hold extended to 3 days to capture post-announcement drift.
        return {"first_green": True, "first_green_pct": 0.02, "hold_days": 3}
    if detector == "ai_pivot":
        # Momentum: live data shows winners peak ~day 8 and leave ~22% on the
        # table under the old fixed first-green exit. "Ride mode" flips a green
        # position into peak-tracking with a wider 3% trailing band so it rides
        # toward the peak, with an 8-day backstop. Plays it safe (locks in via
        # trailing stop) rather than chasing the exact top.
        if price >= 30.0:
            return {"first_green": True, "first_green_pct": 0.02, "hold_days": 8,
                    "ride": True, "trail_pct": 0.03}
        return {"first_green": True, "first_green_pct": 0.01, "hold_days": 6,
                "ride": True, "trail_pct": 0.03}
    if detector == "fda_decision":
        return {"first_green": True, "first_green_pct": 0.03, "hold_days": 3}
    if detector == "analyst_upgrade":
        return {"first_green": True, "first_green_pct": 0.01, "hold_days": 3}
    if detector == "mna_target":
        # Momentum: targets drift toward the offer price, peaking ~day 6-7 and
        # leaving ~11% on the table. Ride mode + 8-day hold captures the drift.
        return {"first_green": True, "first_green_pct": 0.03, "hold_days": 8,
                "ride": True, "trail_pct": 0.03}
    if detector == "guidance_raise":
        # Live data: stocks consistently ran 5-25% beyond the old 2% threshold
        # (CVS +14.3%, GEN +12.3%, TBLA +37.8% post-exit). Raised to 5%;
        # hold extended to 5 days to capture multi-day guidance-revision drift.
        return {"first_green": True, "first_green_pct": 0.05, "hold_days": 5}
    if detector == "dividend_surprise":
        # Live data: MKTW and SII both stopped out on day-0 intraday dips then
        # surged 18-14% — stop_loss_extra widens the effective stop by 1% for
        # this detector only, absorbing the typical post-dividend-surprise noise.
        return {"first_green": True, "first_green_pct": 0.01, "hold_days": 4,
                "stop_loss_extra": 0.01}
    if detector == "contract_win":
        return {"first_green": True, "first_green_pct": 0.02, "hold_days": 5}
    if detector == "stock_split":
        return {"first_green": True, "first_green_pct": 0.015, "hold_days": 4}
    if detector == "crypto_treasury":
        return {"first_green": True, "first_green_pct": 0.03, "hold_days": 3}
    return {"first_green": True, "first_green_pct": 0.0, "hold_days": 5}


def open_position(
    portfolio: Portfolio,
    signal: Signal,
    price: float,
    *,
    stop_loss: float = 0.026,
    hold_days: int = 5,
    market: str = "us",
) -> Position | None:
    if portfolio.max_positions > 0 and len(portfolio.positions) >= portfolio.max_positions:
        log.info("max positions reached, skipping %s", signal.ticker)
        return None
    for p in portfolio.positions:
        if p.ticker == signal.ticker:
            log.info("already holding %s, skipping", signal.ticker)
            return None

    # Price floor check using normalised price (pounds for UK, dollars for US)
    normalised = _normalise_price(price, market)
    floor = 1.0  # £1 or $1 in normalised units
    if normalised < floor:
        log.info("price %.4f (normalised %.4f) below floor, skipping %s", price, normalised, signal.ticker)
        return None

    # Conviction-weighted size: base allocation scaled by the detector's weight,
    # capped per-position and by available cash.
    weight = _position_weight(signal.detector)
    alloc = portfolio.total_value * portfolio.max_position_pct * weight
    alloc = min(alloc, portfolio.cash, portfolio.total_value * _MAX_SINGLE_POSITION_PCT)
    if alloc < 1.0:
        log.info("insufficient cash ($%.2f), skipping %s", portfolio.cash, signal.ticker)
        return None

    # UK prices from yfinance are in pence (GBX). Normalise to GBP so that
    # position sizing, cash deduction, and P&L are all in the same major unit.
    effective_price = _normalise_price(price, market)
    shares = alloc / effective_price
    cost = shares * effective_price
    portfolio.cash -= cost

    profile = _exit_profile(signal.detector, effective_price)
    actual_sl = _tiered_stop_loss(stop_loss, effective_price, market) + profile.get("stop_loss_extra", 0.0)

    pos = Position(
        ticker=signal.ticker,
        detector=signal.detector,
        entry_price=effective_price,  # stored in GBP for UK, USD for US
        shares=shares,
        entry_dt=datetime.now(tz=timezone.utc).isoformat(),
        headline=signal.headline,
        severity=signal.severity,
        stop_loss=actual_sl,
        hold_days=profile["hold_days"],
        first_green=profile["first_green"],
        first_green_pct=profile["first_green_pct"],
    )
    portfolio.positions.append(pos)
    portfolio.seen_signals.append(_signal_key(signal))
    return pos


def _calendar_days_since(entry_dt_str: str) -> int:
    """Calendar days between the position entry and now (kept for the Alpaca loop)."""
    entry = datetime.fromisoformat(entry_dt_str.replace("Z", "+00:00"))
    if entry.tzinfo is None:
        entry = entry.replace(tzinfo=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    return (now.date() - entry.date()).days


def check_exits(portfolio: Portfolio, market: str = "us") -> list[ClosedTrade]:
    closed: list[ClosedTrade] = []
    remaining: list[Position] = []

    # Compute once per call — avoids repeated syscalls inside the loop.
    # stop_loss always checked (defensive); first_green / hold_expiry only
    # fire when the market is actually open so we never exit at stale
    # weekend or bank-holiday prices.
    _mkt_open = _is_market_open(market)

    for pos in portfolio.positions:
        data = get_intraday_data(pos.ticker)
        if data is None:
            remaining.append(pos)
            continue

        # Normalise prices to major currency units (GBP for UK, USD for US).
        # yfinance returns LSE tickers in pence (GBX); entry_price is stored
        # in GBP, so exits must also be in GBP for correct return calculation.
        price = _normalise_price(data["close"], market)
        ret = price / pos.entry_price - 1.0
        ret_low = _normalise_price(data["low"], market) / pos.entry_price - 1.0
        reason = None

        # Trading days elapsed — weekends and bank holidays do NOT count.
        if market == "uk":
            days_elapsed = trading_days_since_lse(pos.entry_dt)
        else:
            days_elapsed = trading_days_since(pos.entry_dt)

        # Profile-derived exit modifiers (re-derived each cycle, so no extra
        # persisted Position fields needed — peak_tracking/peak_price already
        # persist). trail_pct: trailing band once peak-tracking. ride: momentum
        # detectors flip into peak-tracking at first-green instead of exiting,
        # to ride toward the peak.
        _profile = _exit_profile(pos.detector, pos.entry_price)
        trail_pct = _profile.get("trail_pct", 0.005)
        ride = _profile.get("ride", False)

        if ret_low <= -pos.stop_loss:
            # Stop-loss fires regardless of market hours (last-known price is
            # the best we have; price is snapped to the stop level anyway).
            reason = "stop_loss"
            price = pos.entry_price * (1.0 - pos.stop_loss)
        elif pos.peak_tracking:
            if price > pos.peak_price:
                pos.peak_price = price
            drop_from_peak = (pos.peak_price - price) / pos.peak_price if pos.peak_price > 0 else 0
            if drop_from_peak >= trail_pct:
                reason = "peak_trailing"
            elif _mkt_open and days_elapsed >= pos.hold_days:
                reason = "hold_expiry"   # backstop: don't ride indefinitely
        elif _mkt_open and pos.first_green and ret >= 0.08 and days_elapsed == 0:
            pos.peak_tracking = True
            pos.peak_price = price
        elif _mkt_open and pos.first_green and ret >= pos.first_green_pct and days_elapsed >= 1:
            if ride:
                # Ride toward the peak: flip into peak-tracking instead of
                # taking the small first-green win.
                pos.peak_tracking = True
                pos.peak_price = price
            else:
                reason = "first_green"
        elif _mkt_open and days_elapsed >= pos.hold_days:
            reason = "hold_expiry"

        if reason:
            pnl = (price - pos.entry_price) * pos.shares
            pct = price / pos.entry_price - 1.0
            trade = ClosedTrade(
                ticker=pos.ticker,
                detector=pos.detector,
                entry_price=pos.entry_price,
                exit_price=round(price, 4),
                shares=pos.shares,
                entry_dt=pos.entry_dt,
                exit_dt=datetime.now(tz=timezone.utc).isoformat(),
                pnl=round(pnl, 2),
                pct_return=round(pct, 4),
                exit_reason=reason,
                headline=pos.headline,
                peak_price=round(pos.peak_price, 4) if pos.peak_tracking else 0.0,
                severity=pos.severity,
            )
            portfolio.cash += pos.shares * price
            portfolio.trades.append(trade)
            closed.append(trade)
        else:
            pos.days_held = days_elapsed
            remaining.append(pos)

    portfolio.positions = remaining
    return closed


_EDGAR_DETECTORS = {"activist_13d", "insider_cluster"}


def _make_edgar_client() -> "EdgarClient | None":
    """Create an EdgarClient if the env var is set, else return None."""
    import os
    ua = os.environ.get("SWITCHING_EDGAR_UA")
    if not ua:
        log.info("SWITCHING_EDGAR_UA not set — EDGAR-based detectors will be skipped")
        return None
    from switching.sources.sec_edgar import EdgarClient
    return EdgarClient(user_agent=ua)


def scan_for_signals(
    detectors: Sequence[str],
    since: datetime,
    *,
    min_severity: float = 0.0,
    feeds_override: "tuple[str, ...] | None" = None,
    market: str = "us",
) -> list[Signal]:
    from switching import registry
    from switching.sources import rss
    registry.load_builtin_detectors()

    edgar_client = None
    if any(d in _EDGAR_DETECTORS for d in detectors):
        edgar_client = _make_edgar_client()

    # Most detectors call rss.fetch() without a market arg, so set the default
    # for this scan. Reset afterwards so US scans (and tests) aren't affected.
    rss.set_default_market(market)
    signals: list[Signal] = []
    seen: set[tuple[str, str, str]] = set()
    try:
        for name in detectors:
            cls = registry.get(name)
            if name in _EDGAR_DETECTORS:
                det = cls(client=edgar_client)
            else:
                det = cls(feeds=feeds_override) if feeds_override is not None else cls()
            try:
                count = 0
                for sig in det.scan(since):
                    count += 1
                    key = sig.dedup_key()
                    if key in seen:
                        continue
                    seen.add(key)
                    if sig.severity >= min_severity:
                        signals.append(sig)
                log.info("detector %s produced %d signal(s)", name, count)
            except Exception as exc:
                log.warning("scan failed for %s: %s", name, exc)
    finally:
        rss.set_default_market("us")
    return signals


def _check_peak_exits_only(portfolio: Portfolio) -> list[ClosedTrade]:
    """Check ONLY peak-tracking positions for the 0.5%-drop trailing exit.
    Never touches non-peak positions.
    """
    closed: list[ClosedTrade] = []
    remaining: list[Position] = []
    for pos in portfolio.positions:
        if not pos.peak_tracking:
            remaining.append(pos)
            continue
        price = get_current_price(pos.ticker)
        if price is None:
            remaining.append(pos)
            continue
        if price > pos.peak_price:
            pos.peak_price = price
        drop = (pos.peak_price - price) / pos.peak_price if pos.peak_price > 0 else 0
        if drop >= 0.005:
            pnl = (price - pos.entry_price) * pos.shares
            pct = price / pos.entry_price - 1.0
            trade = ClosedTrade(
                ticker=pos.ticker,
                detector=pos.detector,
                entry_price=pos.entry_price,
                exit_price=round(price, 4),
                shares=pos.shares,
                entry_dt=pos.entry_dt,
                exit_dt=datetime.now(tz=timezone.utc).isoformat(),
                pnl=round(pnl, 2),
                pct_return=round(pct, 4),
                exit_reason="peak_trailing",
                headline=pos.headline,
                peak_price=round(pos.peak_price, 4),
            )
            portfolio.cash += pos.shares * price
            portfolio.trades.append(trade)
            closed.append(trade)
        else:
            remaining.append(pos)
    portfolio.positions = remaining
    return closed


def _poll_peak_positions(
    portfolio: Portfolio,
    *,
    until: float,
    notifications,
    exit_tracker,
    tracker_path: Path,
    state_path: Path,
) -> None:
    """1-second poll loop for peak-tracking positions only.
    Runs until `until` (epoch time) or all peak-tracking positions have exited.
    """
    import time as _time
    while _time.time() < until:
        if not any(p.peak_tracking for p in portfolio.positions):
            break
        _time.sleep(1)
        closed = _check_peak_exits_only(portfolio)
        for t in closed:
            notifications.notify_sell(
                ticker=t.ticker,
                exit_price=t.exit_price,
                pnl=t.pnl,
                pct_return=t.pct_return,
                exit_reason=t.exit_reason,
                detector=t.detector,
            )
            exit_tracker.add_trade(t)
        if closed:
            from switching import storage
            exit_tracker.save(tracker_path, storage.service_from_path(state_path))
            portfolio.save(state_path)


def _build_review_insights(portfolio: Portfolio, exit_tracker) -> list[str]:
    """Generate review insights from trade history and post-exit tracking data."""
    from switching.exit_tracker import ExitTracker
    insights: list[str] = []
    total_trades = len(portfolio.trades)

    # Trade count milestone (report the highest crossed)
    for milestone in (100, 50, 25, 10):
        if total_trades >= milestone:
            insights.append(
                f"Milestone: {total_trades} trades completed — review exit profiles and detector win rates"
            )
            break

    # Per-detector win rates (need 10+ trades to be meaningful)
    by_detector: dict[str, list] = {}
    for t in portfolio.trades:
        by_detector.setdefault(t.detector, []).append(t)

    for det, trades in sorted(by_detector.items()):
        if len(trades) < 10:
            continue
        wins = sum(1 for t in trades if t.pnl > 0)
        win_rate = wins / len(trades)
        avg_ret = sum(t.pct_return for t in trades) / len(trades)
        if win_rate < 0.55:
            insights.append(
                f"{det}: {win_rate:.0%} win rate over {len(trades)} trades "
                f"(avg {avg_ret*100:+.1f}%) — below 55% target, consider disabling"
            )
        elif win_rate >= 0.70:
            insights.append(
                f"{det}: {win_rate:.0%} win rate over {len(trades)} trades — strong performer"
            )

    # Post-exit insights from the exit tracker
    completed = [t for t in exit_tracker.tracked if t.tracking_complete]
    insights.extend(exit_tracker._generate_insights(completed))

    return insights


def run_loop(
    *,
    state_path: Path,
    seed_cash: float = 1000.0,
    detectors: Sequence[str],
    stop_loss: float = 0.05,
    hold_days: int = 5,
    scan_interval_minutes: int = 30,
    min_severity: float = 0.0,
    max_position_pct: float = 0.20,
    max_positions: int = 5,
    once: bool = False,
    market: str = "us",
) -> Portfolio:
    portfolio = Portfolio.load(state_path)
    if not portfolio.trades and not portfolio.positions and portfolio.cash == 1000.0:
        portfolio.cash = seed_cash
    portfolio.max_position_pct = max_position_pct
    portfolio.max_positions = max_positions

    from rich.console import Console
    from switching import notifications, storage, detection_funnel
    from switching.exit_tracker import ExitTracker
    from switching.skipped_tracker import SkippedTracker
    console = Console()

    service = storage.service_from_path(state_path)
    # Tag every Telegram message with the market so UK/US alerts are distinct.
    notifications.set_market(service)
    # Capture classified-but-no-ticker drops for this service (detection funnel).
    detection_funnel.configure(service, state_path)

    tracker_path = state_path.parent / "exit_tracker.json"
    exit_tracker = ExitTracker.load(tracker_path, service)

    skipped_path = state_path.parent / "skipped_signals.json"
    skipped_tracker = SkippedTracker.load(skipped_path, service)

    if notifications.is_configured():
        notifications.notify_startup(
            cash=portfolio.cash,
            portfolio_value=portfolio.total_value,
            open_positions=len(portfolio.positions),
            total_trades=len(portfolio.trades),
            detectors=list(detectors),
            scan_interval=scan_interval_minutes,
        )

    last_summary_date = ""

    while True:
        now = datetime.now(tz=timezone.utc)
        console.print(f"\n[bold]── Scan at {now.strftime('%Y-%m-%d %H:%M UTC')} ──[/bold]")
        _sym = "£" if market == "uk" else "$"
        console.print(f"Cash: {_sym}{portfolio.cash:.2f} | Positions: {len(portfolio.positions)} | Total: {_sym}{portfolio.total_value:.2f}")

        closed = check_exits(portfolio, market=market)
        for t in closed:
            color = "green" if t.pnl >= 0 else "red"
            console.print(f"  [{color}]CLOSED {t.ticker} {t.exit_reason}: {t.pct_return*100:+.2f}% (${t.pnl:+.2f})[/{color}]")
            notifications.notify_sell(
                ticker=t.ticker,
                exit_price=t.exit_price,
                pnl=t.pnl,
                pct_return=t.pct_return,
                exit_reason=t.exit_reason,
                detector=t.detector,
            )
            exit_tracker.add_trade(t)

        # --- Daily OHLC snapshot for every open position (recorded once per day) ---
        today_str = now.strftime("%Y-%m-%d")
        for pos in portfolio.positions:
            if not pos.snapshots or pos.snapshots[-1]["date"] != today_str:
                ohlc = get_intraday_data(pos.ticker)
                if ohlc:
                    pos.snapshots.append({
                        "date": today_str,
                        "day": len(pos.snapshots) + 1,
                        "open":  round(ohlc["open"],  4),
                        "high":  round(ohlc["high"],  4),
                        "low":   round(ohlc["low"],   4),
                        "close": round(ohlc["close"], 4),
                        "pct_from_entry":  round(ohlc["close"] / pos.entry_price - 1.0, 4),
                        "high_pct":        round(ohlc["high"]  / pos.entry_price - 1.0, 4),
                        "low_pct":         round(ohlc["low"]   / pos.entry_price - 1.0, 4),
                    })

        tracked = exit_tracker.update(get_intraday_data)
        if tracked:
            console.print(f"  [dim]Post-exit tracker: updated {tracked} price(s), {exit_tracker.active_count} active[/dim]")
        exit_tracker.save(tracker_path, service)

        skipped_updated = skipped_tracker.update(get_intraday_data)
        if skipped_updated:
            console.print(f"  [dim]Skipped-signal tracker: updated {skipped_updated} price(s), {skipped_tracker.active_count} active, {skipped_tracker.completed_count} completed[/dim]")
        skipped_tracker.save(skipped_path, service)

        since = now - timedelta(hours=24)
        feeds_override = None
        if market == "uk":
            from switching.sources import rss as _rss
            feeds_override = _rss.UK_FEEDS
        signals = scan_for_signals(
            detectors, since, min_severity=min_severity,
            feeds_override=feeds_override, market=market,
        )

        from switching.trade_memory import load_memory, update_memory
        from switching.ai_filter import score_signals

        memory_path = state_path.parent / "trade_memory.json"
        if portfolio.trades:
            memory = update_memory(portfolio.trades, memory_path, service)
        else:
            memory = load_memory(memory_path, service)

        if signals:
            signals = score_signals(signals, memory=memory)

        portfolio.last_signals = [s.to_dict() for s in signals]
        portfolio.last_scan_dt = now.isoformat()

        new_signals = [
            s for s in signals
            if _signal_key(s) not in portfolio.seen_signals
        ]

        if new_signals:
            console.print(f"  Found {len(new_signals)} new signal(s)")
        for sig in new_signals:
            # Skip mna_target signals where the detected company is the ACQUIRER,
            # not the target. Acquirers typically drop on deal announcement day
            # (dilution / deal risk). Live data confirmed: 100% of acquirer-tagged
            # mna_target trades hit stop-loss and many kept falling.
            if sig.detector == "mna_target" and sig.extra.get("direction") == "acquirer":
                log.info("mna_target acquirer signal skipped for %s: %s", sig.ticker, sig.headline[:80])
                console.print(f"  [dim]SKIP {sig.ticker} (mna_target acquirer — direction filter)[/dim]")
                portfolio.seen_signals.append(_signal_key(sig))
                continue

            price = get_current_price(sig.ticker)
            if price is None:
                console.print(f"  [yellow]SKIP {sig.ticker}: no price available[/yellow]")
                notifications.notify_skip(sig.ticker, "no price available", sig.detector, sig.headline)
                detection_funnel.record_signal_drop(sig.detector, sig, "price_unavailable")
                portfolio.seen_signals.append(_signal_key(sig))
                continue
            pos = open_position(portfolio, sig, price, stop_loss=stop_loss, hold_days=hold_days, market=market)
            if pos:
                console.print(
                    f"  [cyan]BUY {pos.ticker} @ ${pos.entry_price:.2f} "
                    f"x {pos.shares:.4f} shares (${pos.cost_basis:.2f}) "
                    f"— {sig.detector}: {sig.headline[:60]}[/cyan]"
                )
                notifications.notify_buy(
                    ticker=pos.ticker,
                    price=pos.entry_price,
                    shares=pos.shares,
                    cost=pos.cost_basis,
                    detector=sig.detector,
                    headline=sig.headline,
                    severity=sig.severity,
                    ai_score=sig.extra.get("ai_score"),
                )
            elif pos is None and price is not None:
                if portfolio.max_positions > 0 and len(portfolio.positions) >= portfolio.max_positions:
                    skip_reason = "max_positions"
                elif any(p.ticker == sig.ticker for p in portfolio.positions):
                    skip_reason = "already_holding"
                elif _normalise_price(price, market) < 1.0:
                    skip_reason = "price_too_low"
                else:
                    alloc = portfolio.total_value * portfolio.max_position_pct
                    alloc = min(alloc, portfolio.cash)
                    skip_reason = "insufficient_cash" if alloc < 1.0 else "unknown"
                notifications.notify_skip(sig.ticker, skip_reason.replace("_", " "), sig.detector, sig.headline)
                portfolio.seen_signals.append(_signal_key(sig))
                if skip_reason != "already_holding":
                    profile = _exit_profile(sig.detector, price)
                    skipped_tracker.add(
                        ticker=sig.ticker,
                        detector=sig.detector,
                        severity=sig.severity,
                        headline=sig.headline,
                        skip_reason=skip_reason,
                        price=price,
                        hold_days=profile["hold_days"],
                        first_green=profile["first_green"],
                        first_green_pct=profile["first_green_pct"],
                        stop_loss_pct=_tiered_stop_loss(stop_loss, price, market),
                    )

        held_tickers = {p.ticker for p in portfolio.positions}
        portfolio.cached_prices = {
            t: v for t, v in portfolio.cached_prices.items() if t in held_tickers
        }
        if portfolio.positions:
            console.print("  [dim]Open positions:[/dim]")
            for p in portfolio.positions:
                cur = get_current_price(p.ticker)
                if cur:
                    cur = _normalise_price(cur, market)
                    portfolio.cached_prices[p.ticker] = cur
                    ret = (cur / p.entry_price - 1.0) * 100
                    sym = "£" if market == "uk" else "$"
                    color = "green" if ret >= 0 else "red"
                    console.print(f"    {p.ticker}: entry {sym}{p.entry_price:.2f} now {sym}{cur:.2f} [{color}]{ret:+.1f}%[/{color}] day {p.days_held}/{p.hold_days}")
                else:
                    console.print(f"    {p.ticker}: entry ${p.entry_price:.2f} (price unavailable) day {p.days_held}/{p.hold_days}")

        wins = sum(1 for t in portfolio.trades if t.pnl > 0)
        total = len(portfolio.trades)
        if total:
            wr = wins / total * 100
            total_pnl = sum(t.pnl for t in portfolio.trades)
            console.print(f"  [dim]History: {total} trades, {wins} wins ({wr:.0f}%), total P&L: ${total_pnl:+.2f}[/dim]")

        today_str = now.strftime("%Y-%m-%d")

        # Once-daily review digest (Telegram + persisted for dashboard)
        if portfolio.last_review_sent_dt != today_str:
            review_insights = _build_review_insights(portfolio, exit_tracker)
            if review_insights:
                notifications.notify_review_digest(review_insights, len(portfolio.trades))
                portfolio.last_review_sent_dt = today_str

        market_closed = now.hour >= 21
        if market_closed and today_str != last_summary_date:
            last_summary_date = today_str
            notifications.flush_buy_queue()
            todays_trades = [
                {"ticker": t.ticker, "pnl": t.pnl, "pct_return": t.pct_return, "exit_reason": t.exit_reason}
                for t in portfolio.trades
                if t.exit_dt.startswith(today_str)
            ]
            wins = sum(1 for t in portfolio.trades if t.pnl > 0)
            total_pnl = sum(t.pnl for t in portfolio.trades)
            notifications.notify_daily_summary(
                cash=portfolio.cash,
                portfolio_value=portfolio.total_value,
                positions=[
                    {"ticker": p.ticker, "entry_price": p.entry_price, "days_held": p.days_held, "hold_days": p.hold_days}
                    for p in portfolio.positions
                ],
                todays_trades=todays_trades,
                total_trades=len(portfolio.trades),
                total_wins=wins,
                total_pnl=total_pnl,
            )

        # Weekly report — every Saturday morning (UTC) after market open
        # Fires once per week; the last-sent timestamp prevents duplicates.
        if now.weekday() == 5 and now.hour >= 9:   # Saturday, 09:00+ UTC
            last_wr = portfolio.last_weekly_report_dt
            already_sent_this_week = False
            if last_wr:
                try:
                    last_wr_dt = datetime.fromisoformat(last_wr)
                    already_sent_this_week = (now - last_wr_dt).days < 6
                except ValueError:
                    pass
            if not already_sent_this_week:
                from switching.weekly_report import generate_and_send
                console.print("  [cyan]Generating weekly report...[/cyan]")
                ok = generate_and_send(state_path.parent)
                if ok:
                    portfolio.last_weekly_report_dt = now.isoformat()
                    console.print("  [cyan]Weekly report sent via Telegram.[/cyan]")
                else:
                    console.print("  [yellow]Weekly report send failed — will retry next cycle.[/yellow]")

        portfolio.save(state_path)

        if once:
            break

        next_scan_at = time.time() + scan_interval_minutes * 60
        if any(p.peak_tracking for p in portfolio.positions):
            console.print(f"  [yellow]Peak-tracking active — polling every 60s until next scan[/yellow]")
            _poll_peak_positions(
                portfolio,
                until=next_scan_at,
                notifications=notifications,
                exit_tracker=exit_tracker,
                tracker_path=tracker_path,
                state_path=state_path,
            )
        remaining = next_scan_at - time.time()
        if remaining > 0:
            console.print(f"  [dim]Next scan in {remaining/60:.1f}m...[/dim]")
            time.sleep(remaining)

    return portfolio


def run_loop_alpaca(
    *,
    state_path: Path,
    detectors: Sequence[str],
    stop_loss: float = 0.05,
    hold_days: int = 5,
    scan_interval_minutes: int = 30,
    min_severity: float = 0.0,
    max_position_pct: float = 0.20,
    max_positions: int = 5,
    once: bool = False,
) -> None:
    from switching.broker_alpaca import AlpacaClient
    from rich.console import Console

    client = AlpacaClient()
    console = Console()
    mode = "[bold yellow]PAPER[/bold yellow]" if client.paper else "[bold red]LIVE[/bold red]"

    portfolio = Portfolio.load(state_path)
    portfolio.max_position_pct = max_position_pct
    portfolio.max_positions = max_positions

    while True:
        now = datetime.now(tz=timezone.utc)
        console.print(f"\n[bold]── {mode} Scan at {now.strftime('%Y-%m-%d %H:%M UTC')} ──[/bold]")

        acct = client.get_account()
        console.print(f"Cash: ${acct.cash:.2f} | Equity: ${acct.equity:.2f} | Buying power: ${acct.buying_power:.2f}")

        positions = client.get_positions()
        if not client.is_market_open():
            console.print("  [yellow]Market closed — monitoring only[/yellow]")
            if positions:
                for p in positions:
                    color = "green" if p.unrealized_pnl >= 0 else "red"
                    console.print(f"    {p.ticker}: {p.qty} shares @ ${p.avg_entry_price:.2f} [{color}]{p.unrealized_pnl_pct*100:+.1f}% (${p.unrealized_pnl:+.2f})[/{color}]")
            portfolio.save(state_path)
            if once:
                break
            console.print(f"  [dim]Next scan in {scan_interval_minutes}m...[/dim]")
            time.sleep(scan_interval_minutes * 60)
            continue

        for p in positions:
            ret = p.unrealized_pnl_pct
            color = "green" if ret >= 0 else "red"

            tracker = next((t for t in portfolio.positions if t.ticker == p.ticker), None)
            days = _calendar_days_since(tracker.entry_dt) if tracker else 0

            should_sell = False
            reason = ""
            if ret <= -stop_loss:
                should_sell = True
                reason = "stop_loss"
            elif ret > 0:
                should_sell = True
                reason = "first_green"
            elif days >= hold_days:
                should_sell = True
                reason = "hold_expiry"

            if should_sell:
                try:
                    client.cancel_orders_for(p.ticker)
                    order = client.sell_all(p.ticker)
                    console.print(f"  [{color}]SELL {p.ticker} ({reason}): {ret*100:+.1f}% ${p.unrealized_pnl:+.2f} — order {order.status}[/{color}]")
                    portfolio.trades.append(ClosedTrade(
                        ticker=p.ticker,
                        detector=tracker.detector if tracker else "unknown",
                        entry_price=p.avg_entry_price,
                        exit_price=p.current_price,
                        shares=p.qty,
                        entry_dt=tracker.entry_dt if tracker else "",
                        exit_dt=now.isoformat(),
                        pnl=round(p.unrealized_pnl, 2),
                        pct_return=round(ret, 4),
                        exit_reason=reason,
                        headline=tracker.headline if tracker else "",
                    ))
                    portfolio.positions = [x for x in portfolio.positions if x.ticker != p.ticker]
                except Exception as exc:
                    console.print(f"  [red]SELL FAILED {p.ticker}: {exc}[/red]")
            else:
                if tracker:
                    tracker.days_held = days
                console.print(f"    {p.ticker}: {p.qty} shares @ ${p.avg_entry_price:.2f} [{color}]{ret*100:+.1f}%[/{color}] day {days}/{hold_days}")

        since = now - timedelta(hours=24)
        signals = scan_for_signals(detectors, since, min_severity=min_severity)
        new_signals = [
            s for s in signals
            if _signal_key(s) not in portfolio.seen_signals
        ]

        held_tickers = {p.ticker for p in positions}
        if new_signals:
            console.print(f"  Found {len(new_signals)} new signal(s)")

        for sig in new_signals:
            portfolio.seen_signals.append(_signal_key(sig))

            if sig.ticker in held_tickers:
                console.print(f"  [dim]SKIP {sig.ticker}: already holding[/dim]")
                continue
            if len(positions) >= max_positions:
                console.print(f"  [dim]SKIP {sig.ticker}: max positions ({max_positions})[/dim]")
                continue

            alloc = acct.equity * max_position_pct
            alloc = min(alloc, acct.buying_power, acct.cash)
            if alloc < 1.0:
                console.print(f"  [yellow]SKIP {sig.ticker}: insufficient buying power[/yellow]")
                continue

            try:
                order = client.buy_market(sig.ticker, notional=round(alloc, 2))
                console.print(
                    f"  [cyan]BUY {sig.ticker} ${alloc:.2f} notional — order {order.status} "
                    f"— {sig.detector}: {sig.headline[:60]}[/cyan]"
                )
                price = client.get_quote(sig.ticker) or 0
                shares = alloc / price if price > 0 else 0
                portfolio.positions.append(Position(
                    ticker=sig.ticker,
                    detector=sig.detector,
                    entry_price=price,
                    shares=shares,
                    entry_dt=now.isoformat(),
                    headline=sig.headline,
                    severity=sig.severity,
                    stop_loss=stop_loss,
                    hold_days=hold_days,
                ))
                held_tickers.add(sig.ticker)
            except Exception as exc:
                console.print(f"  [red]BUY FAILED {sig.ticker}: {exc}[/red]")

        wins = sum(1 for t in portfolio.trades if t.pnl > 0)
        total_trades = len(portfolio.trades)
        if total_trades:
            wr = wins / total_trades * 100
            total_pnl = sum(t.pnl for t in portfolio.trades)
            console.print(f"  [dim]History: {total_trades} trades, {wins} wins ({wr:.0f}%), total P&L: ${total_pnl:+.2f}[/dim]")

        portfolio.save(state_path)

        if once:
            break

        console.print(f"  [dim]Next scan in {scan_interval_minutes}m...[/dim]")
        time.sleep(scan_interval_minutes * 60)


# ---------------------------------------------------------------------------
# Trading 212 live-broker loop
# ---------------------------------------------------------------------------

def run_loop_t212(
    *,
    state_path: Path,
    detectors: Sequence[str],
    stop_loss: float = 0.026,
    hold_days: int = 5,
    scan_interval_minutes: int = 10,
    min_severity: float = 0.0,
    max_position_pct: float = 0.01,
    max_positions: int = 0,
    once: bool = False,
) -> None:
    """Trading loop that executes orders via the Trading 212 REST API.

    Uses the same detector-specific exit profiles as the internal paper trader
    so results can be compared side-by-side.  All fills come from T212
    (demo or live), providing realistic slippage data vs the yfinance
    theoretical fills used by the internal simulation.

    State is persisted to *state_path* (separate from the internal paper
    trader's portfolio JSON so both can run simultaneously).

    Env vars required:
        T212_API_KEY  — key from Settings → API in the T212 app
        T212_DEMO     — "true" (default) for demo account
    """
    from switching.broker_trading212 import Trading212Client, T212AuthError, T212OrderError
    from rich.console import Console

    console = Console()

    try:
        client = Trading212Client()
    except T212AuthError as exc:
        console.print(f"[red]Trading 212 auth error: {exc}[/red]")
        return

    mode = "[bold yellow]T212 DEMO[/bold yellow]" if client.demo else "[bold red]T212 LIVE — REAL MONEY[/bold red]"

    if not client.demo:
        console.print("\n[bold red]⚠  WARNING: T212_DEMO=false — THIS WILL PLACE REAL ORDERS WITH REAL MONEY.[/bold red]")
        console.print("[bold red]   Press Ctrl-C within 10 seconds to abort.[/bold red]\n")
        time.sleep(10)
        console.print("[yellow]Proceeding with live trading...[/yellow]")

    portfolio = Portfolio.load(state_path)
    portfolio.max_position_pct = max_position_pct
    portfolio.max_positions = max_positions

    # Full analytics, same as the paper traders — service-scoped to 't212' so
    # this data is separate from US/UK but collected identically. This gives
    # post-exit / skipped / memory analysis on REAL broker fills.
    from switching import storage
    from switching.exit_tracker import ExitTracker
    from switching.skipped_tracker import SkippedTracker
    from switching.trade_memory import update_memory
    from switching import notifications, detection_funnel
    service = storage.service_from_path(state_path)            # "t212"
    notifications.set_market(service)   # tag Telegram messages as T212
    detection_funnel.configure(service, state_path)            # capture drops
    tracker_path = state_path.parent / "exit_tracker.json"
    skipped_path = state_path.parent / "skipped_signals.json"
    memory_path = state_path.parent / "trade_memory.json"
    exit_tracker = ExitTracker.load(tracker_path, service)
    skipped_tracker = SkippedTracker.load(skipped_path, service)

    # Exits are polled every _T212_EXIT_POLL_SECONDS (default 60s) for tight
    # price tracking and fast stop-loss / first-green execution.  New-signal
    # scanning runs only every scan_interval_minutes to avoid hammering feeds.
    last_signal_scan = 0.0   # epoch seconds; 0 forces a scan on first iteration

    while True:
        now = datetime.now(tz=timezone.utc)
        _prune_recently_sold(portfolio.recently_sold, now)
        console.print(f"\n[bold]── {mode} Poll at {now.strftime('%Y-%m-%d %H:%M UTC')} ──[/bold]")

        try:
            acct = client.get_account()
        except Exception as exc:
            console.print(f"[red]T212 account fetch failed: {exc}[/red]")
            if once:
                break
            time.sleep(_T212_EXIT_POLL_SECONDS)
            continue

        # Use the actual T212 account currency (e.g. GBP for UK demo) for
        # account-level + pnl values. Per-position stock prices stay $ since
        # they're quoted in the instrument's currency (USD for US tickers).
        from switching.broker_trading212 import currency_symbol
        cs = currency_symbol(acct.currency)
        console.print(
            f"Free: {cs}{acct.free:.2f} | Invested: {cs}{acct.invested:.2f} | "
            f"Total: {cs}{acct.total:.2f} | P&L: {cs}{acct.ppl:+.2f}"
        )

        # ----------------------------------------------------------------
        # Exit checks — iterate over T212 positions
        # ----------------------------------------------------------------
        positions_ok = True
        try:
            t212_positions = client.get_positions()
        except Exception as exc:
            console.print(f"[red]T212 positions fetch failed: {exc}[/red]")
            t212_positions = []
            positions_ok = False   # do NOT reconcile ghosts off a failed fetch

        t212_map = {p.symbol: p for p in t212_positions}

        if not client.is_market_open():
            console.print("  [yellow]Market closed — monitoring only[/yellow]")
            for sym, tp in t212_map.items():
                color = "green" if tp.unrealized_pnl_pct >= 0 else "red"
                console.print(
                    f"    {sym}: {tp.quantity:.4f} @ ${tp.avg_entry_price:.2f} "
                    f"[{color}]{tp.unrealized_pnl_pct*100:+.1f}% "
                    f"({cs}{tp.unrealized_pnl:+.2f})[/{color}]"
                )
            portfolio.save(state_path)
            if once:
                break
            console.print(f"  [dim]Next scan in {scan_interval_minutes}m...[/dim]")
            time.sleep(scan_interval_minutes * 60)
            continue

        # Positions we track locally (for detector/profile metadata)
        local_map = {p.ticker: p for p in portfolio.positions}

        # Reconcile: T212 positions that have no local tracker (can happen after
        # a container restart that lost the state file, or if the position was
        # opened before this service started).  Create a synthetic tracker so
        # the exit logic has a stable reference and doesn't sell immediately.
        #
        # CRITICAL: skip symbols we sold within the settlement window. After a
        # sell the position is removed locally but T212 may still report it for
        # a short time while the order settles. Re-adopting it here would issue
        # a SECOND sell and record a duplicate trade.
        settling: set[str] = set()
        for sym, tp in t212_map.items():
            if sym not in local_map:
                mins = _minutes_since(portfolio.recently_sold.get(sym, ""), now)
                if mins is not None and mins < _T212_SETTLE_MINUTES:
                    settling.add(sym)
                    console.print(
                        f"  [dim]{sym}: sell settling ({mins:.0f}m ago) — skipping[/dim]"
                    )
                    continue
                log.info(
                    "T212 orphan position %s — creating synthetic tracker "
                    "(entry_price=%.4f, qty=%.4f)",
                    sym, tp.avg_entry_price, tp.quantity,
                )
                synthetic = Position(
                    ticker=sym,
                    detector="t212_orphan",
                    entry_price=tp.avg_entry_price,
                    shares=tp.quantity,
                    entry_dt=now.isoformat(),
                    headline="[position pre-existed — no local record]",
                    severity=0.0,
                    stop_loss=_tiered_stop_loss(stop_loss, tp.avg_entry_price),
                    hold_days=hold_days,
                    first_green=True,
                    first_green_pct=0.0,
                )
                portfolio.positions.append(synthetic)
                local_map[sym] = synthetic
                console.print(f"  [yellow]SYNC {sym}: orphan T212 position adopted[/yellow]")

        for sym, tp in list(t212_map.items()):
            if sym in settling:
                continue   # order still settling — don't touch
            # Cache last-seen price (for the dashboard + a best-effort exit
            # estimate if this position is later closed by a corporate action).
            portfolio.cached_prices[sym] = tp.current_price
            ret = tp.unrealized_pnl_pct
            color = "green" if ret >= 0 else "red"
            tracker = local_map.get(sym)
            days = trading_days_since(tracker.entry_dt) if tracker else 0

            # Apply per-detector exit profile
            profile = _exit_profile(
                tracker.detector if tracker else "unknown",
                tp.avg_entry_price,
            )
            effective_sl = _tiered_stop_loss(stop_loss, tp.avg_entry_price) + profile.get("stop_loss_extra", 0.0)
            first_green = profile.get("first_green", True)
            first_green_pct = profile.get("first_green_pct", 0.0)
            max_hold = profile.get("hold_days", hold_days)

            should_sell = False
            reason = ""

            if ret <= -effective_sl:
                should_sell = True
                reason = "stop_loss"
            elif first_green and ret >= first_green_pct:
                should_sell = True
                reason = "first_green"
            elif not first_green and days >= max_hold:
                should_sell = True
                reason = "hold_expiry"
            elif days >= max_hold:
                should_sell = True
                reason = "hold_expiry"

            if should_sell:
                try:
                    order = client.sell_all(sym, tp.quantity)
                    exit_price = tp.current_price
                    pnl = round(tp.unrealized_pnl, 2)
                    console.print(
                        f"  [{color}]SELL {sym} ({reason}): "
                        f"{ret*100:+.1f}% {cs}{pnl:+.2f} — order {order.status}[/{color}]"
                    )
                    closed = ClosedTrade(
                        ticker=sym,
                        detector=tracker.detector if tracker else "unknown",
                        entry_price=tp.avg_entry_price,
                        exit_price=exit_price,
                        shares=tp.quantity,
                        entry_dt=tracker.entry_dt if tracker else now.isoformat(),
                        exit_dt=now.isoformat(),
                        pnl=pnl,
                        pct_return=round(ret, 4),
                        exit_reason=reason,
                        headline=tracker.headline if tracker else "",
                        severity=tracker.severity if tracker else 0.0,
                    )
                    portfolio.trades.append(closed)
                    exit_tracker.add_trade(closed)   # begin 20-day post-exit tracking
                    portfolio.positions = [p for p in portfolio.positions if p.ticker != sym]
                    # Mark as recently sold so the next poll doesn't re-adopt the
                    # still-settling position and re-sell it (double-sell guard),
                    # and so we don't immediately re-buy the same ticker (churn).
                    portfolio.recently_sold[sym] = now.isoformat()
                except (T212OrderError, Exception) as exc:
                    console.print(f"  [red]SELL FAILED {sym}: {exc}[/red]")
            else:
                if tracker:
                    tracker.days_held = days
                console.print(
                    f"    {sym}: {tp.quantity:.4f} @ ${tp.avg_entry_price:.2f} "
                    f"[{color}]{ret*100:+.1f}%[/{color}] day {days}/{max_hold}"
                )

        # ----------------------------------------------------------------
        # Ghost reconciliation — local positions T212 no longer reports were
        # closed externally (corporate action: M&A cash-out, delisting,
        # liquidation, or a ticker change). Record + remove them so they don't
        # ghost forever, block re-buys, or skew analytics. Gated on a SUCCESSFUL
        # positions fetch with a sane account state, so a transient T212 glitch
        # (empty list but money still invested) can't wrongly close everything.
        # ----------------------------------------------------------------
        if positions_ok and (t212_map or acct.invested < 1.0):
            ghosts = _reconcile_t212_ghosts(portfolio, set(t212_map.keys()), now)
            for g in ghosts:
                console.print(
                    f"  [magenta]RECONCILE {g.ticker}: gone from T212 — recorded as "
                    f"corporate_action close (check T212 for the real realized P&L)[/magenta]"
                )
                try:
                    notifications.notify_sell(
                        ticker=g.ticker, exit_price=g.exit_price, pnl=g.pnl,
                        pct_return=g.pct_return, exit_reason="corporate_action",
                        detector=g.detector,
                    )
                except Exception:
                    pass

        # ----------------------------------------------------------------
        # Signal scan — buy new positions (only every scan_interval_minutes;
        # exit polling above runs every cycle for tight price tracking)
        # ----------------------------------------------------------------
        do_signal_scan = (time.time() - last_signal_scan) >= (scan_interval_minutes * 60)
        new_signals: list[Signal] = []
        if do_signal_scan:
            last_signal_scan = time.time()
            since = now - timedelta(hours=24)
            signals = scan_for_signals(detectors, since, min_severity=min_severity)
            new_signals = [s for s in signals if _signal_key(s) not in portfolio.seen_signals]

        held_symbols = set(t212_map.keys()) | {p.ticker for p in portfolio.positions}
        active_count = len(t212_map)
        pending_buys: list[tuple] = []   # (signal, fallback_price, fallback_qty)

        if new_signals:
            console.print(f"  Found {len(new_signals)} new signal(s)")

        for sig in new_signals:
            portfolio.seen_signals.append(_signal_key(sig))

            # Skip acquirer-direction M&A signals
            if sig.detector == "mna_target" and sig.extra.get("direction") == "acquirer":
                console.print(f"  [dim]SKIP {sig.ticker} (mna_target acquirer)[/dim]")
                continue

            if sig.ticker in held_symbols:
                console.print(f"  [dim]SKIP {sig.ticker}: already holding[/dim]")
                continue

            # Re-buy cooldown — don't churn back into a ticker we just exited
            sold_mins = _minutes_since(portfolio.recently_sold.get(sig.ticker, ""), now)
            if sold_mins is not None and sold_mins < _T212_REBUY_COOLDOWN_HOURS * 60:
                console.print(
                    f"  [dim]SKIP {sig.ticker}: sold {sold_mins/60:.1f}h ago "
                    f"(cooldown {_T212_REBUY_COOLDOWN_HOURS}h)[/dim]"
                )
                continue

            if max_positions > 0 and active_count >= max_positions:
                console.print(f"  [dim]SKIP {sig.ticker}: max positions ({max_positions})[/dim]")
                _record_t212_skip(skipped_tracker, sig, "max_positions", stop_loss)
                continue

            # Get price to calculate quantity
            price = get_current_price(sig.ticker)
            if not price or price <= 0:
                console.print(f"  [yellow]SKIP {sig.ticker}: price unavailable[/yellow]")
                detection_funnel.record_signal_drop(sig.detector, sig, "price_unavailable")
                continue

            # Conviction-weighted size (same model as the internal paper trader),
            # capped per-position and by available buying power.
            weight = _position_weight(sig.detector)
            alloc = acct.total * max_position_pct * weight
            alloc = min(alloc, acct.free, acct.total * _MAX_SINGLE_POSITION_PCT)
            if alloc < 1.0:
                console.print(f"  [yellow]SKIP {sig.ticker}: insufficient buying power ({cs}{acct.free:.2f})[/yellow]")
                _record_t212_skip(skipped_tracker, sig, "insufficient_cash", stop_loss, price=price)
                continue

            quantity = alloc / price

            try:
                order = client.buy_market(sig.ticker, quantity)
                console.print(
                    f"  [cyan]BUY {sig.ticker} {quantity:.4f} shares (~{cs}{alloc:.2f}) "
                    f"— order {order.status} "
                    f"— {sig.detector}: {sig.headline[:55]}[/cyan]"
                )
                # Defer the fill-price lookup — collect now, fetch positions ONCE
                # after all orders so we don't fire a /equity/positions call per buy.
                pending_buys.append((sig, price, quantity))
                held_symbols.add(sig.ticker)
                active_count += 1
            except (T212OrderError, Exception) as exc:
                console.print(f"  [red]BUY FAILED {sig.ticker}: {exc}[/red]")
                # Persist so the dashboard shows what T212 rejected and why
                # (instrument-not-found, too-small qty, rate-limited, …).
                detection_funnel.record_signal_drop(
                    sig.detector, sig,
                    f"t212_rejected: {type(exc).__name__}: {str(exc)[:120]}",
                )

        # Resolve actual fills with a SINGLE positions fetch (staggered/throttled
        # by the client), then record local trackers. Orders that haven't settled
        # yet simply fall back to the yfinance estimate — same as before, but with
        # one API call instead of one per buy.
        if pending_buys:
            try:
                fills = {p.symbol: p for p in client.get_positions()}
            except Exception as exc:
                console.print(f"  [yellow]Fill lookup failed ({exc}); using estimates[/yellow]")
                fills = {}
            for sig, fb_price, fb_qty in pending_buys:
                tp = fills.get(sig.ticker)
                actual_price = tp.avg_entry_price if tp else fb_price
                qty = tp.quantity if tp else fb_qty
                profile = _exit_profile(sig.detector, actual_price)
                actual_sl = _tiered_stop_loss(stop_loss, actual_price) + profile.get("stop_loss_extra", 0.0)
                portfolio.positions.append(Position(
                    ticker=sig.ticker,
                    detector=sig.detector,
                    entry_price=actual_price,
                    shares=qty,
                    entry_dt=now.isoformat(),
                    headline=sig.headline,
                    severity=sig.severity,
                    stop_loss=actual_sl,
                    hold_days=profile["hold_days"],
                    first_green=profile.get("first_green", True),
                    first_green_pct=profile.get("first_green_pct", 0.0),
                ))

        # ----------------------------------------------------------------
        # Analytics — post-exit tracking, skipped-signal sim, trade memory
        # (service-scoped to 't212'; daily-deduped internally so cheap at 60s)
        # ----------------------------------------------------------------
        tracked = exit_tracker.update(get_intraday_data)
        if tracked:
            console.print(f"  [dim]Post-exit tracker: updated {tracked} price(s), {exit_tracker.active_count} active[/dim]")
        exit_tracker.save(tracker_path, service)

        skipped_updated = skipped_tracker.update(get_intraday_data)
        if skipped_updated:
            console.print(f"  [dim]Skipped tracker: updated {skipped_updated}, {skipped_tracker.completed_count} completed[/dim]")
        skipped_tracker.save(skipped_path, service)

        if portfolio.trades:
            update_memory(portfolio.trades, memory_path, service)

        # ----------------------------------------------------------------
        # Summary
        # ----------------------------------------------------------------
        wins = sum(1 for t in portfolio.trades if t.pnl > 0)
        total_trades = len(portfolio.trades)
        if total_trades:
            wr = wins / total_trades * 100
            total_pnl = sum(t.pnl for t in portfolio.trades)
            console.print(
                f"  [dim]History: {total_trades} trades, {wins} wins ({wr:.0f}%), "
                f"total P&L: {cs}{total_pnl:+.2f}[/dim]"
            )

        portfolio.save(state_path)

        if once:
            break

        next_scan_in = max(0, (scan_interval_minutes * 60) - (time.time() - last_signal_scan))
        console.print(
            f"  [dim]Exit poll in {_T212_EXIT_POLL_SECONDS}s · "
            f"next signal scan in {next_scan_in/60:.0f}m...[/dim]"
        )
        time.sleep(_T212_EXIT_POLL_SECONDS)
