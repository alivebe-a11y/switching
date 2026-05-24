"""Flask web dashboard for paper-trading portfolio."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

from switching.exit_tracker import ExitTracker
from switching.paper_trader import Portfolio
from switching.skipped_tracker import SkippedTracker

log = logging.getLogger(__name__)

_STATE_PATH: Path = Path("/app/.cache/paper_portfolio.json")


def create_app(state_path: Path | None = None) -> Flask:
    app = Flask(__name__)
    if state_path:
        global _STATE_PATH
        _STATE_PATH = state_path

    @app.route("/")
    def dashboard():
        return render_template_string(_DASHBOARD_HTML)

    @app.route("/api/portfolio")
    def api_portfolio():
        p = Portfolio.load(_STATE_PATH)
        positions = []
        for pos in p.positions:
            cur_price = p.cached_prices.get(pos.ticker)
            pnl_pct = (cur_price / pos.entry_price - 1.0) if cur_price and pos.entry_price else None
            positions.append({
                "ticker": pos.ticker,
                "detector": pos.detector,
                "entry_price": pos.entry_price,
                "shares": pos.shares,
                "cost_basis": pos.cost_basis,
                "current_price": cur_price,
                "pnl_pct": pnl_pct,
                "pnl_dollar": (cur_price - pos.entry_price) * pos.shares if cur_price else None,
                "entry_dt": pos.entry_dt,
                "days_held": pos.days_held,
                "hold_days": pos.hold_days,
                "headline": pos.headline,
                "severity": pos.severity,
                "stop_loss": pos.stop_loss,
            })
        invested = sum(pos.cost_basis for pos in p.positions)
        market_value = sum(
            (pp["current_price"] or pp["entry_price"]) * pp["shares"]
            for pp in positions
        )
        wins = sum(1 for t in p.trades if t.pnl > 0)
        total_trades = len(p.trades)
        total_pnl = sum(t.pnl for t in p.trades)
        return jsonify({
            "cash": p.cash,
            "invested": invested,
            "market_value": market_value,
            "total_value": p.cash + market_value,
            "positions": positions,
            "open_count": len(p.positions),
            "trade_count": total_trades,
            "wins": wins,
            "win_rate": (wins / total_trades * 100) if total_trades else 0,
            "total_pnl": total_pnl,
            "prices_updated_at": p.last_scan_dt,
        })

    @app.route("/api/trades")
    def api_trades():
        p = Portfolio.load(_STATE_PATH)
        trades = []
        for t in reversed(p.trades):
            trades.append({
                "ticker": t.ticker,
                "detector": t.detector,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "shares": t.shares,
                "entry_dt": t.entry_dt,
                "exit_dt": t.exit_dt,
                "pnl": t.pnl,
                "pct_return": t.pct_return,
                "exit_reason": t.exit_reason,
                "headline": t.headline,
            })
        return jsonify({"trades": trades})

    @app.route("/api/signals")
    def api_signals():
        p = Portfolio.load(_STATE_PATH)
        return jsonify({
            "signals": p.last_signals,
            "scanned_at": p.last_scan_dt,
        })

    @app.route("/api/t212")
    def api_t212():
        """T212 demo portfolio — same structure as /api/portfolio but from t212_portfolio.json."""
        t212_path = _STATE_PATH.parent / "t212_portfolio.json"
        if not t212_path.exists():
            return jsonify({"available": False, "message": "T212 service not started yet."})

        t = Portfolio.load(t212_path)
        paper = Portfolio.load(_STATE_PATH)

        # Build positions list
        positions = []
        for pos in t.positions:
            positions.append({
                "ticker": pos.ticker,
                "detector": pos.detector,
                "entry_price": pos.entry_price,
                "shares": pos.shares,
                "cost_basis": pos.cost_basis,
                "entry_dt": pos.entry_dt,
                "days_held": pos.days_held,
                "hold_days": pos.hold_days,
                "headline": pos.headline,
                "stop_loss": pos.stop_loss,
            })

        # Trade history
        trades = []
        for tr in reversed(t.trades):
            trades.append({
                "ticker": tr.ticker,
                "detector": tr.detector,
                "entry_price": tr.entry_price,
                "exit_price": tr.exit_price,
                "shares": tr.shares,
                "entry_dt": tr.entry_dt,
                "exit_dt": tr.exit_dt,
                "pnl": tr.pnl,
                "pct_return": tr.pct_return,
                "exit_reason": tr.exit_reason,
                "headline": tr.headline,
            })

        # Comparison: find trades in both systems on same ticker+date
        paper_by_key: dict[str, float] = {}
        for tr in paper.trades:
            key = f"{tr.ticker}:{tr.entry_dt[:10]}"
            paper_by_key[key] = tr.pct_return

        comparison = []
        for tr in t.trades:
            key = f"{tr.ticker}:{tr.entry_dt[:10]}"
            if key in paper_by_key:
                paper_ret = paper_by_key[key]
                diff = tr.pct_return - paper_ret
                comparison.append({
                    "ticker": tr.ticker,
                    "entry_dt": tr.entry_dt[:10],
                    "t212_return": tr.pct_return,
                    "paper_return": paper_ret,
                    "slippage": round(diff, 4),
                })

        wins = sum(1 for tr in t.trades if tr.pnl > 0)
        total_trades = len(t.trades)
        total_pnl = sum(tr.pnl for tr in t.trades)
        invested = sum(pos.cost_basis for pos in t.positions)

        return jsonify({
            "available": True,
            "free_cash": t.cash,
            "invested": invested,
            "total": t.cash + invested,
            "ppl": total_pnl,
            "open_count": len(t.positions),
            "trade_count": total_trades,
            "wins": wins,
            "win_rate": (wins / total_trades * 100) if total_trades else 0,
            "total_pnl": total_pnl,
            "positions": positions,
            "trades": trades,
            "comparison": comparison,
            "last_scan_dt": t.last_scan_dt,
        })

    @app.route("/api/uk")
    def api_uk():
        """UK (LSE) paper portfolio — reads uk_portfolio.json."""
        uk_path = _STATE_PATH.parent / "uk_portfolio.json"
        if not uk_path.exists():
            return jsonify({"available": False, "message": "UK service not started yet."})

        p = Portfolio.load(uk_path)

        positions = []
        for pos in p.positions:
            # Prices stored in GBP (normalised from GBX at entry)
            current = p.cached_prices.get(pos.ticker)
            pct = (current / pos.entry_price - 1.0) if current else None
            positions.append({
                "ticker": pos.ticker,
                "detector": pos.detector,
                "entry_price": pos.entry_price,
                "current_price": current,
                "pct_change": round(pct, 4) if pct is not None else None,
                "shares": pos.shares,
                "cost_basis": pos.cost_basis,
                "entry_dt": pos.entry_dt,
                "days_held": pos.days_held,
                "hold_days": pos.hold_days,
                "headline": pos.headline,
                "stop_loss": pos.stop_loss,
            })

        trades = []
        for tr in reversed(p.trades):
            trades.append({
                "ticker": tr.ticker,
                "detector": tr.detector,
                "entry_price": tr.entry_price,
                "exit_price": tr.exit_price,
                "shares": tr.shares,
                "entry_dt": tr.entry_dt,
                "exit_dt": tr.exit_dt,
                "pnl": tr.pnl,
                "pct_return": tr.pct_return,
                "exit_reason": tr.exit_reason,
                "headline": tr.headline,
            })

        wins = sum(1 for tr in p.trades if tr.pnl > 0)
        total_trades = len(p.trades)
        total_pnl = sum(tr.pnl for tr in p.trades)
        invested = sum(pos.cost_basis for pos in p.positions)

        return jsonify({
            "available": True,
            "cash": p.cash,
            "invested": invested,
            "total": p.cash + invested,
            "total_pnl": total_pnl,
            "open_count": len(p.positions),
            "trade_count": total_trades,
            "wins": wins,
            "win_rate": (wins / total_trades * 100) if total_trades else 0,
            "positions": positions,
            "trades": trades,
            "last_scan_dt": p.last_scan_dt,
        })

    @app.route("/api/weekly-reports")
    def api_weekly_reports():
        """Return all saved weekly reports, newest first."""
        from switching.weekly_report import load_all_reports
        reports = load_all_reports(_STATE_PATH.parent)
        return jsonify({
            "count": len(reports),
            "reports": reports,
        })

    @app.route("/api/equity-curve")
    def api_equity_curve():
        p = Portfolio.load(_STATE_PATH)
        if not p.trades:
            return jsonify({"points": []})
        starting = p.total_value - sum(t.pnl for t in p.trades)
        points = [{"dt": p.trades[0].entry_dt, "value": starting}]
        running = starting
        for t in p.trades:
            running += t.pnl
            points.append({"dt": t.exit_dt, "value": round(running, 2)})
        return jsonify({"points": points})

    @app.route("/api/exit-tracker")
    def api_exit_tracker():
        tracker_path = _STATE_PATH.parent / "exit_tracker.json"
        tracker = ExitTracker.load(tracker_path)
        completed = [t for t in tracker.tracked if t.tracking_complete]
        active = [t for t in tracker.tracked if not t.tracking_complete]
        return jsonify({
            "active_count": len(active),
            "completed_count": len(completed),
            "active": [t.to_dict() for t in active],
            "completed": [t.to_dict() for t in completed],
            "summary": tracker._build_summary(),
        })

    @app.route("/api/review")
    def api_review():
        from switching.paper_trader import Portfolio, _build_review_insights
        from switching.exit_tracker import ExitTracker
        p = Portfolio.load(_STATE_PATH)
        tracker_path = _STATE_PATH.parent / "exit_tracker.json"
        exit_tracker = ExitTracker.load(tracker_path)
        insights = _build_review_insights(p, exit_tracker)
        by_detector: dict[str, dict] = {}
        for t in p.trades:
            d = by_detector.setdefault(t.detector, {"trades": 0, "wins": 0, "pnl": 0.0, "returns": []})
            d["trades"] += 1
            d["wins"] += 1 if t.pnl > 0 else 0
            d["pnl"] += t.pnl
            d["returns"].append(t.pct_return)
        detector_stats = {}
        for det, d in sorted(by_detector.items()):
            wr = d["wins"] / d["trades"] if d["trades"] else 0
            avg_ret = sum(d["returns"]) / len(d["returns"]) if d["returns"] else 0
            detector_stats[det] = {
                "trades": d["trades"],
                "win_rate": round(wr, 3),
                "total_pnl": round(d["pnl"], 2),
                "avg_return": round(avg_ret, 4),
            }
        milestones = []
        for m in (10, 25, 50, 100):
            milestones.append({"target": m, "reached": len(p.trades) >= m})
        return jsonify({
            "insights": insights,
            "trade_count": len(p.trades),
            "last_review_sent_dt": p.last_review_sent_dt,
            "detector_stats": detector_stats,
            "milestones": milestones,
        })

    @app.route("/api/charts")
    def api_charts():
        p = Portfolio.load(_STATE_PATH)
        if not p.trades:
            return jsonify({"equity": [], "pnl": [], "win_rate": [], "cash": []})

        starting = p.cash + sum(pos.cost_basis for pos in p.positions) - sum(t.pnl for t in p.trades)

        trades_sorted = sorted(p.trades, key=lambda t: t.exit_dt)

        # ── Equity curve — cumulative closed-trade P&L ──────────────────────
        equity_pts  = [{"dt": trades_sorted[0].entry_dt, "value": round(starting, 2)}]
        pnl_pts     = [{"dt": trades_sorted[0].entry_dt, "value": 0.0}]
        wr_pts      = []

        running = starting
        running_pnl = 0.0
        wins = 0

        for i, t in enumerate(trades_sorted, 1):
            running     += t.pnl
            running_pnl += t.pnl
            wins        += 1 if t.pnl > 0 else 0
            equity_pts.append({"dt": t.exit_dt, "value": round(running, 2)})
            pnl_pts.append({"dt": t.exit_dt, "value": round(running_pnl, 2)})
            wr_pts.append({"dt": t.exit_dt, "value": round(wins / i, 3)})

        # ── Cash curve — actual uninvested cash (drops on entry, rises on exit)
        # Interleave all buy/sell events in time order so the chart shows
        # the real cash balance, not a copy of the equity curve.
        cash_events: list[tuple[str, float]] = []
        for t in p.trades:
            cost = round(t.entry_price * t.shares, 2)
            cash_events.append((t.entry_dt, -cost))          # deployed
            cash_events.append((t.exit_dt,  cost + t.pnl))  # returned + P&L
        for pos in p.positions:
            cash_events.append((pos.entry_dt, -pos.cost_basis))  # still deployed

        cash_events.sort(key=lambda e: e[0])

        running_cash = starting
        cash_pts = [{"dt": cash_events[0][0], "value": round(running_cash, 2)}]
        for dt, delta in cash_events:
            running_cash = round(running_cash + delta, 2)
            cash_pts.append({"dt": dt, "value": running_cash})

        return jsonify({
            "equity": equity_pts,
            "pnl": pnl_pts,
            "win_rate": wr_pts,
            "cash": cash_pts,
        })

    @app.route("/api/analytics")
    def api_analytics():
        from datetime import datetime as _dt
        p = Portfolio.load(_STATE_PATH)

        # ── Exit profile breakdown per detector ────────────────────────────
        det_map: dict = {}
        for t in p.trades:
            d = det_map.setdefault(t.detector, {
                "trades": 0, "wins": 0, "returns": [], "hold_days_list": [],
                "by_exit": {},
            })
            d["trades"] += 1
            if t.pnl > 0:
                d["wins"] += 1
            d["returns"].append(t.pct_return)
            try:
                e = _dt.fromisoformat(t.entry_dt.replace("Z", "+00:00"))
                x = _dt.fromisoformat(t.exit_dt.replace("Z", "+00:00"))
                d["hold_days_list"].append(max(0, (x.date() - e.date()).days))
            except Exception:
                pass
            reason = t.exit_reason or "unknown"
            d["by_exit"][reason] = d["by_exit"].get(reason, 0) + 1

        exit_profiles = []
        for det, d in sorted(det_map.items()):
            n = d["trades"]
            total_exit = sum(d["by_exit"].values()) or 1
            exit_profiles.append({
                "detector": det,
                "trades": n,
                "win_rate": round(d["wins"] / n, 3) if n else 0,
                "avg_return": round(sum(d["returns"]) / len(d["returns"]), 4) if d["returns"] else 0,
                "avg_hold_days": round(sum(d["hold_days_list"]) / len(d["hold_days_list"]), 1) if d["hold_days_list"] else 0,
                "pct_stop_loss": round(d["by_exit"].get("stop_loss", 0) / total_exit, 3),
                "pct_first_green": round(d["by_exit"].get("first_green", 0) / total_exit, 3),
                "pct_hold_expiry": round(d["by_exit"].get("hold_expiry", 0) / total_exit, 3),
                "pct_peak_trailing": round(d["by_exit"].get("peak_trailing", 0) / total_exit, 3),
            })

        # ── Severity buckets (signal quality correlation) ──────────────────
        sev_buckets: dict = {}
        for t in p.trades:
            sev = getattr(t, "severity", 0.0)
            if sev >= 0.90:
                b = "0.90+"
            elif sev >= 0.80:
                b = "0.80–0.90"
            elif sev >= 0.70:
                b = "0.70–0.80"
            else:
                b = "<0.70"
            bkt = sev_buckets.setdefault(b, {"trades": 0, "wins": 0, "returns": []})
            bkt["trades"] += 1
            if t.pnl > 0:
                bkt["wins"] += 1
            bkt["returns"].append(t.pct_return)

        sev_data = []
        for label in ["0.90+", "0.80–0.90", "0.70–0.80", "<0.70"]:
            if label in sev_buckets:
                bkt = sev_buckets[label]
                n = bkt["trades"]
                sev_data.append({
                    "bucket": label,
                    "trades": n,
                    "win_rate": round(bkt["wins"] / n, 3) if n else 0,
                    "avg_return": round(sum(bkt["returns"]) / len(bkt["returns"]), 4) if bkt["returns"] else 0,
                })

        # ── Peak trailing summary ──────────────────────────────────────────
        peak_trades = [t for t in p.trades if t.exit_reason == "peak_trailing" or getattr(t, "peak_price", 0) > 0]
        peak_summary: dict = {"total": len(peak_trades), "trades": []}
        if peak_trades:
            peak_returns = [t.peak_price / t.entry_price - 1.0 for t in peak_trades if getattr(t, "peak_price", 0) > 0 and t.entry_price > 0]
            exit_returns = [t.pct_return for t in peak_trades]
            left_on_table = [(t.peak_price / t.entry_price - 1.0) - t.pct_return for t in peak_trades if getattr(t, "peak_price", 0) > 0 and t.entry_price > 0]
            peak_summary["avg_peak_pct"] = round(sum(peak_returns) / len(peak_returns), 4) if peak_returns else 0
            peak_summary["avg_exit_pct"] = round(sum(exit_returns) / len(exit_returns), 4) if exit_returns else 0
            peak_summary["avg_left_on_table"] = round(sum(left_on_table) / len(left_on_table), 4) if left_on_table else 0
            for t in reversed(peak_trades[-20:]):
                peak_summary["trades"].append({
                    "ticker": t.ticker,
                    "detector": t.detector,
                    "entry_price": t.entry_price,
                    "peak_price": getattr(t, "peak_price", 0),
                    "exit_price": t.exit_price,
                    "peak_pct": round(t.peak_price / t.entry_price - 1.0, 4) if getattr(t, "peak_price", 0) > 0 and t.entry_price > 0 else None,
                    "exit_pct": round(t.pct_return, 4),
                    "exit_reason": t.exit_reason,
                    "exit_dt": t.exit_dt,
                })

        return jsonify({
            "exit_profiles": exit_profiles,
            "severity_buckets": sev_data,
            "peak_trailing": peak_summary,
            "trade_count": len(p.trades),
        })

    @app.route("/api/options-compare")
    def api_options_compare():
        from switching.options_model import compare_options_vs_stock
        p = Portfolio.load(_STATE_PATH)
        try:
            iv = float(request.args.get("iv", "0.30"))
            dte = int(request.args.get("dte", "14"))
        except (ValueError, TypeError):
            iv, dte = 0.30, 14
        iv = max(0.05, min(2.0, iv))
        dte = max(1, min(90, dte))

        result = compare_options_vs_stock(p.trades, assumed_iv=iv, dte=dte)
        by_det = result.by_detector()
        by_det_list = sorted(
            [
                {
                    "detector": det,
                    "trades": v["trades"],
                    "stock_pnl": v["stock_pnl"],
                    "options_pnl": v["options_pnl"],
                    "stock_win_rate": v["stock_win_rate"],
                    "options_win_rate": v["options_win_rate"],
                }
                for det, v in by_det.items()
            ],
            key=lambda x: x["options_pnl"] - x["stock_pnl"],
            reverse=True,
        )
        return jsonify({
            "trade_count": len(result.trades),
            "total_stock_pnl": round(result.total_stock_pnl, 2),
            "total_options_pnl": round(result.total_options_pnl, 2),
            "stock_win_rate": round(result.stock_win_rate, 3),
            "options_win_rate": round(result.options_win_rate, 3),
            "options_better_count": result.options_better_count,
            "stock_better_count": result.stock_better_count,
            "assumed_iv": iv,
            "dte": dte,
            "by_detector": by_det_list,
        })

    @app.route("/api/skipped-signals")
    def api_skipped_signals():
        path = _STATE_PATH.parent / "skipped_signals.json"
        tracker = SkippedTracker.load(path)
        recent_active = sorted(
            (s for s in tracker.skipped if not s.tracking_complete),
            key=lambda s: s.skipped_at, reverse=True,
        )[:50]
        recent_completed = sorted(
            (s for s in tracker.skipped if s.tracking_complete),
            key=lambda s: s.simulated_exit_dt or s.skipped_at, reverse=True,
        )[:50]
        return jsonify({
            "active_count": tracker.active_count,
            "completed_count": tracker.completed_count,
            "active": [s.to_dict() for s in recent_active],
            "completed": [s.to_dict() for s in recent_completed],
            "summary": tracker._build_summary(),
        })

    return app


_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Switching — Paper Trading Dashboard</title>
<style>
:root {
  --bg: #0f1117;
  --card: #1a1d27;
  --border: #2a2d3a;
  --text: #e4e6eb;
  --dim: #8b8fa3;
  --green: #22c55e;
  --red: #ef4444;
  --blue: #3b82f6;
  --amber: #f59e0b;
  --cyan: #06b6d4;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
}
.container { max-width: 1400px; margin: 0 auto; padding: 1rem; }
header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 1rem 0; border-bottom: 1px solid var(--border); margin-bottom: 1.5rem;
}
header h1 { font-size: 1.4rem; font-weight: 600; }
header .status { font-size: 0.85rem; color: var(--dim); }
.kpi-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 1rem; margin-bottom: 1.5rem;
}
.kpi {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 8px; padding: 1.2rem;
}
.kpi .label { font-size: 0.75rem; color: var(--dim); text-transform: uppercase; letter-spacing: 0.05em; }
.kpi .value { font-size: 1.8rem; font-weight: 700; margin-top: 0.3rem; }
.kpi .sub { font-size: 0.8rem; color: var(--dim); margin-top: 0.2rem; }
.pos { color: var(--green); }
.neg { color: var(--red); }
.panel {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 8px; margin-bottom: 1.5rem; overflow: hidden;
}
.panel-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0.8rem 1.2rem; border-bottom: 1px solid var(--border);
}
.panel-header h2 { font-size: 1rem; font-weight: 600; }
.panel-header .badge {
  background: var(--blue); color: #fff; padding: 0.15rem 0.6rem;
  border-radius: 12px; font-size: 0.75rem;
}
table { width: 100%; border-collapse: collapse; }
th {
  text-align: left; padding: 0.6rem 1rem; font-size: 0.7rem;
  text-transform: uppercase; color: var(--dim); letter-spacing: 0.05em;
  border-bottom: 1px solid var(--border);
}
td { padding: 0.6rem 1rem; font-size: 0.85rem; border-bottom: 1px solid var(--border); }
tr:last-child td { border-bottom: none; }
tr:hover { background: rgba(255,255,255,0.02); }
.ticker { font-weight: 600; color: var(--cyan); }
.detector-tag {
  display: inline-block; background: rgba(59,130,246,0.15);
  color: var(--blue); padding: 0.1rem 0.5rem; border-radius: 4px;
  font-size: 0.75rem;
}
.exit-tag {
  display: inline-block; padding: 0.1rem 0.5rem; border-radius: 4px;
  font-size: 0.75rem;
}
.exit-first_green { background: rgba(34,197,94,0.15); color: var(--green); }
.exit-stop_loss { background: rgba(239,68,68,0.15); color: var(--red); }
.exit-hold_expiry, .exit-hold { background: rgba(139,143,163,0.15); color: var(--dim); }
.exit-take_profit { background: rgba(245,158,11,0.15); color: var(--amber); }
.exit-peak_trailing { background: rgba(6,182,212,0.15); color: var(--cyan); }
.lot-small { color: #fbbf24; }
.lot-medium { color: #f97316; }
.lot-large { color: var(--red); }
.filter-btn {
  background: transparent; border: 1px solid var(--border); color: var(--dim);
  padding: 0.2rem 0.7rem; border-radius: 4px; font-size: 0.75rem; cursor: pointer;
}
.filter-btn:hover { border-color: var(--blue); color: var(--text); }
.filter-btn.active-filter { background: rgba(59,130,246,0.15); border-color: var(--blue); color: var(--blue); }
.milestone-chip {
  display: inline-flex; align-items: center; gap: 0.3rem;
  padding: 0.2rem 0.7rem; border-radius: 12px; font-size: 0.75rem;
  border: 1px solid var(--border);
}
.milestone-chip.reached { background: rgba(34,197,94,0.15); color: var(--green); border-color: rgba(34,197,94,0.3); }
.milestone-chip.pending { background: rgba(139,143,163,0.1); color: var(--dim); }
.insight-warn { color: var(--amber); font-size: 0.82rem; margin-bottom: 0.35rem; }
.insight-good { color: var(--green); font-size: 0.82rem; margin-bottom: 0.35rem; }
.insight-info { color: var(--dim); font-size: 0.82rem; margin-bottom: 0.35rem; }
.severity-bar {
  display: inline-block; height: 6px; border-radius: 3px;
  background: var(--blue); min-width: 20px;
}
.empty-state { padding: 2rem; text-align: center; color: var(--dim); }
.chart-area {
  padding: 1.2rem; height: 200px; display: flex;
  align-items: flex-end; gap: 2px;
  overflow-x: auto; overflow-y: hidden;
}
.chart-area::-webkit-scrollbar { height: 4px; }
.chart-area::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
.chart-bar {
  flex: 0 0 24px; background: var(--blue); border-radius: 2px 2px 0 0;
  min-height: 2px; position: relative;
}
.chart-bar.loss { background: var(--red); }
.chart-bar.win { background: var(--green); }
.tabs { display: flex; gap: 0; }
.tab {
  padding: 0.5rem 1rem; cursor: pointer; font-size: 0.85rem;
  color: var(--dim); border-bottom: 2px solid transparent;
  transition: all 0.2s;
}
.tab.active { color: var(--text); border-bottom-color: var(--blue); }
.tab:hover { color: var(--text); }
.refresh-btn {
  background: none; border: 1px solid var(--border); color: var(--dim);
  padding: 0.3rem 0.8rem; border-radius: 4px; cursor: pointer;
  font-size: 0.8rem; transition: all 0.2s;
}
.refresh-btn:hover { border-color: var(--blue); color: var(--text); }
.headline-text { max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 768px) {
  .kpi-grid { grid-template-columns: repeat(2, 1fr); }
  .kpi .value { font-size: 1.3rem; }
  td, th { padding: 0.4rem 0.5rem; font-size: 0.75rem; }
}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>Switching &mdash; Paper Trading</h1>
    <div class="status">
      <button class="refresh-btn" onclick="refresh()">Refresh</button>
      <span id="last-update"></span>
    </div>
  </header>

  <div id="market-clock" style="
    background: var(--card); border: 1px solid var(--border); border-radius: 8px;
    padding: 0.8rem 1.2rem; margin-bottom: 1.5rem; display: flex;
    align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 0.5rem;
  ">
    <div style="display:flex;align-items:center;gap:1rem">
      <span id="market-indicator" style="display:inline-block;width:10px;height:10px;border-radius:50%;background:var(--green)"></span>
      <span id="market-status" style="font-weight:600;font-size:0.95rem;color:var(--green);">Market Open</span>
    </div>
    <div style="display:flex;gap:1.5rem;font-size:0.85rem;color:var(--dim)">
      <span>Open: <b style="color:var(--text)">14:30 GMT</b></span>
      <span>Close: <b style="color:var(--text)">21:00 GMT</b></span>
    </div>
    <div style="font-size:0.9rem">
      <span id="market-countdown" style="color:var(--green);font-weight:600;"></span>
    </div>
  </div>

  <div class="kpi-grid">
    <div class="kpi">
      <div class="label">Portfolio Value</div>
      <div class="value" id="kpi-total">--</div>
      <div class="sub" id="kpi-pnl-sub"></div>
    </div>
    <div class="kpi">
      <div class="label">Cash</div>
      <div class="value" id="kpi-cash">--</div>
      <div class="sub" id="kpi-invested-sub"></div>
    </div>
    <div class="kpi">
      <div class="label">Win Rate</div>
      <div class="value" id="kpi-winrate">--</div>
      <div class="sub" id="kpi-trades-sub"></div>
    </div>
    <div class="kpi">
      <div class="label">Total P&L</div>
      <div class="value" id="kpi-pnl">--</div>
      <div class="sub" id="kpi-return-sub"></div>
    </div>
  </div>

  <div style="border-bottom:1px solid var(--border);margin-bottom:1.5rem">
    <div class="tabs">
      <div class="tab active" id="tab-btn-overview" onclick="switchTab('overview')">Overview</div>
      <div class="tab" id="tab-btn-postexit" onclick="switchTab('postexit')">Post-Exit Tracker <span class="badge" id="tracker-tab-badge" style="margin-left:4px">0</span></div>
      <div class="tab" id="tab-btn-analytics" onclick="switchTab('analytics')">Analytics</div>
      <div class="tab" id="tab-btn-t212" onclick="switchTab('t212')">T212 Demo</div>
      <div class="tab" id="tab-btn-uk" onclick="switchTab('uk')">🇬🇧 LSE</div>
      <div class="tab" id="tab-btn-reports" onclick="switchTab('reports')">📋 Reports</div>
    </div>
  </div>

  <div id="tab-overview">

  <div class="panel">
    <div class="panel-header">
      <h2>Charts</h2>
      <span id="charts-trade-count" style="font-size:0.75rem;color:var(--dim)"></span>
    </div>
    <div id="charts-empty" class="empty-state" style="display:none">No trades yet — charts will appear after the first trade closes.</div>
    <div id="charts-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:0;padding:0">
      <div style="padding:1rem;border-right:1px solid var(--border);border-bottom:1px solid var(--border)">
        <div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;margin-bottom:0.4rem">Portfolio Value</div>
        <div id="chart-val-num" style="font-size:1.3rem;font-weight:700;margin-bottom:0.4rem">--</div>
        <div id="chart-equity"></div>
      </div>
      <div style="padding:1rem;border-bottom:1px solid var(--border)">
        <div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;margin-bottom:0.4rem">Uninvested Cash</div>
        <div id="chart-cash-num" style="font-size:1.3rem;font-weight:700;margin-bottom:0.4rem">--</div>
        <div id="chart-cash"></div>
      </div>
      <div style="padding:1rem;border-right:1px solid var(--border)">
        <div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;margin-bottom:0.4rem">Win Rate</div>
        <div id="chart-wr-num" style="font-size:1.3rem;font-weight:700;margin-bottom:0.4rem">--</div>
        <div id="chart-winrate"></div>
      </div>
      <div style="padding:1rem">
        <div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;margin-bottom:0.4rem">Cumulative P&amp;L</div>
        <div id="chart-pnl-num" style="font-size:1.3rem;font-weight:700;margin-bottom:0.4rem">--</div>
        <div id="chart-pnl"></div>
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-header">
      <h2>Open Positions</h2>
      <div>
        <span id="prices-stamp" style="font-size:0.7rem;color:var(--dim);margin-right:0.5rem"></span>
        <span class="badge" id="pos-count">0</span>
      </div>
    </div>
    <div id="positions-body">
      <div class="empty-state">No open positions</div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-header">
      <h2>Trade History</h2>
      <span class="badge" id="trade-count">0</span>
    </div>
    <div id="trades-chart" class="chart-area"></div>
    <div id="trades-body">
      <div class="empty-state">No trades yet</div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-header">
      <h2>Recent Signals</h2>
      <span class="badge" id="signal-count">0</span>
    </div>
    <div id="signals-body">
      <div class="empty-state">Waiting for paper trader scan...</div>
    </div>
  </div>

  </div><!-- /tab-overview -->

  <div id="tab-postexit" style="display:none">

  <div class="panel">
    <div class="panel-header">
      <h2>Post-Exit Tracker</h2>
      <span style="font-size:0.75rem;color:var(--dim)">Price tracked for 20 days after each trade closes — did we exit too early?</span>
    </div>
    <!-- KPI row -->
    <div id="tracker-kpis" style="display:flex;gap:2rem;flex-wrap:wrap;padding:1rem 1.2rem;border-bottom:1px solid var(--border)">
      <div><div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em">Active</div><div id="tracker-kpi-active" style="font-size:1.4rem;font-weight:700">--</div></div>
      <div><div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em">Completed</div><div id="tracker-kpi-completed" style="font-size:1.4rem;font-weight:700">--</div></div>
      <div><div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em">Avg Left on Table</div><div id="tracker-kpi-lot" style="font-size:1.4rem;font-weight:700">--</div></div>
      <div><div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em">Exited Too Early</div><div id="tracker-kpi-early" style="font-size:1.4rem;font-weight:700">--</div></div>
    </div>
    <!-- Insights -->
    <div id="tracker-insights" style="padding:0.8rem 1.2rem;display:none;border-bottom:1px solid var(--border)"></div>
    <!-- Summary by detector -->
    <div id="tracker-summary" style="display:none">
      <div style="padding:0.6rem 1.2rem;font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid var(--border)">By Detector (completed tracks only)</div>
      <div id="tracker-summary-body"></div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-header">
      <h2>All Tracked Positions</h2>
      <div style="display:flex;gap:0.5rem;align-items:center">
        <button onclick="filterTracker('all')"   id="tf-all"       class="filter-btn active-filter">All</button>
        <button onclick="filterTracker('active')"    id="tf-active"    class="filter-btn">Active</button>
        <button onclick="filterTracker('completed')" id="tf-completed" class="filter-btn">Completed</button>
      </div>
    </div>
    <div id="tracker-body">
      <div class="empty-state">No post-exit data yet. Tracks prices for 20 days after each trade closes.</div>
    </div>
  </div>

  </div><!-- /tab-postexit -->

  <div id="tab-analytics" style="display:none">

  <div class="panel" id="review-panel">
    <div class="panel-header">
      <h2>Strategy Review</h2>
      <span class="badge" id="review-trade-count">0 trades</span>
    </div>
    <div id="review-milestones" style="padding:0.6rem 1.2rem;display:flex;gap:0.5rem;flex-wrap:wrap;border-bottom:1px solid var(--border)"></div>
    <div id="review-insights" style="padding:0.8rem 1.2rem;display:none"></div>
    <div id="review-detector-body">
      <div class="empty-state">Accumulating trades — insights appear after 10+ trades per detector.</div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-header">
      <h2>Missed Signals (Would-Have-Been P&L)</h2>
      <span class="badge" id="skipped-count">0</span>
    </div>
    <div id="skipped-summary" style="padding:0.8rem 1.2rem;display:none;font-size:0.85rem;color:var(--dim)"></div>
    <div id="skipped-body">
      <div class="empty-state">No skipped signals tracked yet. Signals skipped due to max-positions or insufficient cash will appear here with simulated outcomes.</div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-header">
      <h2>Exit Profile Tuning</h2>
      <span style="font-size:0.75rem;color:var(--dim)">% breakdown of how each detector exits — use to tune hold_days &amp; first_green_pct</span>
    </div>
    <div id="analytics-exit-body">
      <div class="empty-state">No trade data yet.</div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-header">
      <h2>Signal Severity → Performance</h2>
      <span style="font-size:0.75rem;color:var(--dim)">Higher severity = stronger regex signal. Informs when to enable AI gating.</span>
    </div>
    <div id="analytics-sev-body">
      <div class="empty-state">No trade data yet.</div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-header">
      <h2>Peak Trailing Summary</h2>
      <span style="font-size:0.75rem;color:var(--dim)">Trades that hit +8% day-0 and switched to 1-second trailing stop</span>
    </div>
    <div id="analytics-peak-kpis" style="display:none;padding:1rem 1.2rem;display:flex;gap:2rem;flex-wrap:wrap;border-bottom:1px solid var(--border)"></div>
    <div id="analytics-peak-body">
      <div class="empty-state">No peak trailing trades yet. Positions that hit +8% on day-0 will appear here.</div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-header">
      <h2>Options Lab 🧪</h2>
      <span style="font-size:0.75rem;color:var(--dim)">If we had bought ATM calls instead of stock — what would P&amp;L have been?</span>
    </div>
    <div style="padding:1rem 1.2rem;display:flex;align-items:center;gap:1.5rem;border-bottom:1px solid var(--border);flex-wrap:wrap">
      <label style="font-size:0.85rem;display:flex;align-items:center;gap:0.5rem">
        Implied Vol (IV)
        <input type="range" id="options-iv" min="10" max="80" value="30" step="5"
               style="width:110px;accent-color:var(--cyan)"
               oninput="document.getElementById('options-iv-val').textContent=this.value+'%'">
        <span id="options-iv-val" style="min-width:2.5rem;font-weight:700;color:var(--cyan)">30%</span>
      </label>
      <label style="font-size:0.85rem;display:flex;align-items:center;gap:0.5rem">
        Days to Expiry (DTE)
        <select id="options-dte" style="background:var(--card);border:1px solid var(--border);color:var(--fg);padding:0.25rem 0.5rem;border-radius:4px;font-size:0.85rem">
          <option value="7">7 d — weekly</option>
          <option value="14" selected>14 d — 2-week</option>
          <option value="21">21 d — 3-week</option>
          <option value="30">30 d — monthly</option>
        </select>
      </label>
      <button onclick="loadOptionsLab()"
              style="background:var(--blue);color:#fff;border:none;padding:0.35rem 1rem;border-radius:4px;cursor:pointer;font-size:0.85rem;font-weight:600">
        Run Comparison
      </button>
    </div>
    <div id="options-kpis" style="display:none;padding:1rem 1.2rem;gap:2rem;flex-wrap:wrap;border-bottom:1px solid var(--border)"></div>
    <div id="options-body">
      <div class="empty-state">Set IV and DTE above, then click <strong>Run Comparison</strong>. Uses Black-Scholes to model what ATM calls on the same signals would have returned (same dollar amount committed to premium as to stock).</div>
    </div>
  </div>

  </div><!-- /tab-analytics -->

  <div id="tab-t212" style="display:none">

  <div class="panel">
    <div class="panel-header">
      <h2>T212 Demo Account</h2>
      <span id="t212-stamp" style="font-size:0.7rem;color:var(--dim)"></span>
    </div>
    <div id="t212-unavailable" class="empty-state" style="display:none">T212 service not started. Run: <code>docker compose up trade-t212 -d</code></div>
    <div id="t212-kpis" style="display:flex;gap:2rem;flex-wrap:wrap;padding:1rem 1.2rem;border-bottom:1px solid var(--border)">
      <div><div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em">Free Cash</div><div id="t212-free" style="font-size:1.4rem;font-weight:700">--</div></div>
      <div><div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em">Invested</div><div id="t212-invested" style="font-size:1.4rem;font-weight:700">--</div></div>
      <div><div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em">Total</div><div id="t212-total" style="font-size:1.4rem;font-weight:700">--</div></div>
      <div><div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em">Closed P&amp;L</div><div id="t212-pnl" style="font-size:1.4rem;font-weight:700">--</div></div>
      <div><div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em">Trades</div><div id="t212-trades" style="font-size:1.4rem;font-weight:700">--</div></div>
      <div><div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em">Win Rate</div><div id="t212-wr" style="font-size:1.4rem;font-weight:700">--</div></div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-header"><h2>Open Positions</h2><span class="badge" id="t212-pos-count">0</span></div>
    <div id="t212-positions-body"><div class="empty-state">No open positions</div></div>
  </div>

  <div class="panel">
    <div class="panel-header"><h2>Trade History</h2><span class="badge" id="t212-trade-count">0</span></div>
    <div id="t212-trades-body"><div class="empty-state">No trades yet</div></div>
  </div>

  <div class="panel">
    <div class="panel-header">
      <h2>Slippage vs Paper Trader</h2>
      <span style="font-size:0.75rem;color:var(--dim)">Same signals, different fills — T212 demo vs yfinance theoretical</span>
    </div>
    <div id="t212-comparison-body"><div class="empty-state">Comparison data will appear once both systems have traded the same signal.</div></div>
  </div>

  </div><!-- /tab-t212 -->

  <div id="tab-uk" style="display:none">

  <div class="panel">
    <div class="panel-header">
      <h2>LSE Paper Trader (UK)</h2>
      <span id="uk-stamp" style="font-size:0.7rem;color:var(--dim)"></span>
    </div>
    <div id="uk-unavailable" class="empty-state" style="display:none">UK service not started. Run: <code>docker compose up paper-trade-uk -d</code></div>
    <div id="uk-kpis" style="display:flex;gap:2rem;flex-wrap:wrap;padding:1rem 1.2rem;border-bottom:1px solid var(--border)">
      <div><div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em">Cash</div><div id="uk-cash" style="font-size:1.4rem;font-weight:700">--</div></div>
      <div><div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em">Invested</div><div id="uk-invested" style="font-size:1.4rem;font-weight:700">--</div></div>
      <div><div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em">Total</div><div id="uk-total" style="font-size:1.4rem;font-weight:700">--</div></div>
      <div><div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em">Closed P&amp;L</div><div id="uk-pnl" style="font-size:1.4rem;font-weight:700">--</div></div>
      <div><div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em">Trades</div><div id="uk-trades" style="font-size:1.4rem;font-weight:700">--</div></div>
      <div><div style="font-size:0.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em">Win Rate</div><div id="uk-wr" style="font-size:1.4rem;font-weight:700">--</div></div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-header"><h2>Open Positions</h2><span class="badge" id="uk-pos-count">0</span></div>
    <div id="uk-positions-body"><div class="empty-state">No open positions — LSE signals will appear here after 08:00 London time</div></div>
  </div>

  <div class="panel">
    <div class="panel-header"><h2>Trade History</h2><span class="badge" id="uk-trade-count">0</span></div>
    <div id="uk-trades-body"><div class="empty-state">No trades yet</div></div>
  </div>

  </div><!-- /tab-uk -->

  <div id="tab-reports" style="display:none">

  <div class="panel">
    <div class="panel-header">
      <h2>Weekly Performance Reports</h2>
      <span class="badge" id="reports-count">0</span>
    </div>
    <div style="font-size:0.78rem;color:var(--dim);padding:0.5rem 1.2rem 0">
      Auto-generated every Saturday at 09:00 UTC. Run <code>switching weekly-report</code> to generate on demand.
    </div>
    <div id="reports-list"><div class="empty-state">No reports yet — first one sends Saturday morning.</div></div>
  </div>

  <div class="panel" id="report-detail" style="display:none">
    <div class="panel-header">
      <h2 id="report-detail-title">Report Detail</h2>
      <button onclick="$('#report-detail').style.display='none'" style="background:none;border:1px solid var(--border);color:var(--fg);padding:2px 10px;border-radius:4px;cursor:pointer;font-size:0.8rem">✕ Close</button>
    </div>
    <div id="report-detail-body"></div>
  </div>

  </div><!-- /tab-reports -->

</div>

<script>
function $(s) { return document.querySelector(s); }
function fmt(n, d) { return n == null ? '--' : '$' + n.toFixed(d || 2); }
function pct(n) { return n == null ? '--' : (n * 100).toFixed(1) + '%'; }
function color(n) { return n > 0 ? 'pos' : n < 0 ? 'neg' : ''; }

function drawLineChart(containerId, points, opts = {}) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const {
    color = null, refLine = null,
    fillArea = false, height = 90,
    formatY = v => v.toFixed(2),
  } = opts;

  if (!points || points.length < 2) {
    el.innerHTML = '<div style="height:' + height + 'px;display:flex;align-items:center;justify-content:center;color:var(--border);font-size:0.75rem">waiting for data</div>';
    return;
  }

  const vals = points.map(p => p.value);
  const lastVal = vals[vals.length - 1];
  const lineColor = color || (lastVal >= (refLine ?? 0) ? 'var(--green)' : 'var(--red)');

  let yMin = Math.min(...vals);
  let yMax = Math.max(...vals);
  if (refLine !== null) { yMin = Math.min(yMin, refLine); yMax = Math.max(yMax, refLine); }
  const pad = yMax === yMin ? Math.abs(yMax) * 0.1 || 1 : (yMax - yMin) * 0.12;
  yMin -= pad; yMax += pad;
  const yRange = yMax - yMin;

  const W = 400, H = height;
  const pl = 0, pr = 0, pt = 6, pb = 6;
  const n = points.length;
  const toX = i  => pl + (i / (n - 1)) * (W - pl - pr);
  const toY = v  => pt + (1 - (v - yMin) / yRange) * (H - pt - pb);

  const polyPts = points.map((p, i) => `${toX(i).toFixed(1)},${toY(p.value).toFixed(1)}`).join(' ');
  const lx = toX(n - 1).toFixed(1), ly = toY(lastVal).toFixed(1);

  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" preserveAspectRatio="none" style="display:block;overflow:visible">`;

  // reference line (e.g. 0 for P&L, 50% for win rate)
  if (refLine !== null) {
    const ry = toY(refLine).toFixed(1);
    svg += `<line x1="0" y1="${ry}" x2="${W}" y2="${ry}" stroke="rgba(255,255,255,0.1)" stroke-width="1" stroke-dasharray="4,3"/>`;
  }

  // fill area between line and ref/baseline
  if (fillArea) {
    const baseY = refLine !== null ? toY(refLine).toFixed(1) : H - pb;
    const fillCol = lastVal >= (refLine ?? 0) ? 'rgba(34,197,94,0.12)' : 'rgba(239,68,68,0.12)';
    svg += `<polygon points="${toX(0).toFixed(1)},${baseY} ${polyPts} ${lx},${baseY}" fill="${fillCol}"/>`;
  }

  // main line
  svg += `<polyline points="${polyPts}" fill="none" stroke="${lineColor}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;

  // end dot
  svg += `<circle cx="${lx}" cy="${ly}" r="3" fill="${lineColor}"/>`;

  svg += '</svg>';
  el.innerHTML = svg;
}

async function loadCharts() {
  try {
    const r = await fetch('/api/charts');
    const d = await r.json();

    if (!d.equity || d.equity.length < 2) {
      $('#charts-empty').style.display = 'block';
      $('#charts-grid').style.display = 'none';
      return;
    }
    $('#charts-empty').style.display = 'none';
    $('#charts-grid').style.display = 'grid';

    const lastEquity = d.equity[d.equity.length - 1].value;
    const lastPnl    = d.pnl[d.pnl.length - 1].value;
    const lastWr     = d.win_rate[d.win_rate.length - 1].value;
    const lastCash   = d.cash[d.cash.length - 1].value;

    $('#chart-val-num').textContent  = '$' + lastEquity.toFixed(2);
    $('#chart-val-num').className    = '';

    $('#chart-cash-num').textContent = '$' + lastCash.toFixed(2);

    $('#chart-wr-num').textContent   = (lastWr * 100).toFixed(0) + '%';
    $('#chart-wr-num').className     = lastWr >= 0.55 ? 'pos' : 'neg';

    $('#chart-pnl-num').textContent  = (lastPnl >= 0 ? '+' : '') + '$' + lastPnl.toFixed(2);
    $('#chart-pnl-num').className    = lastPnl >= 0 ? 'pos' : 'neg';

    $('#charts-trade-count').textContent = d.equity.length - 1 + ' trades';

    drawLineChart('chart-equity',  d.equity,   { color: 'var(--blue)',  height: 90 });
    drawLineChart('chart-cash',    d.cash,     { color: 'var(--cyan)',  height: 90 });
    drawLineChart('chart-winrate', d.win_rate, { refLine: 0.5, height: 90, formatY: v => (v*100).toFixed(0)+'%' });
    drawLineChart('chart-pnl',     d.pnl,      { refLine: 0, fillArea: true, height: 90 });
  } catch(e) {
    console.error('charts load failed', e);
  }
}

async function loadPortfolio() {
  try {
    const r = await fetch('/api/portfolio');
    const d = await r.json();

    $('#kpi-total').textContent = fmt(d.total_value);
    $('#kpi-total').className = 'value';

    $('#kpi-cash').textContent = fmt(d.cash);
    $('#kpi-invested-sub').textContent = 'Invested: ' + fmt(d.invested);

    $('#kpi-winrate').textContent = d.trade_count ? d.win_rate.toFixed(0) + '%' : '--';
    $('#kpi-trades-sub').textContent = d.wins + ' / ' + d.trade_count + ' trades';

    $('#kpi-pnl').textContent = fmt(d.total_pnl);
    $('#kpi-pnl').className = 'value ' + color(d.total_pnl);

    let startVal = d.total_value - d.total_pnl;
    let retPct = startVal > 0 ? ((d.total_value / startVal - 1) * 100).toFixed(1) : '0.0';
    $('#kpi-pnl-sub').textContent = retPct + '% return';
    $('#kpi-return-sub').textContent = 'from ' + fmt(startVal) + ' start';

    $('#pos-count').textContent = d.open_count;

    if (d.prices_updated_at) {
      let ts = d.prices_updated_at.slice(0, 16).replace('T', ' ');
      let elem = $('#prices-stamp');
      if (elem) elem.textContent = 'Prices as of ' + ts + ' UTC (cached, 10-min refresh)';
    }

    if (d.positions.length === 0) {
      $('#positions-body').innerHTML = '<div class="empty-state">No open positions</div>';
    } else {
      let html = '<table><thead><tr><th>Ticker</th><th>Detector</th><th>Entry</th><th>Current</th><th>P&L</th><th>Shares</th><th>Value</th><th>Day</th></tr></thead><tbody>';
      d.positions.forEach(p => {
        let c = color(p.pnl_pct);
        html += '<tr>';
        html += '<td class="ticker">' + p.ticker + '</td>';
        html += '<td><span class="detector-tag">' + p.detector + '</span></td>';
        html += '<td>' + fmt(p.entry_price) + '</td>';
        html += '<td>' + (p.current_price ? fmt(p.current_price) : '--') + '</td>';
        html += '<td class="' + c + '">' + pct(p.pnl_pct) + (p.pnl_dollar != null ? ' (' + fmt(p.pnl_dollar) + ')' : '') + '</td>';
        html += '<td>' + p.shares.toFixed(4) + '</td>';
        html += '<td>' + fmt(p.cost_basis) + '</td>';
        html += '<td>' + p.days_held + '/' + p.hold_days + '</td>';
        html += '</tr>';
      });
      html += '</tbody></table>';
      $('#positions-body').innerHTML = html;
    }
  } catch(e) {
    console.error('portfolio load failed', e);
  }
}

async function loadTrades() {
  try {
    const r = await fetch('/api/trades');
    const d = await r.json();
    $('#trade-count').textContent = d.trades.length;

    if (d.trades.length === 0) {
      $('#trades-body').innerHTML = '<div class="empty-state">No trades yet</div>';
      $('#trades-chart').innerHTML = '';
      return;
    }

    // Mini bar chart of returns
    let bars = d.trades.slice().reverse();
    let maxAbs = Math.max(...bars.map(t => Math.abs(t.pct_return)), 0.01);
    let chartHtml = '';
    bars.forEach(t => {
      let h = Math.max(2, Math.abs(t.pct_return) / maxAbs * 160);
      let cls = t.pnl >= 0 ? 'win' : 'loss';
      chartHtml += '<div class="chart-bar ' + cls + '" style="height:' + h + 'px" title="' + t.ticker + ' ' + (t.pct_return*100).toFixed(1) + '%"></div>';
    });
    $('#trades-chart').innerHTML = chartHtml;

    let html = '<table><thead><tr><th>Ticker</th><th>Detector</th><th>Entry</th><th>Exit</th><th>Return</th><th>P&L</th><th>Exit</th><th>Date</th><th>Signal</th></tr></thead><tbody>';
    d.trades.forEach(t => {
      let c = color(t.pnl);
      html += '<tr>';
      html += '<td class="ticker">' + t.ticker + '</td>';
      html += '<td><span class="detector-tag">' + t.detector + '</span></td>';
      html += '<td>' + fmt(t.entry_price) + '</td>';
      html += '<td>' + fmt(t.exit_price) + '</td>';
      html += '<td class="' + c + '">' + (t.pct_return * 100).toFixed(1) + '%</td>';
      html += '<td class="' + c + '">' + fmt(t.pnl) + '</td>';
      html += '<td><span class="exit-tag exit-' + t.exit_reason + '">' + t.exit_reason + '</span></td>';
      html += '<td>' + (t.exit_dt || '').slice(0, 10) + '</td>';
      html += '<td class="headline-text">' + (t.headline || '').slice(0, 60) + '</td>';
      html += '</tr>';
    });
    html += '</tbody></table>';
    $('#trades-body').innerHTML = html;
  } catch(e) {
    console.error('trades load failed', e);
  }
}

async function loadSignals() {
  try {
    const r = await fetch('/api/signals');
    const d = await r.json();
    $('#signal-count').textContent = d.signals.length;
    if (!d.signals || d.signals.length === 0) {
      let scanInfo = d.scanned_at ? 'Last scan: ' + d.scanned_at.slice(0, 16).replace('T', ' ') + ' UTC' : 'Waiting for first scan...';
      $('#signals-body').innerHTML = '<div class="empty-state">No signals detected. ' + scanInfo + '</div>';
      return;
    }
    let scanTime = d.scanned_at ? d.scanned_at.slice(0, 16).replace('T', ' ') : '';
    let html = '<div style="padding:0.4rem 1rem;font-size:0.75rem;color:var(--dim)">Last scan: ' + scanTime + ' UTC</div>';
    html += '<table><thead><tr><th>Ticker</th><th>Company</th><th>Detector</th><th>Severity</th><th>Headline</th><th>Time</th></tr></thead><tbody>';
    d.signals.forEach(s => {
      let w = Math.round(s.severity * 100);
      html += '<tr>';
      html += '<td class="ticker">' + s.ticker + '</td>';
      html += '<td>' + (s.company || '').slice(0, 30) + '</td>';
      html += '<td><span class="detector-tag">' + s.detector + '</span></td>';
      html += '<td><span class="severity-bar" style="width:' + w + 'px"></span> ' + s.severity.toFixed(2) + '</td>';
      html += '<td class="headline-text">' + (s.headline || '').slice(0, 80) + '</td>';
      html += '<td>' + (s.event_dt || '').slice(0, 16) + '</td>';
      html += '</tr>';
    });
    html += '</tbody></table>';
    $('#signals-body').innerHTML = html;
  } catch(e) {
    console.error('signals load failed', e);
  }
}

let _trackerData = null;
let _trackerFilter = 'all';

async function loadExitTracker() {
  try {
    const r = await fetch('/api/exit-tracker');
    _trackerData = await r.json();
    const d = _trackerData;
    const total = d.active_count + d.completed_count;

    // Update tab badge (always, regardless of which tab is active)
    $('#tracker-tab-badge').textContent = d.active_count > 0 ? d.active_count + ' active' : total;

    if (total === 0) {
      ['tracker-kpi-active','tracker-kpi-completed','tracker-kpi-lot','tracker-kpi-early'].forEach(id => { const el = $('#' + id); if(el) el.textContent = '0'; });
      const tb = $('#tracker-body'); if(tb) tb.innerHTML = '<div class="empty-state">No post-exit data yet. Tracks prices for 20 days after each trade closes.</div>';
      const ti = $('#tracker-insights'); if(ti) ti.style.display = 'none';
      const ts = $('#tracker-summary'); if(ts) ts.style.display = 'none';
      return;
    }

    // KPIs
    const el_a = $('#tracker-kpi-active'); if(el_a) el_a.textContent = d.active_count;
    const el_c = $('#tracker-kpi-completed'); if(el_c) el_c.textContent = d.completed_count;

    const byDet = (d.summary && d.summary.by_detector) || {};
    const detKeys = Object.keys(byDet);

    // Portfolio-wide avg left on table & exit-too-early
    if (detKeys.length > 0) {
      let allLot = [], allEarly = [];
      detKeys.forEach(k => {
        if (byDet[k].avg_left_on_table != null) allLot.push(byDet[k].avg_left_on_table);
        if (byDet[k].exit_too_early_pct != null) allEarly.push(byDet[k].exit_too_early_pct);
      });
      const avgLot = allLot.length ? allLot.reduce((a,b) => a+b, 0) / allLot.length : null;
      const avgEarly = allEarly.length ? allEarly.reduce((a,b) => a+b, 0) / allEarly.length : null;
      const el_lot = $('#tracker-kpi-lot'); if(el_lot) el_lot.textContent = avgLot != null ? (avgLot * 100).toFixed(1) + '%' : '--';
      const el_early = $('#tracker-kpi-early'); if(el_early) el_early.textContent = avgEarly != null ? (avgEarly * 100).toFixed(0) + '%' : '--';
      if (avgLot != null) {
        const el_lot2 = $('#tracker-kpi-lot');
        if(el_lot2) el_lot2.className = avgLot > 0.03 ? 'lot-large' : avgLot > 0.015 ? 'lot-medium' : avgLot > 0.005 ? 'lot-small' : avgLot < 0 ? 'pos' : '';
      }
    } else {
      const el_lot = $('#tracker-kpi-lot'); if(el_lot) el_lot.textContent = '--';
      const el_early = $('#tracker-kpi-early'); if(el_early) el_early.textContent = '--';
    }

    // Insights
    const insights = (d.summary && d.summary.insights) || [];
    const ti = $('#tracker-insights');
    if (ti) {
      if (insights.length > 0) {
        let ihtml = '<div style="font-size:0.8rem;color:var(--amber);margin-bottom:0.5rem;font-weight:600">⚡ Insights</div>';
        insights.forEach(i => { ihtml += '<div style="font-size:0.82rem;color:var(--dim);margin-bottom:0.3rem">&bull; ' + i + '</div>'; });
        ti.innerHTML = ihtml;
        ti.style.display = 'block';
      } else {
        ti.style.display = 'none';
      }
    }

    // Summary by detector
    const ts = $('#tracker-summary');
    const tsb = $('#tracker-summary-body');
    if (ts && tsb && detKeys.length > 0) {
      let html = '<div style="overflow-x:auto"><table><thead><tr>'
        + '<th>Detector</th><th>Completed</th><th>Avg Exit Return</th>'
        + '<th>Avg Max Post-Exit</th><th>Avg Left on Table</th>'
        + '<th>Exited Too Early</th></tr></thead><tbody>';
      detKeys.forEach(det => {
        const s = byDet[det];
        const lotClass = s.avg_left_on_table != null
          ? (s.avg_left_on_table > 0.03 ? 'lot-large' : s.avg_left_on_table > 0.015 ? 'lot-medium' : s.avg_left_on_table > 0.005 ? 'lot-small' : s.avg_left_on_table < 0 ? 'pos' : '')
          : '';
        html += '<tr>';
        html += '<td><span class="detector-tag">' + det + '</span></td>';
        html += '<td>' + s.count + '</td>';
        html += '<td class="' + color(s.avg_exit_return) + '">' + (s.avg_exit_return * 100).toFixed(1) + '%</td>';
        html += '<td>' + (s.avg_max_post_exit != null ? (s.avg_max_post_exit * 100).toFixed(1) + '%' : '--') + '</td>';
        html += '<td class="' + lotClass + '">' + (s.avg_left_on_table != null ? (s.avg_left_on_table * 100).toFixed(1) + '%' : '--') + '</td>';
        html += '<td>' + (s.exit_too_early_pct != null ? (s.exit_too_early_pct * 100).toFixed(0) + '%' : '--') + '</td>';
        html += '</tr>';
      });
      html += '</tbody></table></div>';
      tsb.innerHTML = html;
      ts.style.display = 'block';
    } else if (ts) {
      ts.style.display = 'none';
    }

    _renderTrackerRows();
  } catch(e) {
    console.error('exit tracker load failed', e);
  }
}

function filterTracker(f) {
  _trackerFilter = f;
  ['all','active','completed'].forEach(k => {
    const btn = $('#tf-' + k);
    if(btn) btn.classList.toggle('active-filter', k === f);
  });
  _renderTrackerRows();
}

function _renderTrackerRows() {
  const tb = $('#tracker-body');
  if (!tb || !_trackerData) return;
  const d = _trackerData;

  let items;
  if (_trackerFilter === 'active')    items = d.active;
  else if (_trackerFilter === 'completed') items = d.completed;
  else items = d.active.concat(d.completed);

  if (items.length === 0) {
    tb.innerHTML = '<div class="empty-state">No entries match this filter.</div>';
    return;
  }

  let html = '<div style="overflow-x:auto"><table><thead><tr>'
    + '<th>Status</th><th>Ticker</th><th>Detector</th><th>Exit Reason</th>'
    + '<th>Exit Return</th><th>Days</th><th>Max After</th>'
    + '<th>Left on Table</th><th>Headline</th>'
    + '</tr></thead><tbody>';

  items.forEach(t => {
    const lotClass = t.left_on_table != null
      ? (t.left_on_table > 0.03 ? 'lot-large' : t.left_on_table > 0.015 ? 'lot-medium' : t.left_on_table > 0.005 ? 'lot-small' : t.left_on_table < 0 ? 'pos' : '')
      : '';
    const statusBadge = t.tracking_complete
      ? '<span style="font-size:0.7rem;color:var(--dim)">done</span>'
      : '<span style="font-size:0.7rem;color:var(--amber)">● live</span>';
    const headline = (t.headline || '').replace(/</g,'&lt;').substring(0, 60);
    html += '<tr>';
    html += '<td>' + statusBadge + '</td>';
    html += '<td class="ticker">' + t.ticker + '</td>';
    html += '<td><span class="detector-tag">' + t.detector + '</span></td>';
    html += '<td><span class="exit-tag exit-' + t.exit_reason + '">' + t.exit_reason + '</span></td>';
    html += '<td class="' + color(t.pct_return) + '">' + (t.pct_return * 100).toFixed(1) + '%</td>';
    html += '<td>' + t.days_tracked + '/20</td>';
    html += '<td>' + (t.max_post_exit_return != null ? (t.max_post_exit_return >= 0 ? '+' : '') + (t.max_post_exit_return * 100).toFixed(1) + '%' : '--') + '</td>';
    html += '<td class="' + lotClass + '">' + (t.left_on_table != null ? (t.left_on_table >= 0 ? '+' : '') + (t.left_on_table * 100).toFixed(1) + '%' : '--') + '</td>';
    html += '<td style="font-size:0.78rem;color:var(--dim);max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="' + headline + '">' + headline + '</td>';
    html += '</tr>';
  });

  html += '</tbody></table></div>';
  tb.innerHTML = html;
}

async function loadSkippedSignals() {
  try {
    const r = await fetch('/api/skipped-signals');
    const d = await r.json();
    let total = d.active_count + d.completed_count;
    $('#skipped-count').textContent = total;

    if (total === 0) {
      $('#skipped-body').innerHTML = '<div class="empty-state">No skipped signals tracked yet. Signals skipped due to max-positions or insufficient cash will appear here with simulated outcomes.</div>';
      $('#skipped-summary').style.display = 'none';
      return;
    }

    let s = d.summary || {};
    if (s.completed_count > 0) {
      let wr = (s.would_be_win_rate * 100).toFixed(0);
      let ar = (s.would_be_avg_return * 100).toFixed(2);
      let cls = s.would_be_avg_return >= 0 ? 'pos' : 'neg';
      $('#skipped-summary').innerHTML =
        '<b>Would-have-been:</b> ' + s.completed_count + ' simulated trades, ' +
        wr + '% win rate, avg return <span class="' + cls + '">' + ar + '%</span>' +
        ' &mdash; <span style="color:var(--dim)">' + d.active_count + ' still tracking</span>';
      $('#skipped-summary').style.display = 'block';
    } else {
      $('#skipped-summary').innerHTML =
        '<span style="color:var(--dim)">' + d.active_count + ' signals tracking, none completed yet</span>';
      $('#skipped-summary').style.display = 'block';
    }

    let items = d.completed.concat(d.active).slice(0, 30);
    let html = '<table><thead><tr><th>Ticker</th><th>Detector</th><th>Skip Reason</th><th>Sev</th><th>Entry</th><th>Sim Return</th><th>Status</th><th>Headline</th></tr></thead><tbody>';
    items.forEach(t => {
      let ret = t.tracking_complete ? t.simulated_pct_return : t.max_pct_return;
      let cls = ret == null ? '' : (ret > 0 ? 'pos' : ret < 0 ? 'neg' : '');
      let retStr = ret == null ? '--' : (ret * 100).toFixed(1) + '%';
      let status = t.tracking_complete
        ? '<span class="exit-tag exit-' + (t.simulated_exit_reason || 'hold') + '">' + (t.simulated_exit_reason || '?') + '</span>'
        : '<span style="color:var(--dim)">tracking ' + t.days_tracked + '/' + t.hold_days + 'd</span>';
      html += '<tr>';
      html += '<td class="ticker">' + t.ticker + '</td>';
      html += '<td><span class="detector-tag">' + t.detector + '</span></td>';
      html += '<td><span style="color:var(--dim);font-size:0.75rem">' + t.skip_reason + '</span></td>';
      html += '<td>' + t.severity.toFixed(2) + '</td>';
      html += '<td>$' + t.would_be_entry_price.toFixed(2) + '</td>';
      html += '<td class="' + cls + '">' + retStr + '</td>';
      html += '<td>' + status + '</td>';
      html += '<td class="headline-text">' + (t.headline || '').slice(0, 50) + '</td>';
      html += '</tr>';
    });
    html += '</tbody></table>';
    $('#skipped-body').innerHTML = html;
  } catch(e) {
    console.error('skipped signals load failed', e);
  }
}

function getMarketConfig() {
  // Dynamically resolve US DST (EDT/EST) and UK DST (BST/GMT)
  const now = new Date();
  const yr = now.getUTCFullYear();

  function nthSundayUTC(year, month, n) {
    // month: 0-indexed. Returns UTC midnight of the nth Sunday.
    const d = new Date(Date.UTC(year, month, 1));
    const first = d.getUTCDay() === 0 ? 1 : 8 - d.getUTCDay();
    return new Date(Date.UTC(year, month, first + (n - 1) * 7));
  }
  function lastSundayUTC(year, month) {
    const d = new Date(Date.UTC(year, month + 1, 0));
    d.setUTCDate(d.getUTCDate() - d.getUTCDay());
    return d;
  }

  // US EDT: 2nd Sunday March -> 1st Sunday November
  const isEDT = now >= nthSundayUTC(yr, 2, 2) && now < nthSundayUTC(yr, 10, 1);
  // UK BST: last Sunday March -> last Sunday October
  const isBST = now >= lastSundayUTC(yr, 2) && now < lastSundayUTC(yr, 9);

  const openUTC  = isEDT ? 13 * 60 + 30 : 14 * 60 + 30;  // 9:30 AM ET in UTC
  const closeUTC = isEDT ? 20 * 60       : 21 * 60;        // 4:00 PM ET in UTC
  const tzOffset = isBST ? 60 : 0;  // minutes ahead of UTC
  const tzLabel  = isBST ? 'BST' : 'GMT';

  function minsToStr(m) {
    return String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');
  }

  return {
    openUTC, closeUTC,
    displayOpen:  minsToStr(openUTC  + tzOffset),
    displayClose: minsToStr(closeUTC + tzOffset),
    tzLabel,
  };
}

function updateMarketClock() {
  const now = new Date();
  const cfg = getMarketConfig();
  const mins = now.getUTCHours() * 60 + now.getUTCMinutes();
  const day = now.getUTCDay();
  const isWeekday = day >= 1 && day <= 5;
  const isOpen = isWeekday && mins >= cfg.openUTC && mins < cfg.closeUTC;

  const indicator = $('#market-indicator');
  const status    = $('#market-status');
  const countdown = $('#market-countdown');

  // Update displayed open/close times with correct TZ label
  document.querySelectorAll('#market-clock b').forEach((el, i) => {
    el.textContent = i === 0
      ? cfg.displayOpen  + ' ' + cfg.tzLabel
      : cfg.displayClose + ' ' + cfg.tzLabel;
  });

  if (isOpen) {
    indicator.style.background = 'var(--green)';
    status.textContent = 'Market Open'; status.style.color = 'var(--green)';
    const rem = cfg.closeUTC - mins;
    countdown.textContent = 'Closes in ' + Math.floor(rem / 60) + 'h ' + (rem % 60) + 'm';
    countdown.style.color = 'var(--green)';
  } else {
    indicator.style.background = 'var(--red)';
    status.textContent = 'Market Closed'; status.style.color = 'var(--red)';
    const target = new Date(now);
    target.setUTCHours(Math.floor(cfg.openUTC / 60), cfg.openUTC % 60, 0, 0);
    if (isWeekday && mins >= cfg.closeUTC) target.setUTCDate(target.getUTCDate() + (day === 5 ? 3 : 1));
    else if (day === 6) target.setUTCDate(target.getUTCDate() + 2);
    else if (day === 0) target.setUTCDate(target.getUTCDate() + 1);
    const diff = Math.max(0, Math.floor((target - now) / 1000));
    const parts = [];
    if (Math.floor(diff / 3600) > 0) parts.push(Math.floor(diff / 3600) + 'h');
    parts.push(Math.floor((diff % 3600) / 60) + 'm ' + (diff % 60) + 's');
    countdown.textContent = 'Opens in ' + parts.join(' ');
    countdown.style.color = 'var(--amber)';
  }
}
updateMarketClock();
setInterval(updateMarketClock, 1000);

async function loadReview() {
  try {
    const r = await fetch('/api/review');
    const d = await r.json();

    $('#review-trade-count').textContent = d.trade_count + ' trades';

    // Milestone chips
    let mhtml = '';
    d.milestones.forEach(m => {
      let cls = m.reached ? 'reached' : 'pending';
      let icon = m.reached ? '✓' : '○';
      mhtml += '<span class="milestone-chip ' + cls + '">' + icon + ' ' + m.target + ' trades</span>';
    });
    $('#review-milestones').innerHTML = mhtml;

    // Insights
    if (d.insights && d.insights.length > 0) {
      let ihtml = '';
      d.insights.forEach(i => {
        let cls = i.includes('strong performer') ? 'insight-good'
                : (i.includes('below 55%') || i.includes('left on table') || i.includes('recovered')) ? 'insight-warn'
                : 'insight-info';
        ihtml += '<div class="' + cls + '">&bull; ' + i + '</div>';
      });
      if (d.last_review_sent_dt) {
        ihtml += '<div style="font-size:0.72rem;color:var(--dim);margin-top:0.5rem">Last Telegram digest: ' + d.last_review_sent_dt + '</div>';
      }
      $('#review-insights').innerHTML = ihtml;
      $('#review-insights').style.display = 'block';
    } else {
      $('#review-insights').style.display = 'none';
    }

    // Per-detector table
    let dets = Object.entries(d.detector_stats);
    if (dets.length === 0) {
      $('#review-detector-body').innerHTML = '<div class="empty-state">Accumulating trades — insights appear after 10+ trades per detector.</div>';
      return;
    }
    let html = '<table><thead><tr><th>Detector</th><th>Trades</th><th>Win Rate</th><th>Avg Return</th><th>Total P&L</th></tr></thead><tbody>';
    dets.forEach(([det, s]) => {
      let wrCls = s.win_rate >= 0.70 ? 'pos' : s.win_rate < 0.55 ? 'neg' : '';
      let retCls = color(s.avg_return);
      let pnlCls = color(s.total_pnl);
      html += '<tr>';
      html += '<td><span class="detector-tag">' + det + '</span></td>';
      html += '<td>' + s.trades + '</td>';
      html += '<td class="' + wrCls + '">' + (s.win_rate * 100).toFixed(0) + '%</td>';
      html += '<td class="' + retCls + '">' + (s.avg_return * 100).toFixed(1) + '%</td>';
      html += '<td class="' + pnlCls + '">' + fmt(s.total_pnl) + '</td>';
      html += '</tr>';
    });
    html += '</tbody></table>';
    $('#review-detector-body').innerHTML = html;
  } catch(e) {
    console.error('review load failed', e);
  }
}

let _activeTab = 'overview';

function switchTab(name) {
  ['overview', 'postexit', 'analytics', 't212', 'uk', 'reports'].forEach(t => {
    document.getElementById('tab-' + t).style.display = t === name ? 'block' : 'none';
    document.getElementById('tab-btn-' + t).classList.toggle('active', t === name);
  });
  _activeTab = name;
  if (name === 'analytics') {
    loadAnalytics();
    loadReview();
    loadSkippedSignals();
  } else if (name === 'postexit') {
    loadExitTracker();
  } else if (name === 't212') {
    loadT212();
  } else if (name === 'uk') {
    loadUK();
  } else if (name === 'reports') {
    loadReports();
  }
}

async function loadT212() {
  try {
    const r = await fetch('/api/t212');
    const d = await r.json();

    if (!d.available) {
      $('#t212-unavailable').style.display = 'block';
      $('#t212-kpis').style.display = 'none';
      return;
    }
    $('#t212-unavailable').style.display = 'none';
    $('#t212-kpis').style.display = 'flex';

    const fmt = v => v == null ? '--' : '$' + v.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    const pnlFmt = v => v == null ? '--' : (v >= 0 ? '+' : '') + '$' + Math.abs(v).toFixed(2);
    const pnlCls = v => v == null ? '' : v >= 0 ? 'pos' : 'neg';

    $('#t212-free').textContent = fmt(d.free_cash);
    $('#t212-invested').textContent = fmt(d.invested);
    $('#t212-total').textContent = fmt(d.total);
    $('#t212-pnl').innerHTML = '<span class="' + pnlCls(d.total_pnl) + '">' + pnlFmt(d.total_pnl) + '</span>';
    $('#t212-trades').textContent = d.trade_count;
    $('#t212-wr').textContent = d.trade_count ? d.win_rate.toFixed(0) + '%' : '--';
    $('#t212-trade-count').textContent = d.trade_count;
    $('#t212-pos-count').textContent = d.open_count;
    if (d.last_scan_dt) $('#t212-stamp').textContent = 'Last scan: ' + d.last_scan_dt.slice(0,16) + ' UTC';

    // Positions
    if (!d.positions || d.positions.length === 0) {
      $('#t212-positions-body').innerHTML = '<div class="empty-state">No open positions</div>';
    } else {
      let html = '<table><thead><tr><th>Ticker</th><th>Detector</th><th>Entry</th><th>Shares</th><th>Value</th><th>Day</th><th>Headline</th></tr></thead><tbody>';
      d.positions.forEach(p => {
        html += '<tr>'
          + '<td><strong>' + p.ticker + '</strong></td>'
          + '<td><span class="badge">' + p.detector + '</span></td>'
          + '<td>$' + p.entry_price.toFixed(2) + '</td>'
          + '<td>' + p.shares.toFixed(4) + '</td>'
          + '<td>$' + p.cost_basis.toFixed(2) + '</td>'
          + '<td>' + p.days_held + '/' + p.hold_days + '</td>'
          + '<td style="font-size:0.78rem;color:var(--dim);max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="' + p.headline + '">' + p.headline + '</td>'
          + '</tr>';
      });
      html += '</tbody></table>';
      $('#t212-positions-body').innerHTML = html;
    }

    // Trades
    if (!d.trades || d.trades.length === 0) {
      $('#t212-trades-body').innerHTML = '<div class="empty-state">No trades yet</div>';
    } else {
      let html = '<table><thead><tr><th>Ticker</th><th>Detector</th><th>Entry</th><th>Exit</th><th>Return</th><th>P&L</th><th>Reason</th><th>Date</th></tr></thead><tbody>';
      d.trades.forEach(t => {
        const cls = t.pct_return >= 0 ? 'pos' : 'neg';
        html += '<tr>'
          + '<td><strong>' + t.ticker + '</strong></td>'
          + '<td><span class="badge">' + t.detector + '</span></td>'
          + '<td>$' + t.entry_price.toFixed(2) + '</td>'
          + '<td>$' + t.exit_price.toFixed(2) + '</td>'
          + '<td class="' + cls + '">' + (t.pct_return * 100).toFixed(2) + '%</td>'
          + '<td class="' + cls + '">' + pnlFmt(t.pnl) + '</td>'
          + '<td>' + t.exit_reason + '</td>'
          + '<td style="font-size:0.78rem;color:var(--dim)">' + (t.exit_dt || '').slice(0,10) + '</td>'
          + '</tr>';
      });
      html += '</tbody></table>';
      $('#t212-trades-body').innerHTML = html;
    }

    // Slippage comparison
    if (!d.comparison || d.comparison.length === 0) {
      $('#t212-comparison-body').innerHTML = '<div class="empty-state">Comparison data will appear once both systems have traded the same signal.</div>';
    } else {
      let html = '<table><thead><tr><th>Ticker</th><th>Date</th><th>Paper Return</th><th>T212 Return</th><th>Slippage</th></tr></thead><tbody>';
      d.comparison.forEach(c => {
        const diff = c.slippage;
        const cls = diff >= 0 ? 'pos' : 'neg';
        html += '<tr>'
          + '<td><strong>' + c.ticker + '</strong></td>'
          + '<td style="font-size:0.78rem;color:var(--dim)">' + c.entry_dt + '</td>'
          + '<td>' + (c.paper_return * 100).toFixed(2) + '%</td>'
          + '<td>' + (c.t212_return * 100).toFixed(2) + '%</td>'
          + '<td class="' + cls + '">' + (diff >= 0 ? '+' : '') + (diff * 100).toFixed(2) + '%</td>'
          + '</tr>';
      });
      html += '</tbody></table>';
      $('#t212-comparison-body').innerHTML = html;
    }

  } catch(e) {
    console.error('t212 load failed', e);
  }
}

async function loadUK() {
  try {
    const r = await fetch('/api/uk');
    const d = await r.json();

    if (!d.available) {
      $('#uk-unavailable').style.display = 'block';
      $('#uk-kpis').style.display = 'none';
      return;
    }
    $('#uk-unavailable').style.display = 'none';
    $('#uk-kpis').style.display = 'flex';

    const fmt = v => v == null ? '--' : '£' + v.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    const pnlFmt = v => v == null ? '--' : (v >= 0 ? '+' : '') + '£' + Math.abs(v).toFixed(2);
    const pnlCls = v => v == null ? '' : v >= 0 ? 'pos' : 'neg';
    const priceFmt = v => v == null ? '--' : v < 10 ? '£' + v.toFixed(4) : '£' + v.toFixed(2);

    $('#uk-cash').textContent = fmt(d.cash);
    $('#uk-invested').textContent = fmt(d.invested);
    $('#uk-total').textContent = fmt(d.total);
    $('#uk-pnl').innerHTML = '<span class="' + pnlCls(d.total_pnl) + '">' + pnlFmt(d.total_pnl) + '</span>';
    $('#uk-trades').textContent = d.trade_count;
    $('#uk-wr').textContent = d.trade_count ? d.win_rate.toFixed(0) + '%' : '--';
    $('#uk-trade-count').textContent = d.trade_count;
    $('#uk-pos-count').textContent = d.open_count;
    if (d.last_scan_dt) $('#uk-stamp').textContent = 'Last scan: ' + d.last_scan_dt.slice(0,16) + ' UTC';

    // Positions
    if (!d.positions || d.positions.length === 0) {
      $('#uk-positions-body').innerHTML = '<div class="empty-state">No open positions — LSE signals will appear here after 08:00 London time</div>';
    } else {
      let html = '<table><thead><tr><th>Ticker</th><th>Detector</th><th>Entry</th><th>Current</th><th>Return</th><th>Day</th><th>Headline</th></tr></thead><tbody>';
      d.positions.forEach(p => {
        const retCls = p.pct_change == null ? '' : p.pct_change >= 0 ? 'pos' : 'neg';
        const retStr = p.pct_change == null ? '--' : (p.pct_change * 100).toFixed(2) + '%';
        html += '<tr>'
          + '<td><strong>' + p.ticker.replace('.L','') + '</strong><span style="color:var(--dim);font-size:0.75rem">.L</span></td>'
          + '<td><span class="badge">' + p.detector + '</span></td>'
          + '<td>' + priceFmt(p.entry_price) + '</td>'
          + '<td>' + (p.current_price ? priceFmt(p.current_price) : '--') + '</td>'
          + '<td class="' + retCls + '">' + retStr + '</td>'
          + '<td>' + p.days_held + '/' + p.hold_days + '</td>'
          + '<td style="font-size:0.78rem;color:var(--dim);max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="' + p.headline + '">' + p.headline + '</td>'
          + '</tr>';
      });
      html += '</tbody></table>';
      $('#uk-positions-body').innerHTML = html;
    }

    // Trades
    if (!d.trades || d.trades.length === 0) {
      $('#uk-trades-body').innerHTML = '<div class="empty-state">No trades yet</div>';
    } else {
      let html = '<table><thead><tr><th>Ticker</th><th>Detector</th><th>Entry</th><th>Exit</th><th>Return</th><th>P&L</th><th>Reason</th><th>Date</th></tr></thead><tbody>';
      d.trades.forEach(t => {
        const cls = t.pct_return >= 0 ? 'pos' : 'neg';
        html += '<tr>'
          + '<td><strong>' + t.ticker.replace('.L','') + '</strong></td>'
          + '<td><span class="badge">' + t.detector + '</span></td>'
          + '<td>' + priceFmt(t.entry_price) + '</td>'
          + '<td>' + priceFmt(t.exit_price) + '</td>'
          + '<td class="' + cls + '">' + (t.pct_return * 100).toFixed(2) + '%</td>'
          + '<td class="' + cls + '">' + pnlFmt(t.pnl) + '</td>'
          + '<td><span class="badge">' + t.exit_reason + '</span></td>'
          + '<td style="color:var(--dim);font-size:0.8rem">' + (t.exit_dt || '').slice(0,10) + '</td>'
          + '</tr>';
      });
      html += '</tbody></table>';
      $('#uk-trades-body').innerHTML = html;
    }

  } catch(e) {
    console.error('uk load failed', e);
  }
}

async function loadReports() {
  try {
    const r = await fetch('/api/weekly-reports');
    const d = await r.json();
    $('#reports-count').textContent = d.count;

    if (!d.reports || d.reports.length === 0) {
      $('#reports-list').innerHTML = '<div class="empty-state">No reports yet — first one sends Saturday morning. Run <code>switching weekly-report</code> to generate now.</div>';
      return;
    }

    // Summary cards — one per report, clickable to expand detail
    let html = '<div style="display:flex;flex-direction:column;gap:0.75rem;padding:1rem 1.2rem">';
    d.reports.forEach((rep, idx) => {
      const p = rep.paper || {};
      const allTime = p.all_time || {};
      const week = p.this_week || {};
      const wr = allTime.win_rate != null ? (allTime.win_rate * 100).toFixed(0) + '%' : '--';
      const weekWR = week.win_rate != null ? (week.win_rate * 100).toFixed(0) + '%' : '--';
      const pnlCls = (allTime.total_pnl || 0) >= 0 ? 'pos' : 'neg';
      const weekPnlCls = (week.total_pnl || 0) >= 0 ? 'pos' : 'neg';
      const best = rep.paper && rep.paper.best_trade && rep.paper.best_trade.ticker
        ? rep.paper.best_trade : null;
      html += `<div style="border:1px solid var(--border);border-radius:6px;padding:0.9rem 1rem;cursor:pointer;transition:background 0.15s" onmouseover="this.style.background='var(--panel-bg)'" onmouseout="this.style.background=''" onclick="showReport(${idx})">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:0.5rem">
          <div>
            <span style="font-weight:700;font-size:1rem">Week of ${rep.week_label || rep.week_start}</span>
            <span style="font-size:0.72rem;color:var(--dim);margin-left:0.6rem">${(rep.generated_at || '').slice(0,10)}</span>
          </div>
          <div style="display:flex;gap:1.5rem;font-size:0.85rem">
            <span>This week: <b>${rep.paper && rep.paper.this_week_count != null ? rep.paper.this_week_count : '–'} trades</b>
              <span class="${weekPnlCls}"> ${ (week.total_pnl||0) >= 0 ? '+' : '' }${ (week.total_pnl||0).toFixed(2) }</span>
              @ <b>${weekWR}</b> WR</span>
            <span>All-time: <b>${allTime.count || 0}</b> trades
              <span class="${pnlCls}"> ${ (allTime.total_pnl||0) >= 0 ? '+' : '' }${ (allTime.total_pnl||0).toFixed(2) }</span>
              @ <b>${wr}</b> WR</span>
          </div>
        </div>
        ${ best ? `<div style="font-size:0.75rem;color:var(--dim);margin-top:0.3rem">Best: ${best.ticker} ${(best.pct_return*100).toFixed(1)}% (${best.detector})</div>` : '' }
        ${ rep.suggestions && rep.suggestions.length ? `<div style="font-size:0.73rem;color:var(--dim);margin-top:0.25rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${rep.suggestions[0].replace(/<[^>]+>/g,'')}</div>` : '' }
      </div>`;
    });
    html += '</div>';
    $('#reports-list').innerHTML = html;

    // Store reports globally for detail view
    window._weeklyReports = d.reports;
  } catch(e) {
    console.error('reports load failed', e);
  }
}

function showReport(idx) {
  const rep = (window._weeklyReports || [])[idx];
  if (!rep) return;
  const p = rep.paper || {};
  const allTime = p.all_time || {};
  const week = p.this_week || {};
  const t212 = rep.t212 || {};
  const uk = rep.uk || {};

  let html = `<div style="padding:1rem 1.2rem">`;

  // KPI row
  html += `<div style="display:flex;gap:2rem;flex-wrap:wrap;border-bottom:1px solid var(--border);padding-bottom:1rem;margin-bottom:1rem">
    <div><div class="kpi-label">Week</div><div class="kpi-val">${rep.week_label}</div></div>
    <div><div class="kpi-label">This Week Trades</div><div class="kpi-val">${p.this_week_count ?? '–'}</div></div>
    <div><div class="kpi-label">Week P&L</div><div class="kpi-val ${(week.total_pnl||0)>=0?'pos':'neg'}">${(week.total_pnl||0)>=0?'+':''}$${(week.total_pnl||0).toFixed(2)}</div></div>
    <div><div class="kpi-label">All-Time WR</div><div class="kpi-val">${allTime.win_rate!=null?(allTime.win_rate*100).toFixed(0)+'%':'–'}</div></div>
    <div><div class="kpi-label">All-Time P&L</div><div class="kpi-val ${(allTime.total_pnl||0)>=0?'pos':'neg'}">${(allTime.total_pnl||0)>=0?'+':''}$${(allTime.total_pnl||0).toFixed(2)}</div></div>
    <div><div class="kpi-label">Portfolio Value</div><div class="kpi-val">$${(p.total_value||0).toFixed(2)}</div></div>
  </div>`;

  // Detector rankings table
  const rows = (rep.detector_rankings || []).filter(r => r.count > 0);
  if (rows.length) {
    html += `<h3 style="margin:0 0 0.5rem">Detector Rankings</h3>
    <div style="overflow-x:auto;margin-bottom:1.2rem"><table><thead><tr>
      <th>Detector</th><th>Trades</th><th>Win Rate</th><th>Avg Return</th><th>P&amp;L</th>
    </tr></thead><tbody>`;
    rows.forEach(r => {
      const wr = r.win_rate * 100;
      const wrCls = wr >= 65 ? 'pos' : wr < 55 ? 'neg' : '';
      html += `<tr>
        <td><b>${r.detector}</b></td>
        <td>${r.count}</td>
        <td class="${wrCls}">${wr.toFixed(0)}%</td>
        <td class="${r.avg_return>=0?'pos':'neg'}">${(r.avg_return*100).toFixed(2)}%</td>
        <td class="${r.total_pnl>=0?'pos':'neg'}">${r.total_pnl>=0?'+':''}$${r.total_pnl.toFixed(0)}</td>
      </tr>`;
    });
    html += '</tbody></table></div>';
  }

  // Suggestions
  const sugs = (rep.suggestions || []);
  if (sugs.length) {
    html += `<h3 style="margin:0 0 0.5rem">Suggestions</h3><ul style="margin:0 0 1.2rem;padding-left:1.2rem;font-size:0.85rem">`;
    sugs.forEach(s => { html += `<li style="margin-bottom:0.3rem">${s.replace(/<[^>]+>/g,'')}</li>`; });
    html += '</ul>';
  }

  // T212 slippage
  const slip = rep.t212_slippage || {};
  if (slip.matched_count > 0) {
    html += `<h3 style="margin:0 0 0.5rem">T212 vs Paper Slippage</h3>
    <p style="font-size:0.85rem;margin:0 0 0.5rem">
      ${slip.matched_count} matched trades &nbsp;|&nbsp;
      Avg slippage: <b class="${(slip.avg_slippage||0)>=0?'pos':'neg'}">${((slip.avg_slippage||0)*100).toFixed(3)}%</b> &nbsp;|&nbsp;
      T212 better: ${slip.t212_better_count}/${slip.matched_count}
    </p>`;
    if (slip.top_examples && slip.top_examples.length) {
      html += `<div style="overflow-x:auto;margin-bottom:1.2rem"><table><thead><tr>
        <th>Ticker</th><th>Date</th><th>T212</th><th>Paper</th><th>Slippage</th>
      </tr></thead><tbody>`;
      slip.top_examples.slice(0,5).forEach(m => {
        const sCls = m.slippage >= 0 ? 'pos' : 'neg';
        html += `<tr>
          <td>${m.ticker}</td><td>${m.entry_dt}</td>
          <td>${(m.t212_return*100).toFixed(2)}%</td>
          <td>${(m.paper_return*100).toFixed(2)}%</td>
          <td class="${sCls}">${m.slippage>=0?'+':''}${(m.slippage*100).toFixed(3)}%</td>
        </tr>`;
      });
      html += '</tbody></table></div>';
    }
  }

  // T212 + UK summary
  if (t212.all_time && t212.all_time.count > 0) {
    html += `<p style="font-size:0.82rem;color:var(--dim)">T212: ${t212.all_time.count} trades all-time, ${(t212.all_time.win_rate*100).toFixed(0)}% WR, $${t212.all_time.total_pnl.toFixed(2)} P&L</p>`;
  }
  if (uk.all_time && uk.all_time.count > 0) {
    html += `<p style="font-size:0.82rem;color:var(--dim)">LSE: ${uk.all_time.count} trades all-time, ${(uk.all_time.win_rate*100).toFixed(0)}% WR, £${uk.all_time.total_pnl.toFixed(2)} P&L</p>`;
  }

  html += '</div>';

  $('#report-detail-title').textContent = 'Week of ' + (rep.week_label || rep.week_start);
  $('#report-detail-body').innerHTML = html;
  $('#report-detail').style.display = 'block';
  $('#report-detail').scrollIntoView({ behavior: 'smooth' });
}

async function loadAnalytics() {
  try {
    const r = await fetch('/api/analytics');
    const d = await r.json();

    // ── Exit Profile Tuning table ────────────────────────────────────────
    if (!d.exit_profiles || d.exit_profiles.length === 0) {
      $('#analytics-exit-body').innerHTML = '<div class="empty-state">No trade data yet.</div>';
    } else {
      let html = '<div style="overflow-x:auto"><table><thead><tr>'
        + '<th>Detector</th><th>Trades</th><th>Win Rate</th>'
        + '<th>Avg Return</th><th>Avg Hold Days</th>'
        + '<th>Stop Loss %</th><th>First Green %</th>'
        + '<th>Hold Expiry %</th><th>Peak Trail %</th>'
        + '</tr></thead><tbody>';
      d.exit_profiles.forEach(p => {
        let wrCls = p.win_rate >= 0.70 ? 'pos' : p.win_rate < 0.55 ? 'neg' : '';
        let retCls = p.avg_return > 0 ? 'pos' : p.avg_return < 0 ? 'neg' : '';
        let slCls  = p.pct_stop_loss > 0.40 ? 'neg' : p.pct_stop_loss > 0.25 ? 'lot-small' : '';
        html += '<tr>';
        html += '<td><span class="detector-tag">' + p.detector + '</span></td>';
        html += '<td>' + p.trades + '</td>';
        html += '<td class="' + wrCls + '">' + (p.win_rate * 100).toFixed(0) + '%</td>';
        html += '<td class="' + retCls + '">' + (p.avg_return * 100).toFixed(1) + '%</td>';
        html += '<td>' + p.avg_hold_days.toFixed(1) + 'd</td>';
        html += '<td class="' + slCls + '">' + (p.pct_stop_loss * 100).toFixed(0) + '%</td>';
        html += '<td class="pos">' + (p.pct_first_green * 100).toFixed(0) + '%</td>';
        html += '<td style="color:var(--dim)">' + (p.pct_hold_expiry * 100).toFixed(0) + '%</td>';
        html += '<td style="color:var(--cyan)">' + (p.pct_peak_trailing * 100).toFixed(0) + '%</td>';
        html += '</tr>';
      });
      html += '</tbody></table></div>';
      $('#analytics-exit-body').innerHTML = html;
    }

    // ── Severity buckets table ───────────────────────────────────────────
    if (!d.severity_buckets || d.severity_buckets.length === 0) {
      $('#analytics-sev-body').innerHTML = '<div class="empty-state">No trade data yet. Severity is stored from first trade onwards.</div>';
    } else {
      let html = '<table><thead><tr><th>Severity</th><th>Trades</th><th>Win Rate</th><th>Avg Return</th><th>Signal Strength</th></tr></thead><tbody>';
      d.severity_buckets.forEach(b => {
        let wrCls = b.win_rate >= 0.70 ? 'pos' : b.win_rate < 0.55 ? 'neg' : '';
        let retCls = b.avg_return > 0 ? 'pos' : b.avg_return < 0 ? 'neg' : '';
        let barW = Math.round(b.win_rate * 80);
        html += '<tr>';
        html += '<td style="font-family:monospace">' + b.bucket + '</td>';
        html += '<td>' + b.trades + '</td>';
        html += '<td class="' + wrCls + '">' + (b.win_rate * 100).toFixed(0) + '%</td>';
        html += '<td class="' + retCls + '">' + (b.avg_return * 100).toFixed(1) + '%</td>';
        html += '<td><span class="severity-bar" style="width:' + barW + 'px"></span></td>';
        html += '</tr>';
      });
      html += '</tbody></table>';
      html += '<div style="padding:0.6rem 1rem;font-size:0.75rem;color:var(--dim)">Higher severity = stronger detector regex match. Once 50+ scored trades accumulate, this will show Haiku AI score correlation.</div>';
      $('#analytics-sev-body').innerHTML = html;
    }

    // ── Peak trailing summary ────────────────────────────────────────────
    const pt = d.peak_trailing;
    if (!pt || pt.total === 0) {
      $('#analytics-peak-kpis').style.display = 'none';
      $('#analytics-peak-body').innerHTML = '<div class="empty-state">No peak trailing trades yet. Positions that hit +8% on day-0 will appear here.</div>';
    } else {
      let kpiHtml = '';
      const kpis = [
        { label: 'Total triggered', value: pt.total },
        { label: 'Avg peak reached', value: pt.avg_peak_pct != null ? (pt.avg_peak_pct * 100).toFixed(1) + '%' : '--', cls: 'pos' },
        { label: 'Avg exit return',  value: pt.avg_exit_pct  != null ? (pt.avg_exit_pct  * 100).toFixed(1) + '%' : '--', cls: pt.avg_exit_pct >= 0 ? 'pos' : 'neg' },
        { label: 'Avg left on table', value: pt.avg_left_on_table != null ? (pt.avg_left_on_table * 100).toFixed(1) + '%' : '--',
          cls: pt.avg_left_on_table > 0.02 ? 'lot-medium' : pt.avg_left_on_table > 0 ? 'lot-small' : 'pos' },
      ];
      kpis.forEach(k => {
        kpiHtml += '<div><div style="font-size:0.72rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em">' + k.label + '</div>'
          + '<div style="font-size:1.4rem;font-weight:700" class="' + (k.cls || '') + '">' + k.value + '</div></div>';
      });
      $('#analytics-peak-kpis').innerHTML = kpiHtml;
      $('#analytics-peak-kpis').style.display = 'flex';

      if (pt.trades && pt.trades.length > 0) {
        let html = '<table><thead><tr><th>Ticker</th><th>Detector</th><th>Entry</th><th>Peak</th><th>Exit</th><th>Peak %</th><th>Exit %</th><th>Left on Table</th><th>Date</th></tr></thead><tbody>';
        pt.trades.forEach(t => {
          let lot = t.peak_pct != null ? t.peak_pct - t.exit_pct : null;
          let lotCls = lot == null ? '' : lot > 0.03 ? 'lot-large' : lot > 0.015 ? 'lot-medium' : lot > 0.005 ? 'lot-small' : 'pos';
          html += '<tr>';
          html += '<td class="ticker">' + t.ticker + '</td>';
          html += '<td><span class="detector-tag">' + t.detector + '</span></td>';
          html += '<td>$' + t.entry_price.toFixed(2) + '</td>';
          html += '<td>' + (t.peak_price > 0 ? '$' + t.peak_price.toFixed(2) : '--') + '</td>';
          html += '<td>$' + t.exit_price.toFixed(2) + '</td>';
          html += '<td class="pos">' + (t.peak_pct != null ? (t.peak_pct * 100).toFixed(1) + '%' : '--') + '</td>';
          html += '<td class="' + (t.exit_pct >= 0 ? 'pos' : 'neg') + '">' + (t.exit_pct * 100).toFixed(1) + '%</td>';
          html += '<td class="' + lotCls + '">' + (lot != null ? (lot * 100).toFixed(1) + '%' : '--') + '</td>';
          html += '<td>' + (t.exit_dt || '').slice(0, 10) + '</td>';
          html += '</tr>';
        });
        html += '</tbody></table>';
        $('#analytics-peak-body').innerHTML = html;
      } else {
        $('#analytics-peak-body').innerHTML = '<div class="empty-state">No individual peak trade details yet.</div>';
      }
    }
  } catch(e) {
    console.error('analytics load failed', e);
  }
}

async function loadOptionsLab() {
  const iv  = parseInt(document.getElementById('options-iv').value) / 100;
  const dte = parseInt(document.getElementById('options-dte').value);
  $('#options-body').innerHTML = '<div class="empty-state">Running Black-Scholes model…</div>';
  $('#options-kpis').style.display = 'none';
  try {
    const r = await fetch('/api/options-compare?iv=' + iv + '&dte=' + dte);
    const d = await r.json();

    if (!d.trade_count) {
      $('#options-body').innerHTML = '<div class="empty-state">No closed trades to compare. Wait for some trades to complete.</div>';
      return;
    }

    // ── KPI cards ─────────────────────────────────────────────────────────
    const stockCol  = d.total_stock_pnl  >= 0 ? 'pos' : 'neg';
    const optCol    = d.total_options_pnl >= 0 ? 'pos' : 'neg';
    const delta     = d.total_options_pnl - d.total_stock_pnl;
    const deltaCol  = delta >= 0 ? 'pos' : 'neg';
    const kpis = [
      { label: 'Stock total P&L',       value: fmt(d.total_stock_pnl),              cls: stockCol },
      { label: 'Options total P&L',     value: fmt(d.total_options_pnl),            cls: optCol },
      { label: 'Δ vs stock',            value: (delta >= 0 ? '+' : '') + fmt(delta, 2), cls: deltaCol },
      { label: 'Stock win rate',        value: (d.stock_win_rate * 100).toFixed(0) + '%' },
      { label: 'Options win rate',      value: (d.options_win_rate * 100).toFixed(0) + '%' },
      { label: 'Options beat stock on', value: d.options_better_count + ' / ' + d.trade_count + ' trades' },
    ];
    let kpiHtml = '';
    kpis.forEach(k => {
      kpiHtml += '<div><div style="font-size:0.72rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em">' + k.label + '</div>'
        + '<div style="font-size:1.25rem;font-weight:700" class="' + (k.cls || '') + '">' + k.value + '</div></div>';
    });
    $('#options-kpis').innerHTML = kpiHtml;
    $('#options-kpis').style.display = 'flex';

    // ── Per-detector table ─────────────────────────────────────────────────
    let html = '<div style="padding:0.5rem 1.2rem;font-size:0.75rem;color:var(--dim)">'
      + 'Assumption: ATM European call, IV=' + (d.assumed_iv * 100).toFixed(0) + '%, DTE=' + d.dte
      + ' d, risk-free 5%, Black-Scholes mid-price. Same dollar amount committed to premium as to stock.'
      + '</div>';
    if (d.by_detector && d.by_detector.length > 0) {
      html += '<div style="overflow-x:auto"><table><thead><tr>'
        + '<th>Detector</th><th>Trades</th>'
        + '<th>Stock P&amp;L</th><th>Options P&amp;L</th><th>Δ P&amp;L</th>'
        + '<th>Stock WR</th><th>Options WR</th>'
        + '</tr></thead><tbody>';
      d.by_detector.forEach(row => {
        const rowDelta   = row.options_pnl - row.stock_pnl;
        const deltaClass = rowDelta > 0 ? 'pos' : rowDelta < 0 ? 'neg' : '';
        const sCls  = row.stock_pnl   >= 0 ? 'pos' : 'neg';
        const oCls  = row.options_pnl >= 0 ? 'pos' : 'neg';
        html += '<tr>';
        html += '<td><span class="detector-tag">' + row.detector + '</span></td>';
        html += '<td>' + row.trades + '</td>';
        html += '<td class="' + sCls + '">' + fmt(row.stock_pnl) + '</td>';
        html += '<td class="' + oCls + '">' + fmt(row.options_pnl) + '</td>';
        html += '<td class="' + deltaClass + '">' + (rowDelta >= 0 ? '+' : '') + fmt(rowDelta, 2) + '</td>';
        html += '<td>' + (row.stock_win_rate * 100).toFixed(0)   + '%</td>';
        html += '<td>' + (row.options_win_rate * 100).toFixed(0) + '%</td>';
        html += '</tr>';
      });
      html += '</tbody></table></div>';
    }
    $('#options-body').innerHTML = html;
  } catch(e) {
    console.error('options lab failed', e);
    $('#options-body').innerHTML = '<div class="empty-state">Error running options model.</div>';
  }
}

function refresh() {
  loadPortfolio();
  loadTrades();
  loadSignals();
  loadCharts();
  loadExitTracker();  // always refresh so tab badge stays current
  if (_activeTab === 'analytics') {
    loadAnalytics();
    loadReview();
    loadSkippedSignals();
  }
  $('#last-update').textContent = 'Updated ' + new Date().toLocaleTimeString();
}

refresh();
setInterval(refresh, 60000);
</script>
</body>
</html>
"""
