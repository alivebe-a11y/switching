from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pandas as pd

from switching.signal import PriceReaction

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (ticker, trade_date)
);
"""


def _default_cache_path() -> Path:
    override = os.environ.get("SWITCHING_CACHE")
    if override:
        return Path(override)
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "switching" / "prices.sqlite"


class PriceCache:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_cache_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as cx:
            cx.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        cx = sqlite3.connect(self.path)
        try:
            yield cx
            cx.commit()
        finally:
            cx.close()

    def put(self, ticker: str, frame: pd.DataFrame) -> None:
        if frame.empty:
            return
        rows = []
        for idx, row in frame.iterrows():
            d = idx.date() if hasattr(idx, "date") else idx
            rows.append(
                (
                    ticker.upper(),
                    d.isoformat(),
                    _f(row.get("Open")),
                    _f(row.get("High")),
                    _f(row.get("Low")),
                    _f(row.get("Close")),
                    _f(row.get("Volume")),
                )
            )
        with self._conn() as cx:
            cx.executemany(
                "INSERT OR REPLACE INTO prices VALUES (?, ?, ?, ?, ?, ?, ?)", rows
            )

    def get(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        with self._conn() as cx:
            cur = cx.execute(
                "SELECT trade_date, open, high, low, close, volume "
                "FROM prices WHERE ticker = ? AND trade_date BETWEEN ? AND ? "
                "ORDER BY trade_date",
                (ticker.upper(), start.isoformat(), end.isoformat()),
            )
            rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(
            rows, columns=["trade_date", "Open", "High", "Low", "Close", "Volume"]
        )
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        return df.set_index("trade_date")


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return float(v)


def _fetch_yf(ticker: str, start: date, end: date) -> pd.DataFrame:
    import yfinance as yf  # deferred import so tests can monkeypatch

    data = yf.Ticker(ticker).history(
        start=start.isoformat(), end=end.isoformat(), auto_adjust=False
    )
    if hasattr(data.index, "tz") and data.index.tz is not None:
        data.index = data.index.tz_localize(None)
    return data


def get_history(
    ticker: str,
    start: date,
    end: date,
    *,
    cache: PriceCache | None = None,
) -> pd.DataFrame:
    cache = cache or PriceCache()
    cached = cache.get(ticker, start, end)
    # We key on trading days; if the cached range spans the requested window, use it.
    if not cached.empty and cached.index.min().date() <= start and cached.index.max().date() >= end - timedelta(days=5):
        return cached
    fresh = _fetch_yf(ticker, start, end)
    if not fresh.empty:
        cache.put(ticker, fresh)
    return cache.get(ticker, start, end) if not fresh.empty else cached


def get_reaction(
    ticker: str,
    event_dt: datetime,
    *,
    hold_days: int = 5,
    baseline_days: int = 20,
    cache: PriceCache | None = None,
) -> PriceReaction | None:
    if event_dt.tzinfo is None:
        event_dt = event_dt.replace(tzinfo=timezone.utc)
    start = event_dt.date() - timedelta(days=baseline_days * 2 + 10)
    end = event_dt.date() + timedelta(days=hold_days * 2 + 10)
    try:
        hist = get_history(ticker, start, end, cache=cache)
    except Exception as exc:  # pragma: no cover
        log.warning("price fetch failed for %s: %s", ticker, exc)
        return None
    if hist.empty:
        return None
    return compute_reaction(hist, event_dt, hold_days=hold_days, baseline_days=baseline_days)


def compute_reaction(
    hist: pd.DataFrame,
    event_dt: datetime,
    *,
    hold_days: int = 5,
    baseline_days: int = 20,
) -> PriceReaction | None:
    if hist.empty:
        return None
    event_date = pd.Timestamp(event_dt.date())
    pre = hist.loc[hist.index < event_date]
    post = hist.loc[hist.index >= event_date]
    if pre.empty or post.empty:
        return None
    baseline_close = float(pre["Close"].iloc[-1])
    close_1d = float(post["Close"].iloc[0])
    pct_1d = (close_1d / baseline_close - 1.0) if baseline_close else None
    pct_5d: float | None = None
    if len(post) > hold_days:
        close_nd = float(post["Close"].iloc[hold_days])
        pct_5d = (close_nd / baseline_close - 1.0) if baseline_close else None
    volume_ratio: float | None = None
    pre_vol = pre["Volume"].tail(baseline_days)
    if len(pre_vol) > 0 and pre_vol.mean() > 0:
        volume_ratio = float(post["Volume"].iloc[0] / pre_vol.mean())
    return PriceReaction(
        baseline_close=baseline_close,
        pct_change_1d=pct_1d,
        pct_change_5d=pct_5d,
        volume_ratio=volume_ratio,
    )
