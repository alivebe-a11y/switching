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

    edgar_client = None
    edgar_names = {"activist_13d", "insider_cluster"}
    if any(n in edgar_names for n in names):
        import os
        if os.environ.get("SWITCHING_EDGAR_UA"):
            from switching.sources.sec_edgar import EdgarClient
            edgar_client = EdgarClient()

    collected = []
    seen: set[tuple[str, str, str]] = set()
    for name in names:
        cls = registry.get(name)
        if name in edgar_names:
            det = cls(client=edgar_client)
        else:
            det = cls()
        count = 0
        for sig in det.scan(since_dt):
            count += 1
            key = sig.dedup_key()
            if key in seen:
                continue
            seen.add(key)
            if sig.severity < min_severity:
                continue
            reaction = get_reaction(sig.ticker, sig.event_dt, hold_days=hold_days, cache=cache)
            collected.append(sig.with_reaction(reaction))
        console.print(f"[dim]{name}: {count} signal(s)[/dim]")

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
    entry_delay: int = typer.Option(
        1, "--entry-delay",
        help="Trading days after event to enter. 1 = next-day open (realistic), 0 = same-day (optimistic).",
    ),
    min_severity: float = typer.Option(0.0, help="Drop events below this severity."),
    cost_bps: float = typer.Option(10.0, help="Round-trip transaction cost in basis points."),
    stop_loss: Optional[float] = typer.Option(
        None, "--stop-loss", help="Exit if loss exceeds this fraction (e.g. 0.05 = -5%)."
    ),
    take_profit: Optional[float] = typer.Option(
        None, "--take-profit", help="Exit if gain exceeds this fraction (e.g. 0.10 = +10%)."
    ),
    first_green: bool = typer.Option(
        False, "--first-green", help="Exit at close of first day that closes above entry."
    ),
    live_seed: bool = typer.Option(
        False,
        "--live-seed",
        help="Augment the curated seed with events pulled live from SEC EDGAR. "
        "Requires $SWITCHING_EDGAR_UA (a descriptive User-Agent).",
    ),
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

    live_client = None
    if live_seed:
        from switching.sources.sec_edgar import EdgarClient, EdgarAuthError
        try:
            live_client = EdgarClient()
        except EdgarAuthError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2)

    events = [
        e for e in historical.load(detector, live=live_client, since=start, until=end)
        if start <= e.event_dt <= end and e.severity >= min_severity
    ]
    if not events:
        console.print(f"[yellow]no seed events found for {detector} in range[/yellow]")
        raise typer.Exit(code=0)

    cache = PriceCache()
    trades = backtest_mod.simulate(
        events, hold_days=hold_days, cost_bps=cost_bps, min_severity=min_severity, cache=cache,
        stop_loss=stop_loss, take_profit=take_profit, first_green=first_green,
        entry_delay=entry_delay,
    )
    parts = []
    if entry_delay == 0:
        parts.append("same-day")
    else:
        parts.append(f"T+{entry_delay}")
    if stop_loss is not None:
        parts.append(f"SL={stop_loss*100:.0f}%")
    if take_profit is not None:
        parts.append(f"TP={take_profit*100:.0f}%")
    if first_green:
        parts.append("first-green")
    else:
        parts.append("hold")
    strategy = " + ".join(parts)
    perf = backtest_mod.summarize(trades)
    _render_performance(perf, detector=detector, hold_days=hold_days, events=len(events), trades_run=len(trades), strategy=strategy)

    if json_out:
        backtest_mod.write_trades_json(trades, json_out)
        console.print(f"[dim]wrote {len(trades)} trades to {json_out}[/dim]")
    if csv_out:
        backtest_mod.write_trades_csv(trades, csv_out)
        console.print(f"[dim]wrote {len(trades)} trades to {csv_out}[/dim]")


def _render_performance(perf, *, detector: str, hold_days: int, events: int, trades_run: int, strategy: str = "hold") -> None:
    header = Table(title=f"Backtest — {detector} (hold={hold_days}d, strategy={strategy}, events={events}, trades={trades_run})")
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


_DEFAULT_DETECTORS = [
    "earnings_surprise", "ai_pivot", "index_inclusion",
    "buyback", "insider_cluster", "activist_13d",
    "analyst_upgrade", "fda_decision",
    "mna_target", "guidance_raise", "dividend_surprise", "contract_win",
]


@app.command("paper-trade")
def paper_trade(
    seed_cash: float = typer.Option(1000.0, "--seed", help="Starting cash."),
    detectors: list[str] = typer.Option(
        None, "--detector", "-d",
        help="Detector(s) to trade. Omit for recommended set (excludes spinoff).",
    ),
    stop_loss: float = typer.Option(0.05, "--stop-loss", help="Stop-loss fraction (e.g. 0.05 = 5%)."),
    hold_days: int = typer.Option(5, "--hold-days", help="Max hold window in trading days."),
    interval: int = typer.Option(30, "--interval", help="Scan interval in minutes."),
    min_severity: float = typer.Option(0.0, help="Minimum signal severity to trade."),
    state_file: Path = typer.Option(
        "/app/.cache/paper_portfolio.json", "--state",
        help="Path to portfolio state file.",
    ),
    once: bool = typer.Option(False, "--once", help="Run one scan cycle and exit."),
    log_level: str = typer.Option("WARNING", help="Python log level."),
) -> None:
    """Run a paper-trading simulation against live RSS signals."""
    logging.basicConfig(level=log_level.upper())
    from switching.paper_trader import run_loop
    run_loop(
        state_path=state_file,
        seed_cash=seed_cash,
        detectors=detectors or _DEFAULT_DETECTORS,
        stop_loss=stop_loss,
        hold_days=hold_days,
        scan_interval_minutes=interval,
        min_severity=min_severity,
        once=once,
    )


@app.command("trade")
def trade_cmd(
    detectors: list[str] = typer.Option(
        None, "--detector", "-d",
        help="Detector(s) to trade. Omit for recommended set.",
    ),
    stop_loss: float = typer.Option(0.05, "--stop-loss", help="Stop-loss fraction (e.g. 0.05 = 5%)."),
    hold_days: int = typer.Option(5, "--hold-days", help="Max hold window in trading days."),
    interval: int = typer.Option(30, "--interval", help="Scan interval in minutes."),
    min_severity: float = typer.Option(0.0, help="Minimum signal severity to trade."),
    max_position_pct: float = typer.Option(0.20, "--max-position", help="Max % of portfolio per trade."),
    max_positions: int = typer.Option(5, "--max-positions", help="Max concurrent positions."),
    state_file: Path = typer.Option(
        "/app/.cache/alpaca_state.json", "--state",
        help="Path to trade state file.",
    ),
    once: bool = typer.Option(False, "--once", help="Run one scan cycle and exit."),
    log_level: str = typer.Option("WARNING", help="Python log level."),
) -> None:
    """Trade live via Alpaca. Requires ALPACA_API_KEY and ALPACA_SECRET_KEY.

    Set ALPACA_PAPER=true (default) for paper trading, ALPACA_PAPER=false for real money.
    """
    logging.basicConfig(level=log_level.upper())
    from switching.paper_trader import run_loop_alpaca
    run_loop_alpaca(
        state_path=state_file,
        detectors=detectors or _DEFAULT_DETECTORS,
        stop_loss=stop_loss,
        hold_days=hold_days,
        scan_interval_minutes=interval,
        min_severity=min_severity,
        max_position_pct=max_position_pct,
        max_positions=max_positions,
        once=once,
    )


@app.command("paper-status")
def paper_status(
    state_file: Path = typer.Option(
        "/app/.cache/paper_portfolio.json", "--state",
        help="Path to portfolio state file.",
    ),
) -> None:
    """Show current paper-trading portfolio status."""
    from switching.paper_trader import Portfolio
    p = Portfolio.load(state_file)
    table = Table(title="Paper Trading Portfolio")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Cash", f"${p.cash:.2f}")
    table.add_row("Open positions", str(len(p.positions)))
    table.add_row("Portfolio value", f"${p.total_value:.2f}")
    total_trades = len(p.trades)
    wins = sum(1 for t in p.trades if t.pnl > 0)
    table.add_row("Closed trades", str(total_trades))
    if total_trades:
        table.add_row("Win rate", f"{wins/total_trades*100:.0f}%")
        table.add_row("Total P&L", f"${sum(t.pnl for t in p.trades):+.2f}")
        table.add_row("Starting cash", f"${p.total_value - sum(t.pnl for t in p.trades):.2f}")
    console.print(table)

    if p.positions:
        pos_table = Table(title="Open Positions")
        pos_table.add_column("Ticker")
        pos_table.add_column("Detector")
        pos_table.add_column("Entry", justify="right")
        pos_table.add_column("Shares", justify="right")
        pos_table.add_column("Value", justify="right")
        pos_table.add_column("Day")
        for pos in p.positions:
            pos_table.add_row(
                pos.ticker, pos.detector,
                f"${pos.entry_price:.2f}", f"{pos.shares:.4f}",
                f"${pos.cost_basis:.2f}", f"{pos.days_held}/{pos.hold_days}",
            )
        console.print(pos_table)

    if p.trades:
        trade_table = Table(title="Trade History (last 10)")
        trade_table.add_column("Ticker")
        trade_table.add_column("Return", justify="right")
        trade_table.add_column("P&L", justify="right")
        trade_table.add_column("Exit")
        trade_table.add_column("Headline")
        for t in p.trades[-10:]:
            color = "green" if t.pnl >= 0 else "red"
            trade_table.add_row(
                t.ticker,
                f"[{color}]{t.pct_return*100:+.1f}%[/{color}]",
                f"[{color}]${t.pnl:+.2f}[/{color}]",
                t.exit_reason,
                t.headline[:50],
            )
        console.print(trade_table)


@app.command("check-feeds")
def check_feeds() -> None:
    """Diagnostic: test RSS feed connectivity and report item counts."""
    import feedparser
    from switching.sources import rss

    all_feeds = {
        "DEFAULT_FEEDS": rss.DEFAULT_FEEDS,
        "EARNINGS_FEEDS": rss.EARNINGS_FEEDS,
        "CORPORATE_FEEDS": rss.CORPORATE_FEEDS,
    }
    total_ok = 0
    total_fail = 0
    for group_name, urls in all_feeds.items():
        console.print(f"\n[bold]{group_name}[/bold]")
        for url in urls:
            short = url.split("/")[-1][:60] if "/" in url else url[:60]
            try:
                parsed = feedparser.parse(url)
                n = len(parsed.entries)
                if n > 0:
                    console.print(f"  [green]OK[/green] {short}: {n} items")
                    total_ok += 1
                else:
                    console.print(f"  [yellow]EMPTY[/yellow] {short}: 0 items")
                    total_fail += 1
            except Exception as exc:
                console.print(f"  [red]FAIL[/red] {short}: {exc}")
                total_fail += 1

    import os
    ua = os.environ.get("SWITCHING_EDGAR_UA")
    console.print(f"\n[bold]EDGAR[/bold]")
    if ua:
        console.print(f"  SWITCHING_EDGAR_UA = {ua!r}")
        try:
            from switching.sources.sec_edgar import EdgarClient
            client = EdgarClient()
            ticker = client.ticker_for_cik("320193")
            console.print(f"  [green]OK[/green] CIK 320193 → {ticker}")
        except Exception as exc:
            console.print(f"  [red]FAIL[/red] {exc}")
    else:
        console.print("  [yellow]SWITCHING_EDGAR_UA not set — EDGAR detectors disabled[/yellow]")

    console.print(f"\n[bold]Summary:[/bold] {total_ok} feeds OK, {total_fail} failed/empty")


@app.command("web")
def web_cmd(
    host: str = typer.Option("0.0.0.0", help="Bind address."),
    port: int = typer.Option(8080, help="Port to listen on."),
    state_file: Path = typer.Option(
        "/app/.cache/paper_portfolio.json", "--state",
        help="Path to portfolio state file.",
    ),
    log_level: str = typer.Option("WARNING", help="Python log level."),
) -> None:
    """Launch web dashboard to view paper-trading portfolio and signals."""
    logging.basicConfig(level=log_level.upper())
    from switching.web import create_app
    flask_app = create_app(state_path=state_file)
    console.print(f"[bold]Dashboard running at http://{host}:{port}[/bold]")
    flask_app.run(host=host, port=port, debug=False)


if __name__ == "__main__":  # pragma: no cover
    app()
