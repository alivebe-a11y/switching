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
    "spinoff", "stock_split", "crypto_treasury",
]

_UK_DEFAULT_DETECTORS = [
    "earnings_surprise", "analyst_upgrade", "mna_target",
    "guidance_raise", "dividend_surprise", "buyback",
    "index_inclusion", "spinoff", "contract_win",
    "stock_split", "crypto_treasury",
    "uk_director_dealing",
    # 2026-05-27: enabled on UK to collect data — both are RSS/regex detectors
    # (no US-only data source), so they CAN fire on UK headlines.
    # ai_pivot: UK has plenty of AI-pivot stories (Darktrace, ARM, smaller AIM
    #   tech). Live data will tell us whether the edge holds at LSE liquidity.
    # fda_decision: UK-listed biotechs (Hutchmed, Vectura, GSK) routinely
    #   announce FDA decisions that move their UK price — real signal.
    # NOTE: activist_13d + insider_cluster intentionally NOT enabled — they
    # pull from SEC EDGAR with no UK data source, so flipping the flag is a
    # no-op. UK equivalents are roadmap items: uk_activist_holdings (RNS
    # TR-1) and uk_insider_cluster (PDMR clusters on top of uk_director_dealing).
    "ai_pivot", "fda_decision",
]


@app.command("paper-trade")
def paper_trade(
    seed_cash: float = typer.Option(1000.0, "--seed", help="Starting cash."),
    detectors: list[str] = typer.Option(
        None, "--detector", "-d",
        help="Detector(s) to trade. Omit for the recommended US set (_DEFAULT_DETECTORS).",
    ),
    stop_loss: float = typer.Option(0.05, "--stop-loss", help="Stop-loss fraction (e.g. 0.05 = 5%)."),
    hold_days: int = typer.Option(5, "--hold-days", help="Max hold window in trading days."),
    interval: int = typer.Option(30, "--interval", help="Scan interval in minutes."),
    min_severity: float = typer.Option(0.0, help="Minimum signal severity to trade."),
    max_position_pct: float = typer.Option(0.20, "--max-position", help="Max % of portfolio per trade."),
    max_positions: int = typer.Option(5, "--max-positions", help="Max concurrent positions (0 = unlimited)."),
    state_file: Path = typer.Option(
        None, "--state",
        help="Path to portfolio state file. Defaults to /app/.cache/paper_portfolio.json (US) or /app/.cache/uk_portfolio.json (UK).",
    ),
    once: bool = typer.Option(False, "--once", help="Run one scan cycle and exit."),
    log_level: str = typer.Option("WARNING", help="Python log level."),
    market: str = typer.Option("us", "--market", help="Market to trade: 'us' (NYSE/NASDAQ) or 'uk' (LSE)."),
) -> None:
    """Run a paper-trading simulation against live RSS signals."""
    logging.basicConfig(level=log_level.upper())
    from switching.paper_trader import run_loop

    if market == "uk":
        default_detectors = _UK_DEFAULT_DETECTORS
        default_state = Path("/app/.cache/uk_portfolio.json")
    else:
        default_detectors = _DEFAULT_DETECTORS
        default_state = Path("/app/.cache/paper_portfolio.json")

    resolved_state = state_file if state_file is not None else default_state

    run_loop(
        state_path=resolved_state,
        seed_cash=seed_cash,
        detectors=detectors or default_detectors,
        stop_loss=stop_loss,
        hold_days=hold_days,
        scan_interval_minutes=interval,
        min_severity=min_severity,
        max_position_pct=max_position_pct,
        max_positions=max_positions,
        once=once,
        market=market,
    )


@app.command("paper-trade-uk")
def paper_trade_uk(
    seed_cash: float = typer.Option(20000.0, "--seed", help="Starting cash (GBP)."),
    detectors: list[str] = typer.Option(
        None, "--detector", "-d",
        help="Detector(s) to trade. Omit for UK recommended set.",
    ),
    stop_loss: float = typer.Option(0.026, "--stop-loss", help="Stop-loss fraction (e.g. 0.026 = 2.6%)."),
    hold_days: int = typer.Option(5, "--hold-days", help="Max hold window in trading days."),
    interval: int = typer.Option(10, "--interval", help="Scan interval in minutes."),
    min_severity: float = typer.Option(0.0, help="Minimum signal severity to trade."),
    max_position_pct: float = typer.Option(0.01, "--max-position", help="Max % of portfolio per trade."),
    max_positions: int = typer.Option(0, "--max-positions", help="Max concurrent positions (0 = unlimited)."),
    state_file: Path = typer.Option(
        "/app/.cache/uk_portfolio.json", "--state",
        help="Path to portfolio state file.",
    ),
    once: bool = typer.Option(False, "--once", help="Run one scan cycle and exit."),
    log_level: str = typer.Option("WARNING", help="Python log level."),
) -> None:
    """Run a paper-trading simulation against LSE signals (UK market shorthand)."""
    logging.basicConfig(level=log_level.upper())
    from switching.paper_trader import run_loop
    run_loop(
        state_path=state_file,
        seed_cash=seed_cash,
        detectors=detectors or _UK_DEFAULT_DETECTORS,
        stop_loss=stop_loss,
        hold_days=hold_days,
        scan_interval_minutes=interval,
        min_severity=min_severity,
        max_position_pct=max_position_pct,
        max_positions=max_positions,
        once=once,
        market="uk",
    )


@app.command("trade-t212")
def trade_t212_cmd(
    detectors: list[str] = typer.Option(
        None, "--detector", "-d",
        help="Detector(s) to trade. Omit for recommended set.",
    ),
    stop_loss: float = typer.Option(0.026, "--stop-loss", help="Base stop-loss fraction (e.g. 0.026 = 2.6%)."),
    hold_days: int = typer.Option(5, "--hold-days", help="Default max hold window in trading days."),
    interval: int = typer.Option(10, "--interval", help="Scan interval in minutes."),
    min_severity: float = typer.Option(0.0, help="Minimum signal severity to trade."),
    max_position_pct: float = typer.Option(0.01, "--max-position", help="Max % of portfolio per trade."),
    max_positions: int = typer.Option(0, "--max-positions", help="Max concurrent positions (0 = unlimited)."),
    state_file: Path = typer.Option(
        None, "--state",
        help=("Path to T212 trade state file (separate from internal paper trader). "
              "Defaults to t212_portfolio.json (US) or t212_uk_portfolio.json (UK) "
              "based on --market."),
    ),
    market: str = typer.Option(
        "us", "--market",
        help="T212 market: 'us' (NYSE/NASDAQ) or 'uk' (LSE). "
             "Picks the broker ticker convention, market-hours gate, and "
             "detector set. The two markets share ONE T212 account but are "
             "isolated by ticker suffix at the client layer.",
    ),
    once: bool = typer.Option(False, "--once", help="Run one scan cycle and exit."),
    log_level: str = typer.Option("WARNING", help="Python log level."),
) -> None:
    """Trade via Trading 212 (demo or live). Requires T212_API_KEY env var.

    Runs the same detector-specific exit profiles as the internal paper trader
    so you can compare execution quality side-by-side.

    Set T212_DEMO=true (default) for demo account, T212_DEMO=false for real money.

    State is saved to a separate JSON file — both this service and the internal
    paper-trade service can run simultaneously for comparison.

    Two parallel T212 services can also run side-by-side (one --market us,
    one --market uk) — they share the same T212 account but are isolated
    by the broker's position-filter bulkhead and use distinct state files
    (t212_portfolio.json vs t212_uk_portfolio.json).
    """
    logging.basicConfig(level=log_level.upper())

    market_norm = (market or "us").lower()
    if market_norm not in ("us", "uk"):
        typer.echo(f"Error: --market must be 'us' or 'uk', got {market!r}", err=True)
        raise typer.Exit(code=2)

    # Pick a sensible default state file + detector set per market when
    # the caller didn't override them on the command line.
    if state_file is None:
        default_state = (
            "/app/.cache/t212_portfolio.json"
            if market_norm == "us"
            else "/app/.cache/t212_uk_portfolio.json"
        )
        state_file = Path(default_state)
    default_detectors = (
        _DEFAULT_DETECTORS if market_norm == "us" else _UK_DEFAULT_DETECTORS
    )

    from switching.paper_trader import run_loop_t212
    run_loop_t212(
        state_path=state_file,
        detectors=detectors or default_detectors,
        stop_loss=stop_loss,
        hold_days=hold_days,
        scan_interval_minutes=interval,
        min_severity=min_severity,
        max_position_pct=max_position_pct,
        max_positions=max_positions,
        once=once,
        market=market_norm,
    )


@app.command("check-t212")
def check_t212_cmd() -> None:
    """Diagnostic: verify Trading 212 API connectivity and show account snapshot.

    Read-only — no orders are placed. Requires T212_API_KEY env var.
    Set T212_DEMO=true (default) to test against the demo environment.
    """
    from switching.broker_trading212 import Trading212Client, T212AuthError
    from rich.table import Table

    try:
        client = Trading212Client()
    except T212AuthError as exc:
        console.print(f"[red]✗ Auth error: {exc}[/red]")
        raise SystemExit(1)

    env = "[yellow]DEMO[/yellow]" if client.demo else "[red]LIVE[/red]"
    console.print(f"\n[bold]Trading 212 connection check ({env})[/bold]")

    # Account summary
    acct_ok = False
    try:
        acct = client.get_account()
        acct_ok = True
        console.print(f"  [green]✓ Account data[/green]")
        console.print(f"    Free cash : ${acct.free:,.2f}")
        console.print(f"    Invested  : ${acct.invested:,.2f}")
        console.print(f"    Total     : ${acct.total:,.2f}")
        console.print(f"    P&L       : ${acct.ppl:+,.2f}")
    except Exception as exc:
        console.print(f"  [red]✗ Account data failed: {exc}[/red]")

    # Positions
    positions_ok = False
    try:
        positions = client.get_positions()
        positions_ok = True
        console.print(f"  [green]✓ Portfolio ({len(positions)} open position(s))[/green]")
        if positions:
            t = Table(show_header=True, header_style="bold")
            t.add_column("Ticker")
            t.add_column("Qty", justify="right")
            t.add_column("Avg Price", justify="right")
            t.add_column("Current", justify="right")
            t.add_column("P&L", justify="right")
            for p in positions:
                color = "green" if p.unrealized_pnl_pct >= 0 else "red"
                t.add_row(
                    p.symbol,
                    f"{p.quantity:.4f}",
                    f"${p.avg_entry_price:.2f}",
                    f"${p.current_price:.2f}",
                    f"[{color}]{p.unrealized_pnl_pct*100:+.1f}%[/{color}]",
                )
            console.print(t)
    except Exception as exc:
        console.print(f"  [red]✗ Portfolio failed: {exc}[/red]")

    # Market hours
    open_str = "[green]OPEN[/green]" if client.is_market_open() else "[yellow]CLOSED[/yellow]"
    console.print(f"  [green]✓ Market hours[/green]: {open_str}")

    if acct_ok and positions_ok:
        console.print("\n[bold green]✓ Connection OK[/bold green] — ready to start trade-t212 service.\n")
    else:
        console.print("\n[bold red]✗ Connection failed[/bold red] — fix the errors above before starting trade-t212.\n")
        raise SystemExit(1)


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


@app.command("options-compare")
def options_compare(
    state_file: Path = typer.Option(
        "/app/.cache/paper_portfolio.json", "--state",
        help="Path to portfolio state file.",
    ),
    iv: float = typer.Option(
        0.30, "--iv",
        help="Assumed implied volatility (e.g. 0.30 = 30%%). Large-caps ~0.25-0.35, "
             "biotech/small-caps ~0.50-0.80.",
    ),
    dte: int = typer.Option(
        14, "--dte",
        help="Days to expiry for the modelled ATM call at entry (7=weekly, 14=2-week, 30=monthly).",
    ),
    log_level: str = typer.Option("WARNING", help="Python log level."),
) -> None:
    """Model what P&L would have been if we bought ATM calls instead of stock.

    Uses Black-Scholes with assumed IV and DTE. Allocates the same dollar amount
    to option premium as was deployed into each stock trade. Results are
    hypothetical and for strategy exploration only — not investment advice.

    Useful questions to answer:
      • Which detectors would have benefited most from leverage?
      • At what IV does the options strategy break even vs stock?
      • Do short DTE (weekly) or longer DTE (monthly) suit our hold periods?
    """
    logging.basicConfig(level=log_level.upper())
    from switching.options_model import compare_options_vs_stock
    from switching.paper_trader import Portfolio

    p = Portfolio.load(state_file)
    if not p.trades:
        console.print("[yellow]No closed trades found. Run some paper trades first.[/yellow]")
        raise typer.Exit(code=0)

    result = compare_options_vs_stock(p.trades, assumed_iv=iv, dte=dte)

    console.print(
        f"\n[bold]Options Lab[/bold] — ATM calls, IV={iv*100:.0f}%, DTE={dte}d\n"
        f"[dim]Black-Scholes, European, same $ committed to premium as to stock.[/dim]"
    )

    summary = Table(title="Portfolio-Level Comparison")
    summary.add_column("Metric")
    summary.add_column("Stock", justify="right")
    summary.add_column("Options", justify="right")

    sc = "green" if result.total_stock_pnl >= 0 else "red"
    oc = "green" if result.total_options_pnl >= 0 else "red"
    delta = result.total_options_pnl - result.total_stock_pnl
    dc = "green" if delta >= 0 else "red"

    summary.add_row("Total P&L",
        f"[{sc}]${result.total_stock_pnl:+.2f}[/{sc}]",
        f"[{oc}]${result.total_options_pnl:+.2f}[/{oc}]",
    )
    summary.add_row("Δ vs stock",
        "—",
        f"[{dc}]{'+' if delta >= 0 else ''}${delta:.2f}[/{dc}]",
    )
    summary.add_row("Win Rate",
        f"{result.stock_win_rate*100:.0f}%",
        f"{result.options_win_rate*100:.0f}%",
    )
    summary.add_row("Trades analysed", str(len(result.trades)), str(len(result.trades)))
    summary.add_row("Options beat stock on", "—",
        f"{result.options_better_count} / {len(result.trades)} trades",
    )
    console.print(summary)

    by_det = result.by_detector()
    if by_det:
        det_table = Table(title="Per-Detector (sorted by Δ options vs stock)")
        det_table.add_column("Detector")
        det_table.add_column("Trades", justify="right")
        det_table.add_column("Stock P&L", justify="right")
        det_table.add_column("Options P&L", justify="right")
        det_table.add_column("Δ P&L", justify="right")
        det_table.add_column("Stock WR", justify="right")
        det_table.add_column("Options WR", justify="right")
        for det, v in sorted(
            by_det.items(),
            key=lambda x: x[1]["options_pnl"] - x[1]["stock_pnl"],
            reverse=True,
        ):
            d_val = v["options_pnl"] - v["stock_pnl"]
            dc2 = "green" if d_val >= 0 else "red"
            sc2 = "green" if v["stock_pnl"] >= 0 else "red"
            oc2 = "green" if v["options_pnl"] >= 0 else "red"
            det_table.add_row(
                det,
                str(v["trades"]),
                f"[{sc2}]${v['stock_pnl']:+.2f}[/{sc2}]",
                f"[{oc2}]${v['options_pnl']:+.2f}[/{oc2}]",
                f"[{dc2}]{'+' if d_val >= 0 else ''}${d_val:.2f}[/{dc2}]",
                f"{v['stock_win_rate']*100:.0f}%",
                f"{v['options_win_rate']*100:.0f}%",
            )
        console.print(det_table)

    console.print(
        "\n[dim]Tip: try --iv 0.40 for small-caps, --dte 7 for weekly options, "
        "--dte 30 for monthlies. Higher IV = more expensive premium, "
        "so options need a bigger move to beat stock.[/dim]"
    )


@app.command("movers-audit")
def movers_audit_cmd(
    market: str = typer.Option("us", "--market", help="Market: us | uk."),
    state_file: Path = typer.Option(
        None, "--state",
        help="Portfolio state file (defaults per market).",
    ),
    limit: int = typer.Option(25, "--limit", help="Top N movers to audit."),
    news: str = typer.Option("auto", "--news", help="News source: auto | yfinance | benzinga."),
    log_level: str = typer.Option("WARNING", help="Python log level."),
) -> None:
    """Audit today's top movers: for each, did our detectors catch it — and if not, WHY?

    Buckets each missed mover as ticker_drop / feed_gap / no_detector / no_news, so we
    can see recall holes (news we could classify but never received) and uncovered
    catalyst types (new-detector candidates). Log-only research; writes a report the
    dashboard "Movers" tab renders. Run daily (cron) or ad-hoc.

    A/B the news source: run `--news yfinance` then `--news benzinga` on the same day
    and compare the no_detector/feed_gap counts — that shows whether Benzinga surfaces
    catalysts (on the movers) that Yahoo's commentary headlines miss.
    """
    logging.basicConfig(level=log_level.upper())
    from switching.movers import run_audit
    if state_file is None:
        state_file = Path(
            "/app/.cache/uk_portfolio.json" if market == "uk"
            else "/app/.cache/paper_portfolio.json"
        )
    report = run_audit(state_file, market=market, limit=limit, news_source=news)
    s = report["summary"]
    console.print(
        f"\n[bold]Movers audit — {market.upper()}[/bold] "
        f"({report['count']} movers, news={report.get('news_source')})\n"
        f"  caught={s['caught']}  ticker_drop={s['ticker_drop']}  "
        f"feed_gap={s['feed_gap']}  no_detector={s['no_detector']}  no_news={s['no_news']}"
    )
    for m in report["movers"]:
        if m["status"] == "caught":
            continue
        tag = {"ticker_drop": "yellow", "feed_gap": "red",
               "no_detector": "magenta", "no_news": "dim"}.get(m["reason"], "white")
        det = f" [{m['detector']}]" if m.get("detector") else ""
        console.print(
            f"  [{tag}]{m['symbol']:<8} {m['pct_change']:+6.1f}%  "
            f"{m['reason']}{det}[/{tag}]  {m['evidence'][:80]}"
        )


@app.command("weekly-report")
def weekly_report_cmd(
    state_file: Path = typer.Option(
        "/app/.cache/paper_portfolio.json", "--state",
        help="Path to portfolio state file (used to locate the .cache directory).",
    ),
    log_level: str = typer.Option("WARNING", help="Python log level."),
) -> None:
    """Generate and send the weekly performance report via Telegram immediately.

    Reads paper_portfolio.json, t212_portfolio.json, uk_portfolio.json and
    skipped_signals.json from the same directory as --state. Useful for
    testing the report or triggering it manually outside of Saturday.
    """
    logging.basicConfig(level=log_level.upper())
    from switching.weekly_report import generate_and_send, generate_report
    state_dir = state_file.parent
    console.print(f"[bold]Generating weekly report from {state_dir}...[/bold]")

    messages, _ = generate_report(state_dir)
    for i, msg in enumerate(messages, 1):
        console.print(f"\n[dim]── Message {i}/{len(messages)} ──[/dim]")
        # Strip HTML tags for console display
        import re
        plain = re.sub(r"<[^>]+>", "", msg)
        console.print(plain)

    ok = generate_and_send(state_dir)
    if ok:
        console.print("\n[green]✓ Report sent via Telegram.[/green]")
    else:
        console.print("\n[yellow]⚠ Telegram send failed (check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).[/yellow]")
        console.print("[dim]Report printed above — copy-paste if needed.[/dim]")


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
