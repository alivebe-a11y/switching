"""Flask web dashboard for paper-trading portfolio."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

from switching.paper_trader import Portfolio

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
            cur_price = _safe_price(pos.ticker)
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

    return app


def _safe_price(ticker: str) -> float | None:
    try:
        from switching.paper_trader import get_current_price
        return get_current_price(ticker)
    except Exception:
        return None


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
.severity-bar {
  display: inline-block; height: 6px; border-radius: 3px;
  background: var(--blue); min-width: 20px;
}
.empty-state { padding: 2rem; text-align: center; color: var(--dim); }
.chart-area {
  padding: 1.2rem; height: 200px; display: flex;
  align-items: flex-end; gap: 2px;
}
.chart-bar {
  flex: 1; background: var(--blue); border-radius: 2px 2px 0 0;
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

  <div class="panel">
    <div class="panel-header">
      <h2>Open Positions</h2>
      <span class="badge" id="pos-count">0</span>
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
</div>

<script>
function $(s) { return document.querySelector(s); }
function fmt(n, d) { return n == null ? '--' : '$' + n.toFixed(d || 2); }
function pct(n) { return n == null ? '--' : (n * 100).toFixed(1) + '%'; }
function color(n) { return n > 0 ? 'pos' : n < 0 ? 'neg' : ''; }

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

function refresh() {
  loadPortfolio();
  loadTrades();
  loadSignals();
  $('#last-update').textContent = 'Updated ' + new Date().toLocaleTimeString();
}

refresh();
setInterval(refresh, 60000);
</script>
</body>
</html>
"""
