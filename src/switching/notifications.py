"""Telegram notifications for paper trading.

Sends trade alerts, daily summaries, and skipped signals to a Telegram
chat via the Bot API. Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
env vars. If not configured, all send functions are silent no-ops.

Buy notifications are batched and sent as a digest every 2 hours to avoid
spam when many positions open in quick succession. Sells and stop-losses
are sent immediately (time-sensitive).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}"
_BATCH_INTERVAL_SECONDS = 2 * 60 * 60  # 2 hours

# Per-process market label prepended to every message so UK/US/T212 alerts are
# visually distinct in the same Telegram chat. Set once at loop start via
# set_market(). Empty by default (no prefix) so unconfigured callers/tests are
# unaffected.
_MARKET_PREFIX = ""
_MARKET_LABELS = {
    "us": "🇺🇸 US",
    "uk": "🇬🇧 LSE",
    "t212": "🇺🇸 T212",
    "t212_uk": "🇬🇧 T212",
}


def set_market(market: str) -> None:
    """Set the market tag prepended to every notification (per process)."""
    global _MARKET_PREFIX
    _MARKET_PREFIX = _MARKET_LABELS.get(market, market.upper())


def _config() -> tuple[str, str] | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return None
    return token, chat_id


def _alert_config() -> tuple[str, str] | None:
    """Config for the SEPARATE ops/alert bot used for 'something has gone wrong'
    alerts. Distinct token (TELEGRAM_ALERT_BOT_TOKEN) so failure alerts come from a
    different sender than routine trade notifications — and still reach you even if
    the MAIN bot's token is what failed. Chat id defaults to the main chat
    (TELEGRAM_CHAT_ID) unless TELEGRAM_ALERT_CHAT_ID overrides it."""
    token = os.environ.get("TELEGRAM_ALERT_BOT_TOKEN", "")
    if not token:
        return None
    chat_id = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "") or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        return None
    return token, chat_id


def _html_to_plain(text: str) -> str:
    """Strip HTML tags and unescape entities for a plain-text fallback send."""
    import html
    import re
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _post_message(token: str, chat_id: str, text: str, parse_mode: str | None) -> tuple[bool, int | None]:
    """POST a single message. Returns (ok, http_status).

    http_status is the HTTP code on an HTTPError (so the caller can react to a
    400), or None for non-HTTP failures (network/timeout).
    """
    body: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        body["parse_mode"] = parse_mode
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{_API_BASE.format(token=token)}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200, resp.status
    except urllib.error.HTTPError as exc:
        log.warning("Telegram send failed: HTTP %s %s", exc.code, exc.reason)
        return False, exc.code
    except Exception as exc:
        log.warning("Telegram send failed: %s", exc)
        return False, None


def _send_via(token: str, chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
    """Send to a specific bot/chat with the market prefix + 400→plain-text fallback."""
    if _MARKET_PREFIX:
        text = f"<b>[{_MARKET_PREFIX}]</b>\n{text}"
    ok, status = _post_message(token, chat_id, text, parse_mode)
    if ok:
        return True
    # HTTP 400 from Telegram in HTML mode almost always means a malformed entity
    # — a bare '&', '<' or '>' in dynamic text (e.g. "P&L", or a headline like
    # "Marks & Spencer raises guidance"). Rather than silently drop the message,
    # degrade to PLAIN TEXT (tags stripped, entities unescaped, no parse_mode) so
    # delivery still succeeds. Release-it: degrade, don't drop.
    if status == 400 and parse_mode:
        log.info("Telegram: retrying as plain text after HTTP 400 (likely bad HTML entity)")
        ok, _ = _post_message(token, chat_id, _html_to_plain(text), parse_mode=None)
        return ok
    return False


def _send(text: str, parse_mode: str = "HTML") -> bool:
    cfg = _config()
    if cfg is None:
        return False
    return _send_via(cfg[0], cfg[1], text, parse_mode)


def notify_text(text: str) -> None:
    """Send an ad-hoc one-off alert (gets the market prefix like everything else)."""
    _send(text)


def notify_alert(text: str) -> None:
    """Critical / 'something has gone wrong' alert — sent from the SEPARATE ops bot
    so it stands out from routine trade notifications and survives the main bot's
    token failing. Falls back to the main bot if the ops bot isn't configured, and
    to a plain log if neither is (degrade, never drop a failure alert silently)."""
    msg = f"🚨 {text}"
    alert = _alert_config()
    if alert is not None and _send_via(alert[0], alert[1], msg):
        return
    _send(msg)   # fall back to the main bot; silent no-op if neither is configured
                 # (callers already log the underlying failure, so we don't re-log here)


# ---------------------------------------------------------------------------
# Buy notification batching
# ---------------------------------------------------------------------------


@dataclass
class _BuyRecord:
    ticker: str
    price: float
    shares: float
    cost: float
    detector: str
    headline: str
    severity: float
    ai_score: float | None = None
    timestamp: str = ""


class _NotificationQueue:
    """Batches buy notifications and flushes every 2 hours."""

    def __init__(self) -> None:
        self._queue: list[_BuyRecord] = []
        self._lock = threading.Lock()
        self._last_flush: float = time.time()
        self._timer: threading.Timer | None = None

    def enqueue_buy(self, record: _BuyRecord) -> None:
        with self._lock:
            self._queue.append(record)
            if self._timer is None:
                self._timer = threading.Timer(_BATCH_INTERVAL_SECONDS, self.flush)
                self._timer.daemon = True
                self._timer.start()

    def flush(self) -> None:
        with self._lock:
            pending = self._queue[:]
            self._queue.clear()
            self._last_flush = time.time()
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

        if not pending:
            return

        if len(pending) == 1:
            b = pending[0]
            ai_line = f"\nAI: <b>{b.ai_score:.2f}</b>" if b.ai_score is not None else ""
            text = (
                f"📈 <b>BUY {b.ticker}</b>\n"
                f"${b.price:.2f} × {b.shares:.4f} = ${b.cost:.2f}\n"
                f"{b.detector} (sev {b.severity:.2f}){ai_line}\n"
                f"<i>{_truncate(b.headline, 100)}</i>"
            )
        else:
            total_cost = sum(b.cost for b in pending)
            lines = [
                f"📈 <b>{len(pending)} new positions opened</b> (${total_cost:.2f} deployed)",
                "",
            ]
            for b in pending:
                ai_tag = f" AI:{b.ai_score:.1f}" if b.ai_score is not None else ""
                lines.append(
                    f"• <b>{b.ticker}</b> ${b.price:.2f} × {b.shares:.2f} = ${b.cost:.2f}"
                    f" — {b.detector}{ai_tag}"
                )
            text = "\n".join(lines)

        _send(text)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def seconds_until_flush(self) -> float:
        elapsed = time.time() - self._last_flush
        return max(0, _BATCH_INTERVAL_SECONDS - elapsed)


_buy_queue = _NotificationQueue()


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
    record = _BuyRecord(
        ticker=ticker,
        price=price,
        shares=shares,
        cost=cost,
        detector=detector,
        headline=headline,
        severity=severity,
        ai_score=ai_score,
        timestamp=datetime.now(tz=timezone.utc).isoformat(),
    )
    _buy_queue.enqueue_buy(record)


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


def notify_rate_limit_warning(endpoint: str) -> None:
    """Send an immediate Telegram alert when EDGAR returns HTTP 429.

    Called before the 2-second backoff so the operator knows the scraper
    is being throttled. If this fires repeatedly, reduce ``rate_limit`` on
    the EdgarClient or increase the scan interval.
    """
    short_url = endpoint[:120]
    text = (
        f"⚠️ <b>EDGAR rate limit hit (429)</b>\n"
        f"<code>{short_url}</code>\n"
        f"Backing off 2 s and retrying. If this repeats, check scan frequency."
    )
    notify_alert(text)


def notify_review_digest(insights: list[str], trade_count: int) -> None:
    lines = [f"📋 <b>Daily Strategy Review</b> ({trade_count} total trades)", ""]
    for insight in insights:
        lines.append(f"• {insight}")
    _send("\n".join(lines))


def is_configured() -> bool:
    return _config() is not None


def flush_buy_queue() -> None:
    """Force-flush any pending buy notifications (call before shutdown or daily summary)."""
    _buy_queue.flush()


def pending_buy_count() -> int:
    """Number of buy notifications waiting in the batch queue."""
    return _buy_queue.pending_count


def _truncate(text: str, length: int) -> str:
    return text[:length] + "…" if len(text) > length else text
