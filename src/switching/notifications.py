"""Telegram notifications for paper trading.

Sends trade alerts, daily summaries, and skipped signals to a Telegram
chat via the Bot API. Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
env vars. If not configured, all send functions are silent no-ops.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}"


def _config() -> tuple[str, str] | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return None
    return token, chat_id


def _send(text: str, parse_mode: str = "HTML") -> bool:
    cfg = _config()
    if cfg is None:
        return False
    token, chat_id = cfg
    url = f"{_API_BASE.format(token=token)}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as exc:
        log.warning("Telegram send failed: %s", exc)
        return False


def notify_buy(
    ticker: str,
    price: float,
    shares: float,
    cost: float,
    detector: str,
    headline: str,
    severity: float,
    ai_score: float | None = None,
) -> None:
    ai_line = f"\nAI score: <b>{ai_score:.2f}</b>" if ai_score is not None else ""
    text = (
        f"📈 <b>BUY {ticker}</b>\n"
        f"Price: ${price:.2f} × {shares:.4f} = ${cost:.2f}\n"
        f"Detector: {detector} (severity {severity:.2f}){ai_line}\n"
        f"<i>{_truncate(headline, 120)}</i>"
    )
    _send(text)


def notify_sell(
    ticker: str,
    exit_price: float,
    pnl: float,
    pct_return: float,
    exit_reason: str,
    detector: str,
) -> None:
    if exit_reason == "stop_loss":
        icon = "🔴"
        label = "STOP LOSS"
    elif pnl >= 0:
        icon = "💰"
        label = "SELL"
    else:
        icon = "📉"
        label = "SELL"
    text = (
        f"{icon} <b>{label} {ticker}</b>\n"
        f"Exit: ${exit_price:.2f} ({exit_reason})\n"
        f"P&L: <b>${pnl:+.2f}</b> ({pct_return*100:+.1f}%)\n"
        f"Detector: {detector}"
    )
    _send(text)


def notify_skip(ticker: str, reason: str, detector: str, headline: str) -> None:
    text = (
        f"⏭ <b>SKIP {ticker}</b> — {reason}\n"
        f"Detector: {detector}\n"
        f"<i>{_truncate(headline, 100)}</i>"
    )
    _send(text)


def notify_daily_summary(
    cash: float,
    portfolio_value: float,
    positions: list[dict[str, Any]],
    todays_trades: list[dict[str, Any]],
    total_trades: int,
    total_wins: int,
    total_pnl: float,
) -> None:
    win_rate = (total_wins / total_trades * 100) if total_trades else 0

    lines = [
        f"📊 <b>Daily Summary</b>",
        f"Portfolio: <b>${portfolio_value:.2f}</b> (cash: ${cash:.2f})",
        f"Record: {total_trades} trades, {total_wins} wins ({win_rate:.0f}%)",
        f"Total P&L: <b>${total_pnl:+.2f}</b>",
    ]

    if positions:
        lines.append(f"\n<b>Open positions ({len(positions)}):</b>")
        for p in positions:
            lines.append(f"  {p['ticker']}: ${p['entry_price']:.2f} day {p['days_held']}/{p['hold_days']}")

    if todays_trades:
        day_pnl = sum(t["pnl"] for t in todays_trades)
        lines.append(f"\n<b>Today's trades ({len(todays_trades)}):</b>")
        for t in todays_trades:
            icon = "✅" if t["pnl"] >= 0 else "❌"
            lines.append(f"  {icon} {t['ticker']}: {t['pct_return']*100:+.1f}% (${t['pnl']:+.2f}) — {t['exit_reason']}")
        lines.append(f"Day P&L: <b>${day_pnl:+.2f}</b>")
    else:
        lines.append("\nNo trades today.")

    _send("\n".join(lines))


def notify_startup(
    cash: float,
    portfolio_value: float,
    open_positions: int,
    total_trades: int,
    detectors: list[str],
    scan_interval: int,
) -> None:
    text = (
        f"🚀 <b>Paper Trader Started</b>\n"
        f"Portfolio: ${portfolio_value:.2f} (cash: ${cash:.2f})\n"
        f"Open positions: {open_positions}\n"
        f"History: {total_trades} trades\n"
        f"Detectors: {', '.join(detectors)}\n"
        f"Scan interval: {scan_interval}m"
    )
    _send(text)


def is_configured() -> bool:
    return _config() is not None


def _truncate(text: str, length: int) -> str:
    return text[:length] + "…" if len(text) > length else text
