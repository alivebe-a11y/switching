"""Paper-trading engine.

Runs a continuous loop: scan for signals, open positions at market price,
monitor exits (first-green / stop-loss / hold expiry), and track P&L
against a simulated cash balance. All state persists to a JSON file so
the process can restart without losing history.
"""

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


def open_position(
    portfolio: Portfolio,
    signal: Signal,
    price: float,
    *,
    stop_loss: float = 0.05,
    hold_days: int = 5,
) -> Position | None:
    if len(portfolio.positions) >= portfolio.max_positions:
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

    pos = Position(
        ticker=signal.ticker,
        detector=signal.detector,
        entry_price=price,
        shares=shares,
        entry_dt=datetime.now(tz=timezone.utc).isoformat(),
        headline=signal.headline,
        severity=signal.severity,
        stop_loss=stop_loss,
        hold_days=hold_days,
    )
    portfolio.positions.append(pos)
    portfolio.seen_signals.append(_signal_key(signal))
    return pos


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

        if ret_low <= -pos.stop_loss:
            reason = "stop_loss"
            price = pos.entry_price * (1.0 - pos.stop_loss)
        elif pos.first_green and ret > 0:
            reason = "first_green"
        elif pos.days_held >= pos.hold_days:
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
            pos.days_held += 1
            remaining.append(pos)

    portfolio.positions = remaining
    return closed


def scan_for_signals(
    detectors: Sequence[str],
    since: datetime,
    *,
    min_severity: float = 0.0,
) -> list[Signal]:
    from switching import registry
    registry.load_builtin_detectors()

    signals: list[Signal] = []
    seen: set[tuple[str, str, str]] = set()
    for name in detectors:
        cls = registry.get(name)
        det = cls()
        try:
            for sig in det.scan(since):
                key = sig.dedup_key()
                if key in seen:
                    continue
                seen.add(key)
                if sig.severity >= min_severity:
                    signals.append(sig)
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
    once: bool = False,
) -> Portfolio:
    portfolio = Portfolio.load(state_path)
    if not portfolio.trades and not portfolio.positions and portfolio.cash == 1000.0:
        portfolio.cash = seed_cash

    from rich.console import Console
    console = Console()

    while True:
        now = datetime.now(tz=timezone.utc)
        console.print(f"\n[bold]── Scan at {now.strftime('%Y-%m-%d %H:%M UTC')} ──[/bold]")
        console.print(f"Cash: ${portfolio.cash:.2f} | Positions: {len(portfolio.positions)} | Total: ${portfolio.total_value:.2f}")

        closed = check_exits(portfolio)
        for t in closed:
            color = "green" if t.pnl >= 0 else "red"
            console.print(f"  [{color}]CLOSED {t.ticker} {t.exit_reason}: {t.pct_return*100:+.2f}% (${t.pnl:+.2f})[/{color}]")

        since = now - timedelta(hours=24)
        signals = scan_for_signals(detectors, since, min_severity=min_severity)

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
                portfolio.seen_signals.append(_signal_key(sig))
                continue
            pos = open_position(portfolio, sig, price, stop_loss=stop_loss, hold_days=hold_days)
            if pos:
                console.print(
                    f"  [cyan]BUY {pos.ticker} @ ${pos.entry_price:.2f} "
                    f"x {pos.shares:.4f} shares (${pos.cost_basis:.2f}) "
                    f"— {sig.detector}: {sig.headline[:60]}[/cyan]"
                )

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

        portfolio.save(state_path)

        if once:
            break

        console.print(f"  [dim]Next scan in {scan_interval_minutes}m...[/dim]")
        time.sleep(scan_interval_minutes * 60)

    return portfolio
