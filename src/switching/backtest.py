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
) -> list[Trade]:
    """Replay signals through a next-day-open / N-day-close trading rule.

    No look-ahead: entry is the open on the first trading day at or after the
    event; exit is the close ``hold_days`` trading sessions later.
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
        entry_row = post.iloc[0]
        exit_row = post.iloc[hold_days]
        entry_price = float(entry_row["Open"])
        exit_price = float(exit_row["Close"])
        if entry_price <= 0:
            continue
        gross = exit_price / entry_price - 1.0
        net = gross - (cost_bps / 10_000.0)
        trades.append(
            Trade(
                ticker=s.ticker,
                detector=s.detector,
                event_dt=s.event_dt,
                entry_dt=post.index[0].date(),
                entry_price=entry_price,
                exit_dt=post.index[hold_days].date(),
                exit_price=exit_price,
                hold_days=hold_days,
                severity=s.severity,
                gross_return=gross,
                net_return=net,
                headline=s.headline,
            )
        )
    return trades


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
