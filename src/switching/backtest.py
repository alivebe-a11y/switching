from __future__ import annotations

import csv
import json
import logging
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

from switching.pricing import PriceCache, get_history
from switching.signal import Signal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Trade:
    ticker: str
    detector: str
    event_dt: datetime
    entry_dt: date
    entry_price: float
    exit_dt: date
    exit_price: float
    hold_days: int
    severity: float
    gross_return: float          # (exit - entry) / entry
    net_return: float            # gross_return - cost_bps/10000
    headline: str
    exit_reason: str = "hold"    # hold | stop_loss | take_profit | first_green

    def to_dict(self) -> dict:
        d = asdict(self)
        d["event_dt"] = self.event_dt.isoformat()
        d["entry_dt"] = self.entry_dt.isoformat()
        d["exit_dt"] = self.exit_dt.isoformat()
        return d


@dataclass(frozen=True)
class Performance:
    trades: int
    wins: int
    win_rate: float
    avg_return: float
    median_return: float
    total_return: float
    sharpe: float | None
    max_drawdown: float
    best: float
    worst: float
    by_severity: dict[str, dict[str, float]]

    def to_dict(self) -> dict:
        return asdict(self)


def simulate(
    signals: Sequence[Signal],
    *,
    hold_days: int = 5,
    cost_bps: float = 10.0,
    min_severity: float = 0.0,
    cache: PriceCache | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    first_green: bool = False,
) -> list[Trade]:
    """Replay signals through a configurable trading rule.

    Exit strategies (evaluated in order each day):
    1. ``stop_loss``   – sell if intraday return ≤ -stop_loss (e.g. 0.05 = -5%)
    2. ``take_profit`` – sell if intraday return ≥ +take_profit (e.g. 0.10 = +10%)
    3. ``first_green`` – sell at close of first day that closes above entry
    4. Fixed hold      – sell at close of trading day ``hold_days``

    Strategies compose: stop_loss + first_green means "sell on first green
    close, but bail at -X% if it never goes green."
    """
    cache = cache or PriceCache()
    trades: list[Trade] = []
    for s in signals:
        if s.severity < min_severity:
            continue
        start = s.event_dt.date() - timedelta(days=10)
        end = s.event_dt.date() + timedelta(days=hold_days * 2 + 20)
        try:
            hist = get_history(s.ticker, start, end, cache=cache)
        except Exception as exc:  # pragma: no cover
            log.warning("skip %s: price fetch failed (%s)", s.ticker, exc)
            continue
        if hist.empty:
            continue
        event_date = pd.Timestamp(s.event_dt.date())
        post = hist.loc[hist.index >= event_date]
        if len(post) <= hold_days:
            continue
        entry_price = float(post.iloc[0]["Open"])
        if entry_price <= 0:
            continue

        exit_idx, exit_reason = _find_exit(
            post, entry_price, hold_days,
            stop_loss=stop_loss, take_profit=take_profit, first_green=first_green,
        )
        exit_price = float(post.iloc[exit_idx]["Close"])
        gross = exit_price / entry_price - 1.0
        net = gross - (cost_bps / 10_000.0)
        trades.append(
            Trade(
                ticker=s.ticker,
                detector=s.detector,
                event_dt=s.event_dt,
                entry_dt=post.index[0].date(),
                entry_price=entry_price,
                exit_dt=post.index[exit_idx].date(),
                exit_price=exit_price,
                hold_days=exit_idx,
                severity=s.severity,
                gross_return=gross,
                net_return=net,
                headline=s.headline,
                exit_reason=exit_reason,
            )
        )
    return trades


def _find_exit(
    post: pd.DataFrame,
    entry_price: float,
    hold_days: int,
    *,
    stop_loss: float | None,
    take_profit: float | None,
    first_green: bool,
) -> tuple[int, str]:
    """Return (index into post, exit_reason)."""
    max_idx = min(hold_days, len(post) - 1)
    for i in range(1, max_idx + 1):
        close = float(post.iloc[i]["Close"])
        low = float(post.iloc[i]["Low"])
        high = float(post.iloc[i]["High"])
        ret_close = close / entry_price - 1.0
        ret_low = low / entry_price - 1.0
        ret_high = high / entry_price - 1.0

        if stop_loss is not None and ret_low <= -stop_loss:
            return i, "stop_loss"
        if take_profit is not None and ret_high >= take_profit:
            return i, "take_profit"
        if first_green and ret_close > 0:
            return i, "first_green"
    return max_idx, "hold"


def _severity_bucket(sev: float) -> str:
    if sev < 0.6:
        return "0.00-0.60"
    if sev < 0.75:
        return "0.60-0.75"
    if sev < 0.90:
        return "0.75-0.90"
    return "0.90-1.00"


def summarize(trades: Sequence[Trade]) -> Performance:
    if not trades:
        return Performance(
            trades=0, wins=0, win_rate=0.0, avg_return=0.0, median_return=0.0,
            total_return=0.0, sharpe=None, max_drawdown=0.0, best=0.0, worst=0.0,
            by_severity={},
        )
    returns = pd.Series([t.net_return for t in trades])
    wins = int((returns > 0).sum())
    win_rate = wins / len(returns)
    avg = float(returns.mean())
    med = float(returns.median())
    total = float((1.0 + returns).prod() - 1.0)
    std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    # Rough Sharpe: assume each trade is one period; annualize by trades/year
    # heuristic only makes sense with many trades, so guard it.
    sharpe: float | None = None
    if std > 0 and len(returns) >= 5:
        sharpe = float(avg / std * math.sqrt(252 / max(1, trades[0].hold_days)))
    equity = (1.0 + returns).cumprod()
    peak = equity.cummax()
    drawdown = float(((equity / peak) - 1.0).min())
    best = float(returns.max())
    worst = float(returns.min())

    by_sev: dict[str, dict[str, float]] = {}
    buckets: dict[str, list[float]] = {}
    for t in trades:
        buckets.setdefault(_severity_bucket(t.severity), []).append(t.net_return)
    for label, vals in buckets.items():
        s = pd.Series(vals)
        by_sev[label] = {
            "trades": float(len(vals)),
            "win_rate": float((s > 0).mean()),
            "avg_return": float(s.mean()),
        }
    return Performance(
        trades=len(trades),
        wins=wins,
        win_rate=win_rate,
        avg_return=avg,
        median_return=med,
        total_return=total,
        sharpe=sharpe,
        max_drawdown=drawdown,
        best=best,
        worst=worst,
        by_severity=by_sev,
    )


def write_trades_json(trades: Iterable[Trade], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([t.to_dict() for t in trades], indent=2), encoding="utf-8")


def write_trades_csv(trades: Iterable[Trade], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    trades = list(trades)
    fields = [
        "event_dt",
        "ticker",
        "detector",
        "entry_dt",
        "entry_price",
        "exit_dt",
        "exit_price",
        "hold_days",
        "exit_reason",
        "severity",
        "gross_return",
        "net_return",
        "headline",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for t in trades:
            writer.writerow(t.to_dict())
