"""Weekly performance report.

Generated every Saturday and sent via Telegram. Covers:

  1. All-time portfolio summary (paper-trade)
  2. This week's trades (since last Saturday)
  3. Detector rankings — win rate, avg return, P&L, trend
  4. T212 vs paper-trade comparison and slippage analysis
  5. UK (LSE) service performance (if running)
  6. Exit-reason breakdown (first_green / stop_loss / hold_expiry)
  7. Signal-quality correlation (severity vs outcome)
  8. Skipped-signal opportunity cost (would-have-been P&L)
  9. Data-driven improvement suggestions

Call ``generate_and_send(state_dir)`` from the paper-trade loop on
Saturday mornings. The report is split into multiple Telegram messages
if it would exceed the 4 096-char API limit.

Every report is also saved to ``<state_dir>/weekly_reports/YYYY-MM-DD.json``
for permanent archiving and dashboard display. Use ``load_all_reports()``
to retrieve the full history in reverse-chronological order.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_WIN_RATE_TARGET = 0.55   # below this → detector needs attention
_WIN_RATE_STRONG = 0.65   # above this → consider increasing allocation
_MIN_TRADES_FOR_JUDGEMENT = 5  # need at least this many trades to draw conclusions
_STOP_LOSS_CONCERN_PCT = 0.40  # if >40% of trades hit stop-loss, something's wrong


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def _week_start() -> datetime:
    """Return the most recent Saturday 00:00 UTC (the start of the reporting week)."""
    now = datetime.now(tz=timezone.utc)
    # weekday(): Mon=0 … Sat=5, Sun=6
    days_since_sat = (now.weekday() - 5) % 7   # 0 on Saturday
    return (now - timedelta(days=days_since_sat)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _prev_week_start() -> datetime:
    return _week_start() - timedelta(days=7)


def _parse_dt(s: str) -> datetime:
    s = s.rstrip("Z")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return datetime.now(tz=timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _pct(n: int, total: int) -> str:
    return f"{n/total*100:.0f}%" if total else "–"


def _sign(v: float) -> str:
    return f"+{v:.2f}" if v >= 0 else f"{v:.2f}"


# ---------------------------------------------------------------------------
# Core analysis functions
# ---------------------------------------------------------------------------


def _analyse_trades(trades: list[dict]) -> dict[str, Any]:
    """Compute aggregate stats from a list of trade dicts."""
    if not trades:
        return {
            "count": 0, "wins": 0, "win_rate": 0.0,
            "total_pnl": 0.0, "avg_return": 0.0,
        }
    wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
    total_pnl = sum(t.get("pnl", 0.0) for t in trades)
    avg_return = sum(t.get("pct_return", 0.0) for t in trades) / len(trades)
    return {
        "count": len(trades),
        "wins": wins,
        "win_rate": wins / len(trades),
        "total_pnl": total_pnl,
        "avg_return": avg_return,
    }


def _detector_rankings(trades: list[dict]) -> list[dict]:
    """Per-detector stats, sorted by win rate descending."""
    by_det: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_det[t.get("detector", "unknown")].append(t)

    rows = []
    for det, det_trades in sorted(by_det.items()):
        stats = _analyse_trades(det_trades)
        rows.append({
            "detector": det,
            **stats,
        })
    return sorted(rows, key=lambda r: (r["win_rate"], r["total_pnl"]), reverse=True)


def _exit_reason_breakdown(trades: list[dict]) -> dict[str, dict]:
    by_reason: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_reason[t.get("exit_reason", "unknown")].append(t)
    return {reason: _analyse_trades(ts) for reason, ts in sorted(by_reason.items())}


def _severity_analysis(trades: list[dict]) -> dict[str, Any]:
    """Does higher severity actually predict better outcomes?"""
    high = [t for t in trades if t.get("severity", 0) >= 0.75]
    low  = [t for t in trades if t.get("severity", 0) < 0.75]
    return {
        "high_severity": _analyse_trades(high),
        "low_severity": _analyse_trades(low),
    }


def _t212_vs_paper(t212_trades: list[dict], paper_trades: list[dict]) -> list[dict]:
    """Match trades on ticker + entry date for slippage comparison."""
    paper_by_key: dict[str, dict] = {}
    for t in paper_trades:
        key = f"{t.get('ticker', '')}:{t.get('entry_dt', '')[:10]}"
        paper_by_key[key] = t

    matched = []
    for t in t212_trades:
        key = f"{t.get('ticker', '')}:{t.get('entry_dt', '')[:10]}"
        paper = paper_by_key.get(key)
        if paper:
            slippage = t.get("pct_return", 0.0) - paper.get("pct_return", 0.0)
            matched.append({
                "ticker": t.get("ticker"),
                "entry_dt": t.get("entry_dt", "")[:10],
                "t212_return": t.get("pct_return", 0.0),
                "paper_return": paper.get("pct_return", 0.0),
                "slippage": slippage,
            })
    return matched


def _skipped_opportunity(skipped: list[dict]) -> dict[str, Any]:
    """Summarise the P&L we left on the table from skipped signals."""
    completed = [s for s in skipped if s.get("tracking_complete") and s.get("simulated_pct_return") is not None]
    if not completed:
        return {"count": 0, "would_have_won": 0, "would_have_pnl": 0.0}
    would_have_won = sum(1 for s in completed if (s.get("simulated_pct_return") or 0) > 0)
    # Estimate P&L using a fixed allocation size (£200 = 1% of £20k seed)
    approx_alloc = 200.0
    would_have_pnl = sum(
        (s.get("simulated_pct_return") or 0) * approx_alloc
        for s in completed
    )
    return {
        "count": len(completed),
        "would_have_won": would_have_won,
        "would_have_win_rate": would_have_won / len(completed) if completed else 0,
        "would_have_pnl": would_have_pnl,
    }


# ---------------------------------------------------------------------------
# Suggestion engine
# ---------------------------------------------------------------------------


def _generate_suggestions(
    *,
    overall: dict,
    detector_rows: list[dict],
    exit_breakdown: dict[str, dict],
    severity_data: dict,
    t212_matched: list[dict],
    t212_stats: dict,
    paper_stats: dict,
    skipped_opps: dict,
) -> list[str]:
    suggestions: list[str] = []

    # 1. Detectors with poor win rate
    for row in detector_rows:
        if row["count"] >= _MIN_TRADES_FOR_JUDGEMENT and row["win_rate"] < _WIN_RATE_TARGET:
            wr_pct = row["win_rate"] * 100
            suggestions.append(
                f"🔻 <b>{row['detector']}</b>: {wr_pct:.0f}% WR over {row['count']} trades "
                f"(below {_WIN_RATE_TARGET*100:.0f}% target) — consider raising severity threshold or disabling"
            )

    # 2. Detectors with strong win rate → increase allocation
    for row in detector_rows:
        if row["count"] >= _MIN_TRADES_FOR_JUDGEMENT and row["win_rate"] >= _WIN_RATE_STRONG:
            wr_pct = row["win_rate"] * 100
            suggestions.append(
                f"⭐ <b>{row['detector']}</b>: {wr_pct:.0f}% WR — strong performer, "
                f"consider bumping allocation"
            )

    # 3. Too many stop-losses
    sl_data = exit_breakdown.get("stop_loss", {})
    total = overall.get("count", 0)
    if total > 0 and sl_data.get("count", 0) / total >= _STOP_LOSS_CONCERN_PCT:
        pct = sl_data["count"] / total * 100
        suggestions.append(
            f"⚠️ Stop-loss exits are {pct:.0f}% of all trades — signals may be entering too early. "
            f"Consider raising min_severity or widening stops by 0.5%"
        )

    # 4. Severity not predicting outcome
    high = severity_data.get("high_severity", {})
    low = severity_data.get("low_severity", {})
    h_count = high.get("count", 0)
    l_count = low.get("count", 0)
    if h_count >= 5 and l_count >= 5:
        h_wr = high.get("win_rate", 0)
        l_wr = low.get("win_rate", 0)
        if abs(h_wr - l_wr) < 0.05:
            suggestions.append(
                f"📊 High-severity (≥0.75) and low-severity signals are winning at similar rates "
                f"({h_wr*100:.0f}% vs {l_wr*100:.0f}%) — "
                f"the severity score may not be a reliable filter yet"
            )
        elif h_wr > l_wr + 0.10:
            suggestions.append(
                f"📊 High-severity signals win at {h_wr*100:.0f}% vs {l_wr*100:.0f}% for low — "
                f"consider raising min_severity to 0.65+"
            )

    # 5. T212 vs paper-trade slippage
    if t212_matched:
        avg_slip = sum(m["slippage"] for m in t212_matched) / len(t212_matched)
        if avg_slip < -0.005:  # T212 is 0.5%+ worse
            suggestions.append(
                f"📉 T212 is averaging {avg_slip*100:+.2f}% slippage vs yfinance fills "
                f"over {len(t212_matched)} matched trades — "
                f"real fills are worse than theoretical (expected, but worth monitoring)"
            )
        elif avg_slip > 0.005:  # T212 is actually beating yfinance
            suggestions.append(
                f"📈 T212 is averaging {avg_slip*100:+.2f}% better than yfinance fills "
                f"over {len(t212_matched)} matched trades — "
                f"intraday execution is outperforming the theoretical model"
            )

    # 6. T212 vs paper-trade overall P&L difference
    t_pnl = t212_stats.get("total_pnl", 0.0)
    p_pnl = paper_stats.get("total_pnl", 0.0)
    if t212_stats.get("count", 0) >= 5 and paper_stats.get("count", 0) >= 5:
        diff = t_pnl - p_pnl
        if abs(diff) > 50:
            leader = "T212" if diff > 0 else "Paper"
            suggestions.append(
                f"💱 {leader} is ahead by ${abs(diff):.2f} total P&amp;L — "
                f"{'real fills are better' if diff > 0 else 'theoretical yfinance fills are better, check for adverse T212 execution'}"
            )

    # 7. Skipped signals opportunity cost
    skipped_pnl = skipped_opps.get("would_have_pnl", 0.0)
    if skipped_opps.get("count", 0) >= 5 and abs(skipped_pnl) > 100:
        wr = skipped_opps.get("would_have_win_rate", 0) * 100
        suggestions.append(
            f"⏭ Skipped signals (max positions / low cash) would have generated "
            f"~${skipped_pnl:+.0f} at {wr:.0f}% WR — "
            f"consider increasing max-positions or seed capital"
        )

    # 8. Overall win rate trend
    overall_wr = overall.get("win_rate", 0)
    if overall.get("count", 0) >= 10 and overall_wr < _WIN_RATE_TARGET:
        suggestions.append(
            f"📉 Overall win rate is {overall_wr*100:.0f}% (target {_WIN_RATE_TARGET*100:.0f}%) — "
            f"review detector selection and exit profiles"
        )

    if not suggestions:
        suggestions.append("✅ All systems within normal parameters — no changes recommended this week")

    return suggestions


# ---------------------------------------------------------------------------
# Archive helpers
# ---------------------------------------------------------------------------

_REPORTS_DIR = "weekly_reports"


def save_report(state_dir: Path, report_data: dict) -> Path:
    """Persist a report dict to ``<state_dir>/weekly_reports/YYYY-MM-DD.json``.

    The filename is based on the week-start date so re-running the report on
    the same Saturday overwrites rather than duplicates.  Returns the saved path.
    """
    reports_dir = state_dir / _REPORTS_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)
    week_start = report_data.get("week_start", datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"))
    out = reports_dir / f"{week_start}.json"
    out.write_text(json.dumps(report_data, indent=2), encoding="utf-8")
    log.info("weekly report saved to %s", out)
    return out


def load_all_reports(state_dir: Path) -> list[dict]:
    """Return all saved weekly reports, newest first.

    Each entry is the full structured dict saved by ``save_report()``.
    Returns an empty list if no reports have been saved yet.
    """
    reports_dir = state_dir / _REPORTS_DIR
    if not reports_dir.exists():
        return []
    reports = []
    for p in sorted(reports_dir.glob("*.json"), reverse=True):
        try:
            reports.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception as exc:
            log.warning("weekly_report: failed to load %s: %s", p, exc)
    return reports


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def generate_report(state_dir: Path) -> tuple[list[str], dict]:
    """Generate the weekly report.

    Returns a 2-tuple:
      - ``messages``: list of Telegram-ready HTML strings (one per message)
      - ``data``: structured dict with all underlying stats (saved to disk)

    Multiple messages are used because Telegram's 4 096-char limit is easily
    hit with a full weekly digest.
    """
    now = datetime.now(tz=timezone.utc)
    week_start = _week_start()
    week_label = week_start.strftime("%d %b %Y")

    # Load all state from SQLite (per service), shaped as dicts so the rest of
    # this function is unchanged. Reading via Portfolio.load keeps US/UK/T212
    # cleanly separated and never reads the now-frozen legacy JSON backups.
    from dataclasses import asdict
    from switching.paper_trader import Portfolio
    from switching.skipped_tracker import SkippedTracker

    def _pf(filename: str) -> dict:
        p = Portfolio.load(state_dir / filename)
        return {
            "cash": p.cash,
            "positions": [asdict(x) for x in p.positions],
            "trades": [asdict(t) for t in p.trades],
        }

    paper_data = _pf("paper_portfolio.json")
    t212_data  = _pf("t212_portfolio.json")
    uk_data    = _pf("uk_portfolio.json")
    skipped_signals: list[dict] = [
        s.to_dict()
        for s in SkippedTracker.load(state_dir / "skipped_signals.json", "us").skipped
    ]

    paper_trades_all: list[dict] = paper_data["trades"]
    t212_trades_all: list[dict]  = t212_data["trades"]
    uk_trades_all: list[dict]    = uk_data["trades"]

    # Split into this-week vs historical
    def _this_week(trades: list[dict]) -> list[dict]:
        return [
            t for t in trades
            if _parse_dt(t.get("exit_dt") or t.get("entry_dt", "")) >= week_start
        ]

    paper_week = _this_week(paper_trades_all)
    t212_week  = _this_week(t212_trades_all)
    uk_week    = _this_week(uk_trades_all)

    # Aggregate stats
    paper_all_stats   = _analyse_trades(paper_trades_all)
    paper_week_stats  = _analyse_trades(paper_week)
    t212_all_stats    = _analyse_trades(t212_trades_all)
    t212_week_stats   = _analyse_trades(t212_week)
    uk_all_stats      = _analyse_trades(uk_trades_all)

    det_rows   = _detector_rankings(paper_trades_all)
    exit_brkdn = _exit_reason_breakdown(paper_trades_all)
    sev_data   = _severity_analysis(paper_trades_all)
    matched    = _t212_vs_paper(t212_trades_all, paper_trades_all)
    skipped_opps = _skipped_opportunity(skipped_signals)
    suggestions = _generate_suggestions(
        overall=paper_all_stats,
        detector_rows=det_rows,
        exit_breakdown=exit_brkdn,
        severity_data=sev_data,
        t212_matched=matched,
        t212_stats=t212_all_stats,
        paper_stats=paper_all_stats,
        skipped_opps=skipped_opps,
    )

    # -----------------------------------------------------------------------
    # Message 1: Header + Portfolio + This week
    # -----------------------------------------------------------------------
    cash = paper_data.get("cash", 0.0)
    invested = sum(p.get("entry_price", 0) * p.get("shares", 0) for p in paper_data.get("positions", []))
    total_val = cash + invested

    best_this_week = max(paper_week, key=lambda t: t.get("pct_return", 0), default=None)
    worst_this_week = min(paper_week, key=lambda t: t.get("pct_return", 0), default=None)

    m1_lines = [
        f"📅 <b>Weekly Report — week of {week_label}</b>",
        f"Generated: {now.strftime('%Y-%m-%d %H:%M')} UTC",
        "",
        "💼 <b>Portfolio (paper trade)</b>",
        f"Cash: ${cash:,.2f} | Invested: ${invested:,.2f} | Total: ${total_val:,.2f}",
        (
            f"All-time: {paper_all_stats['count']} trades, "
            f"{paper_all_stats['win_rate']*100:.0f}% WR, "
            f"${paper_all_stats['total_pnl']:+,.2f} P&amp;L"
        ),
        "",
        f"📆 <b>This week ({len(paper_week)} trades)</b>",
    ]
    if paper_week:
        m1_lines.append(
            f"Win rate: {paper_week_stats['win_rate']*100:.0f}% "
            f"({paper_week_stats['wins']}/{len(paper_week)})  "
            f"P&amp;L: ${paper_week_stats['total_pnl']:+,.2f}"
        )
        if best_this_week:
            m1_lines.append(
                f"Best:  {best_this_week.get('ticker','?')} "
                f"{best_this_week.get('pct_return',0)*100:+.1f}% "
                f"(${best_this_week.get('pnl',0):+.2f}) — {best_this_week.get('detector','')}"
            )
        if worst_this_week and worst_this_week is not best_this_week:
            m1_lines.append(
                f"Worst: {worst_this_week.get('ticker','?')} "
                f"{worst_this_week.get('pct_return',0)*100:+.1f}% "
                f"(${worst_this_week.get('pnl',0):+.2f}) — {worst_this_week.get('detector','')}"
            )
    else:
        m1_lines.append("No trades closed this week.")

    if t212_trades_all:
        open_t212 = len(t212_data.get("positions", []))
        m1_lines += [
            "",
            f"📊 <b>T212 Demo ({len(t212_trades_all)} trades all-time)</b>",
            (
                f"Win rate: {t212_all_stats['win_rate']*100:.0f}%  "
                f"P&amp;L: ${t212_all_stats['total_pnl']:+,.2f}  "
                f"Open: {open_t212}"
            ),
            f"This week: {len(t212_week)} trades, ${t212_week_stats['total_pnl']:+,.2f}",
        ]

    if uk_trades_all or uk_data:
        uk_cash = uk_data.get("cash", 0.0)
        uk_open = len(uk_data.get("positions", []))
        m1_lines += [
            "",
            f"🇬🇧 <b>LSE Paper ({len(uk_trades_all)} trades all-time)</b>",
            (
                f"Cash: £{uk_cash:,.2f} | Open positions: {uk_open}  "
                f"Win rate: {uk_all_stats['win_rate']*100:.0f}%  "
                f"P&amp;L: £{uk_all_stats['total_pnl']:+,.2f}"
            ),
            f"This week: {len(uk_week)} trades",
        ]

    msg1 = "\n".join(m1_lines)

    # -----------------------------------------------------------------------
    # Message 2: Detector rankings
    # -----------------------------------------------------------------------
    m2_lines = ["🔭 <b>Detector Rankings (all-time)</b>", ""]
    for i, row in enumerate(det_rows, 1):
        n = row["count"]
        if n == 0:
            continue
        wr = row["win_rate"]
        if n < _MIN_TRADES_FOR_JUDGEMENT:
            badge = "⏳"   # not enough data
        elif wr >= _WIN_RATE_STRONG:
            badge = "⭐"
        elif wr >= _WIN_RATE_TARGET:
            badge = "✅"
        else:
            badge = "❌"
        m2_lines.append(
            f"{badge} <b>{row['detector']}</b>: "
            f"{wr*100:.0f}% WR  avg {row['avg_return']*100:+.2f}%  "
            f"${row['total_pnl']:+.0f}  ({n} trades)"
        )
    if not any(row["count"] > 0 for row in det_rows):
        m2_lines.append("No trades yet.")

    # Exit reason breakdown
    m2_lines += ["", "🚪 <b>Exit Reasons</b>"]
    total_trades = paper_all_stats["count"]
    for reason, stats in sorted(exit_brkdn.items(), key=lambda x: x[1]["count"], reverse=True):
        n = stats["count"]
        pct = n / total_trades * 100 if total_trades else 0
        avg_ret = stats["avg_return"] * 100
        m2_lines.append(
            f"  {reason}: {n} ({pct:.0f}%)  avg {avg_ret:+.2f}%  "
            f"${stats['total_pnl']:+.0f}"
        )

    # Severity correlation (brief)
    high = sev_data.get("high_severity", {})
    low = sev_data.get("low_severity", {})
    if high.get("count", 0) > 0 or low.get("count", 0) > 0:
        m2_lines += [
            "",
            "🎯 <b>Signal Severity → Outcome</b>",
            (
                f"  High (≥0.75): {high.get('count',0)} trades, "
                f"{high.get('win_rate',0)*100:.0f}% WR, "
                f"avg {high.get('avg_return',0)*100:+.2f}%"
            ),
            (
                f"  Low (<0.75):  {low.get('count',0)} trades, "
                f"{low.get('win_rate',0)*100:.0f}% WR, "
                f"avg {low.get('avg_return',0)*100:+.2f}%"
            ),
        ]

    msg2 = "\n".join(m2_lines)

    # -----------------------------------------------------------------------
    # Message 3: T212 slippage + skipped + suggestions
    # -----------------------------------------------------------------------
    m3_lines = []

    if matched:
        avg_slip = sum(m["slippage"] for m in matched) / len(matched)
        t212_ahead = sum(1 for m in matched if m["slippage"] > 0)
        m3_lines += [
            "💱 <b>T212 vs Paper (slippage on matched trades)</b>",
            f"  Matched: {len(matched)} trades",
            f"  Avg slippage: {avg_slip*100:+.3f}%",
            f"  T212 better: {t212_ahead}/{len(matched)} trades",
            "",
        ]
        # Show up to 5 biggest slippage examples
        sorted_matched = sorted(matched, key=lambda m: abs(m["slippage"]), reverse=True)[:5]
        m3_lines.append("  Top slippage:")
        for m in sorted_matched:
            direction = "🟢" if m["slippage"] > 0 else "🔴"
            m3_lines.append(
                f"    {direction} {m['ticker']} {m['entry_dt']}: "
                f"T212 {m['t212_return']*100:+.2f}% vs paper {m['paper_return']*100:+.2f}% "
                f"= {m['slippage']*100:+.2f}%"
            )
        m3_lines.append("")

    if skipped_opps.get("count", 0) > 0:
        sk = skipped_opps
        m3_lines += [
            "⏭ <b>Skipped Signal Opportunity Cost</b>",
            (
                f"  {sk['count']} completed simulations  "
                f"{sk['would_have_win_rate']*100:.0f}% WR  "
                f"~${sk['would_have_pnl']:+.0f} est. P&amp;L"
            ),
            "",
        ]

    m3_lines.append("💡 <b>This Week's Suggestions</b>")
    for sug in suggestions:
        m3_lines.append(f"  {sug}")

    msg3 = "\n".join(m3_lines)

    # -----------------------------------------------------------------------
    # Filter out empty messages and truncate each to Telegram limit
    # -----------------------------------------------------------------------
    messages = []
    for msg in (msg1, msg2, msg3):
        stripped = msg.strip()
        if stripped:
            # Telegram hard limit is 4096 chars; truncate gracefully
            if len(stripped) > 4000:
                stripped = stripped[:3990] + "\n…(truncated)"
            messages.append(stripped)

    # -----------------------------------------------------------------------
    # Structured data for archiving and dashboard display
    # -----------------------------------------------------------------------
    t212_slippage_avg = (
        sum(m["slippage"] for m in matched) / len(matched) if matched else None
    )
    structured_data: dict = {
        "generated_at": now.isoformat(),
        "week_start": week_start.strftime("%Y-%m-%d"),
        "week_label": week_label,
        "paper": {
            "cash": cash,
            "invested": invested,
            "total_value": total_val,
            "all_time": paper_all_stats,
            "this_week": paper_week_stats,
            "this_week_count": len(paper_week),
            "best_trade": {
                "ticker": best_this_week.get("ticker") if best_this_week else None,
                "pct_return": best_this_week.get("pct_return") if best_this_week else None,
                "pnl": best_this_week.get("pnl") if best_this_week else None,
                "detector": best_this_week.get("detector") if best_this_week else None,
            },
            "worst_trade": {
                "ticker": worst_this_week.get("ticker") if worst_this_week and worst_this_week is not best_this_week else None,
                "pct_return": worst_this_week.get("pct_return") if worst_this_week and worst_this_week is not best_this_week else None,
                "pnl": worst_this_week.get("pnl") if worst_this_week and worst_this_week is not best_this_week else None,
                "detector": worst_this_week.get("detector") if worst_this_week and worst_this_week is not best_this_week else None,
            },
        },
        "t212": {
            "all_time": t212_all_stats,
            "this_week": t212_week_stats,
            "this_week_count": len(t212_week),
            "open_count": len(t212_data.get("positions", [])),
        },
        "uk": {
            "all_time": uk_all_stats,
            "this_week_count": len(uk_week),
            "cash": uk_data.get("cash", 0.0),
            "open_count": len(uk_data.get("positions", [])),
        },
        "detector_rankings": det_rows,
        "exit_breakdown": exit_brkdn,
        "severity_analysis": sev_data,
        "t212_slippage": {
            "matched_count": len(matched),
            "avg_slippage": t212_slippage_avg,
            "t212_better_count": sum(1 for m in matched if m["slippage"] > 0),
            "top_examples": sorted(matched, key=lambda m: abs(m["slippage"]), reverse=True)[:10],
        },
        "skipped_opportunities": skipped_opps,
        "suggestions": suggestions,
        "messages": messages,
    }

    return messages, structured_data


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_and_send(state_dir: Path) -> bool:
    """Generate the weekly report, save it to disk, and send via Telegram.

    The structured data is always saved to
    ``<state_dir>/weekly_reports/YYYY-MM-DD.json`` regardless of whether
    the Telegram send succeeds, so the archive is never lost.

    Returns True if the Telegram send succeeded.
    """
    from switching.notifications import _send
    try:
        messages, data = generate_report(state_dir)

        # Always archive — even if Telegram fails
        try:
            save_report(state_dir, data)
        except Exception as exc:
            log.warning("weekly report: archive save failed: %s", exc)

        ok = True
        for msg in messages:
            if not _send(msg):
                ok = False
        if ok:
            log.info("weekly report sent (%d messages)", len(messages))
        else:
            log.warning("weekly report: one or more Telegram sends failed")
        return ok
    except Exception as exc:
        log.error("weekly report generation failed: %s", exc, exc_info=True)
        return False
