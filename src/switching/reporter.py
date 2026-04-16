from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable, Sequence

from rich.console import Console
from rich.table import Table

from switching.signal import Signal


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v * 100:+.2f}%"


def _rank_score(signal: Signal) -> float:
    reaction = signal.price_reaction
    if reaction is None or reaction.pct_change_1d is None:
        return signal.severity
    return signal.severity * abs(reaction.pct_change_1d)


def rank(signals: Iterable[Signal]) -> list[Signal]:
    return sorted(signals, key=_rank_score, reverse=True)


def render_table(signals: Sequence[Signal], *, console: Console | None = None) -> None:
    console = console or Console()
    table = Table(title="Switching signals", show_lines=False)
    table.add_column("Date")
    table.add_column("Ticker")
    table.add_column("Detector")
    table.add_column("Headline", overflow="fold")
    table.add_column("1d", justify="right")
    table.add_column("5d", justify="right")
    table.add_column("Severity", justify="right")
    for s in signals:
        r = s.price_reaction
        table.add_row(
            s.event_dt.date().isoformat(),
            s.ticker,
            s.detector,
            s.headline,
            _fmt_pct(r.pct_change_1d) if r else "-",
            _fmt_pct(r.pct_change_5d) if r else "-",
            f"{s.severity:.2f}",
        )
    console.print(table)


def write_json(signals: Iterable[Signal], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [s.to_dict() for s in signals]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_csv(signals: Iterable[Signal], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    signals = list(signals)
    fields = [
        "event_dt",
        "ticker",
        "company",
        "detector",
        "severity",
        "pct_change_1d",
        "pct_change_5d",
        "volume_ratio",
        "headline",
        "url",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for s in signals:
            r = s.price_reaction
            writer.writerow(
                {
                    "event_dt": s.event_dt.isoformat(),
                    "ticker": s.ticker,
                    "company": s.company,
                    "detector": s.detector,
                    "severity": s.severity,
                    "pct_change_1d": "" if (not r or r.pct_change_1d is None) else r.pct_change_1d,
                    "pct_change_5d": "" if (not r or r.pct_change_5d is None) else r.pct_change_5d,
                    "volume_ratio": "" if (not r or r.volume_ratio is None) else r.volume_ratio,
                    "headline": s.headline,
                    "url": s.url,
                }
            )
