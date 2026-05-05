"""Paper-trading engine.

Runs a continuous loop: scan for signals, open positions at market price,
monitor exits (first-green / stop-loss / hold expiry), track P&L against
a simulated cash balance, and monitor post-exit price paths for detector
refinement. All state persists to JSON files so the process can restart
without losing history."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

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

    @property
    def total_value(self) -> float:
        return self.cash + sum(p.cost_basis for p in self.positions)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "cash": self.cash,
            "positions": [asdict(p) for p in self.positions],
            "trades": [asdict(t) for t in self.trades],
            "seen_signals": self.seen_signals[-500:],
            "last_signals": self.last_signals[-50:],
            "last_scan_dt": self.last_scan_dt,
            "max_position_pct": self.max_position_pct,
            "max_positions": self.max_positions,
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Portfolio:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            cash=data["cash"],
            positions=[Position(**p) for p in data.get("positions", [])],
            trades=[ClosedTrade(**t) for t in data.get("trades", [])],
            seen_signals=data.get("seen_signals", []),
            last_signals=data.get("last_signals", []),
            last_scan_dt=data.get("last_scan_dt", ""),
            max_position_pct=data.get("max_position_pct", 0.20),
            max_positions=data.get("max_positions", 5),
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
    return f"{sig.detector}:{sig.ticker}:{sig.event_dt.date().isoformat()}"


def _tiered_stop_loss(base_stop: float, price: float) -> float:
    """Widen stop-loss for volatile low-price stocks, tighten for large caps."""
    if price >= 30.0:
        return base_stop
    if price >= 5.0:
        return base_stop + 0.01
    return base_stop + 0.02


def _exit_profile(detector: str, price: float) -> dict:
    """Return detector-specific exit parameters based on observed performance."""
    if detector == "buyback":
        return {"first_green": False, "first_green_pct": 0.0, "hold_days": 5}
    if detector == "earnings_surprise":
        return {"first_green": True, "first_green_pct": 0.0, "hold_days": 2}
    if detector == "ai_pivot":
        if price >= 30.0:
            return {"first_green": True, "first_green_pct": 0.02, "hold_days": 5}
        return {"first_green": True, "first_green_pct": 0.0, "hold_days": 3}
    if detector == "fda_decision":
        return {"first_green": True, "first_green_pct": 0.03, "hold_days": 3}
    if detector == "analyst_upgrade":
        return {"first_green": True, "first_green_pct": 0.01, "hold_days": 3}
    if detector == "mna_target":
        return {"first_green": True, "first_green_pct": 0.03, "hold_days": 5}
    if detector == "guidance_raise":
        return {"first_green": True, "first_green_pct": 0.02, "hold_days": 3}
    if detector == "dividend_surprise":
        return {"first_green": True, "first_green_pct": 0.01, "hold_days": 3}
    if detector == "contract_win":
        return {"first_green": True, "first_green_pct": 0.02, "hold_days": 5}
    return {"first_green": True, "first_green_pct": 0.0, "hold_days": 5}


def open_position(
    portfolio: Portfolio,
    signal: Signal,
    price: float,
    *,
    stop_loss: float = 0.026,
    hold_days: int = 5,
) -> Position | None:
    if portfolio.max_positions > 0 and len(portfolio.positions) >= portfolio.max_positions:
        log.info("max positions reached, skipping %s", signal.ticker)
        return None
    for p in portfolio.positions:
        if p.ticker == signal.ticker:
            log.info("already holding %s, skipping", signal.ticker)
            return None

    alloc = portfolio.total_value * portfolio.max_position_pct
    alloc = min(alloc, portfolio.cash)
    if alloc < 1.0:
        log.info("insufficient cash ($%.2f), skipping %s", portfolio.cash, signal.ticker)
        return None

    shares = alloc / price
    cost = shares * price
    portfolio.cash -= cost

    actual_sl = _tiered_stop_loss(stop_loss, price)
    profile = _exit_profile(signal.detector, price)

    pos = Position(
        ticker=signal.ticker,
        detector=signal.detector,
        entry_price=price,
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
    """Calendar days between the position entry and now, rounded down."""
    entry = datetime.fromisoformat(entry_dt_str.replace("Z", "+00:00"))
    if entry.tzinfo is None:
        entry = entry.replace(tzinfo=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    return (now.date() - entry.date()).days


def check_exits(portfolio: Portfolio) -> list[ClosedTrade]:
    closed: list[ClosedTrade] = []
    remaining: list[Position] = []

    for pos in portfolio.positions:
        data = get_intraday_data(pos.ticker)
        if data is None:
            remaining.append(pos)
            continue

        price = data["close"]
        ret = price / pos.entry_price - 1.0
        ret_low = data["low"] / pos.entry_price - 1.0
        reason = None

        days_elapsed = _calendar_days_since(pos.entry_dt)

        if ret_low <= -pos.stop_loss:
            reason = "stop_loss"
            price = pos.entry_price * (1.0 - pos.stop_loss)
        elif pos.first_green and ret >= pos.first_green_pct:
            reason = "first_green"
        elif days_elapsed >= pos.hold_days:
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
) -> list[Signal]:
    from switching import registry
    registry.load_builtin_detectors()

    edgar_client = None
    if any(d in _EDGAR_DETECTORS for d in detectors):
        edgar_client = _make_edgar_client()

    signals: list[Signal] = []
    seen: set[tuple[str, str, str]] = set()
    for name in detectors:
        cls = registry.get(name)
        if name in _EDGAR_DETECTORS:
            det = cls(client=edgar_client)
        else:
            det = cls()
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
    return signals


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
) -> Portfolio:
    portfolio = Portfolio.load(state_path)
    if not portfolio.trades and not portfolio.positions and portfolio.cash == 1000.0:
        portfolio.cash = seed_cash
    portfolio.max_position_pct = max_position_pct
    portfolio.max_positions = max_positions

    from rich.console import Console
    from switching import notifications
    from switching.exit_tracker import ExitTracker
    console = Console()

    tracker_path = state_path.parent / "exit_tracker.json"
    exit_tracker = ExitTracker.load(tracker_path)

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
        console.print(f"Cash: ${portfolio.cash:.2f} | Positions: {len(portfolio.positions)} | Total: ${portfolio.total_value:.2f}")

        closed = check_exits(portfolio)
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

        tracked = exit_tracker.update(get_current_price)
        if tracked:
            console.print(f"  [dim]Post-exit tracker: updated {tracked} price(s), {exit_tracker.active_count} active[/dim]")
        exit_tracker.save(tracker_path)

        since = now - timedelta(hours=24)
        signals = scan_for_signals(detectors, since, min_severity=min_severity)

        from switching.trade_memory import load_memory, update_memory
        from switching.ai_filter import score_signals

        memory_path = state_path.parent / "trade_memory.json"
        if portfolio.trades:
            memory = update_memory(portfolio.trades, memory_path)
        else:
            memory = load_memory(memory_path)

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
            price = get_current_price(sig.ticker)
            if price is None:
                console.print(f"  [yellow]SKIP {sig.ticker}: no price available[/yellow]")
                notifications.notify_skip(sig.ticker, "no price available", sig.detector, sig.headline)
                portfolio.seen_signals.append(_signal_key(sig))
                continue
            pos = open_position(portfolio, sig, price, stop_loss=stop_loss, hold_days=hold_days)
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
                reason = "max positions" if len(portfolio.positions) >= portfolio.max_positions else "already holding or insufficient cash"
                notifications.notify_skip(sig.ticker, reason, sig.detector, sig.headline)

        if portfolio.positions:
            console.print("  [dim]Open positions:[/dim]")
            for p in portfolio.positions:
                cur = get_current_price(p.ticker)
                if cur:
                    ret = (cur / p.entry_price - 1.0) * 100
                    color = "green" if ret >= 0 else "red"
                    console.print(f"    {p.ticker}: entry ${p.entry_price:.2f} now ${cur:.2f} [{color}]{ret:+.1f}%[/{color}] day {p.days_held}/{p.hold_days}")
                else:
                    console.print(f"    {p.ticker}: entry ${p.entry_price:.2f} (price unavailable) day {p.days_held}/{p.hold_days}")

        wins = sum(1 for t in portfolio.trades if t.pnl > 0)
        total = len(portfolio.trades)
        if total:
            wr = wins / total * 100
            total_pnl = sum(t.pnl for t in portfolio.trades)
            console.print(f"  [dim]History: {total} trades, {wins} wins ({wr:.0f}%), total P&L: ${total_pnl:+.2f}[/dim]")

        today_str = now.strftime("%Y-%m-%d")
        market_closed = now.hour >= 21
        if market_closed and today_str != last_summary_date:
            last_summary_date = today_str
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

        portfolio.save(state_path)

        if once:
            break

        console.print(f"  [dim]Next scan in {scan_interval_minutes}m...[/dim]")
        time.sleep(scan_interval_minutes * 60)

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
