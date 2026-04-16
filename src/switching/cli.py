from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from switching import backtest as backtest_mod
from switching import registry
from switching.pricing import PriceCache, get_reaction
from switching.reporter import rank, render_table, write_csv, write_json
from switching.sources import historical

app = typer.Typer(
    add_completion=False,
    help=(
        "Switching — scan public data for corporate-narrative pivots (AI-pivot, "
        "crypto-treasury, activist 13D, ...) and measure the stock reaction.\n\n"
        "Research tool only. Not investment advice."
    ),
)
console = Console()


def _parse_since(value: str) -> datetime:
    # Accept "7d", "30d", "2024-01-01", full ISO timestamps.
    value = value.strip()
    if value.endswith("d") and value[:-1].isdigit():
        return datetime.now(tz=timezone.utc) - timedelta(days=int(value[:-1]))
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"cannot parse --since: {value!r}") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_date(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@app.command("list-detectors")
def list_detectors() -> None:
    """List all registered detectors."""
    registry.load_builtin_detectors()
    table = Table(title="Registered detectors")
    table.add_column("Name", style="bold")
    table.add_column("Description")
    for name, cls in sorted(registry.all_detectors().items()):
        table.add_row(name, getattr(cls, "description", ""))
    console.print(table)


@app.command("scan")
def scan(
    since: str = typer.Option("7d", help="Window: e.g. 7d, 30d, or ISO date."),
    detector: list[str] = typer.Option(
        None, "--detector", "-d", help="Detector name; repeatable. Omit to run all."
    ),
    min_severity: float = typer.Option(0.0, help="Drop signals below this severity."),
    hold_days: int = typer.Option(5, help="Price-reaction hold window in trading days."),
    json_out: Optional[Path] = typer.Option(None, "--json", help="Write JSON output to this path."),
    csv_out: Optional[Path] = typer.Option(None, "--csv", help="Write CSV output to this path."),
    log_level: str = typer.Option("WARNING", help="Python log level."),
) -> None:
    """Run detectors against live sources and report a ranked signal list."""
    logging.basicConfig(level=log_level.upper())
    registry.load_builtin_detectors()
    names = detector or sorted(registry.all_detectors())
    since_dt = _parse_since(since)
    cache = PriceCache()

    collected = []
    seen: set[tuple[str, str, str]] = set()
    for name in names:
        cls = registry.get(name)
        det = cls()
        for sig in det.scan(since_dt):
            key = sig.dedup_key()
            if key in seen:
                continue
            seen.add(key)
            if sig.severity < min_severity:
                continue
            reaction = get_reaction(sig.ticker, sig.event_dt, hold_days=hold_days, cache=cache)
            collected.append(sig.with_reaction(reaction))

    collected = rank(collected)
    render_table(collected, console=console)
    if json_out:
        write_json(collected, json_out)
        console.print(f"[dim]wrote {len(collected)} rows to {json_out}[/dim]")
    if csv_out:
        write_csv(collected, csv_out)
        console.print(f"[dim]wrote {len(collected)} rows to {csv_out}[/dim]")


@app.command("backtest")
def backtest_cmd(
    detector: str = typer.Option(..., "--detector", "-d", help="Detector name to backtest."),
    frm: str = typer.Option(..., "--from", help="Start date (ISO)."),
    to: str = typer.Option(..., "--to", help="End date (ISO)."),
    hold_days: int = typer.Option(5, "--hold-days", help="Hold window in trading days."),
    min_severity: float = typer.Option(0.0, help="Drop events below this severity."),
    cost_bps: float = typer.Option(10.0, help="Round-trip transaction cost in basis points."),
    json_out: Optional[Path] = typer.Option(None, "--json", help="Write per-trade JSON."),
    csv_out: Optional[Path] = typer.Option(None, "--csv", help="Write per-trade CSV."),
    log_level: str = typer.Option("WARNING", help="Python log level."),
) -> None:
    """Replay historical events through a next-day-open / N-day-close rule."""
    logging.basicConfig(level=log_level.upper())
    registry.load_builtin_detectors()
    # Ensure detector exists so unknown names fail fast.
    registry.get(detector)

    start = _parse_date(frm)
    end = _parse_date(to)
    events = [
        e for e in historical.load(detector)
        if start <= e.event_dt <= end and e.severity >= min_severity
    ]
    if not events:
        console.print(f"[yellow]no seed events found for {detector} in range[/yellow]")
        raise typer.Exit(code=0)

    cache = PriceCache()
    trades = backtest_mod.simulate(
        events, hold_days=hold_days, cost_bps=cost_bps, min_severity=min_severity, cache=cache
    )
    perf = backtest_mod.summarize(trades)
    _render_performance(perf, detector=detector, hold_days=hold_days, events=len(events), trades_run=len(trades))

    if json_out:
        backtest_mod.write_trades_json(trades, json_out)
        console.print(f"[dim]wrote {len(trades)} trades to {json_out}[/dim]")
    if csv_out:
        backtest_mod.write_trades_csv(trades, csv_out)
        console.print(f"[dim]wrote {len(trades)} trades to {csv_out}[/dim]")


def _render_performance(perf, *, detector: str, hold_days: int, events: int, trades_run: int) -> None:
    header = Table(title=f"Backtest — {detector} (hold={hold_days}d, events={events}, trades={trades_run})")
    header.add_column("Metric")
    header.add_column("Value", justify="right")
    header.add_row("Trades", str(perf.trades))
    header.add_row("Wins", str(perf.wins))
    header.add_row("Win rate", f"{perf.win_rate * 100:.1f}%")
    header.add_row("Avg return / trade", f"{perf.avg_return * 100:+.2f}%")
    header.add_row("Median return", f"{perf.median_return * 100:+.2f}%")
    header.add_row("Total return (compounded)", f"{perf.total_return * 100:+.2f}%")
    header.add_row("Sharpe (approx)", f"{perf.sharpe:.2f}" if perf.sharpe is not None else "-")
    header.add_row("Max drawdown", f"{perf.max_drawdown * 100:.2f}%")
    header.add_row("Best / worst trade", f"{perf.best * 100:+.2f}% / {perf.worst * 100:+.2f}%")
    console.print(header)

    if perf.by_severity:
        sev_tbl = Table(title="By severity bucket")
        sev_tbl.add_column("Severity")
        sev_tbl.add_column("Trades", justify="right")
        sev_tbl.add_column("Win rate", justify="right")
        sev_tbl.add_column("Avg return", justify="right")
        for label in sorted(perf.by_severity):
            row = perf.by_severity[label]
            sev_tbl.add_row(
                label,
                f"{int(row['trades'])}",
                f"{row['win_rate'] * 100:.1f}%",
                f"{row['avg_return'] * 100:+.2f}%",
            )
        console.print(sev_tbl)


if __name__ == "__main__":  # pragma: no cover
    app()
