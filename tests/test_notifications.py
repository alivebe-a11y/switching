"""Tests for Telegram notifications."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import switching.notifications as _notif
from switching.notifications import (
    _send,
    flush_buy_queue,
    is_configured,
    notify_buy,
    notify_sell,
    notify_skip,
    notify_daily_summary,
    notify_startup,
    notify_text,
    pending_buy_count,
    set_market,
)


class TestMarketPrefix:
    def teardown_method(self):
        _notif._MARKET_PREFIX = ""   # don't leak market state across tests

    def _capture(self):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_no_prefix_by_default(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}), \
             patch("urllib.request.urlopen", return_value=self._capture()) as mo:
            _send("hello")
        assert b"[" not in mo.call_args[0][0].data.split(b"hello")[0][-3:]

    def test_uk_prefix_applied(self):
        set_market("uk")
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}), \
             patch("urllib.request.urlopen", return_value=self._capture()) as mo:
            notify_text("hello")
        data = mo.call_args[0][0].data.decode("utf-8")
        assert "LSE" in data and "hello" in data

    def test_us_and_uk_prefixes_differ(self):
        set_market("us")
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}), \
             patch("urllib.request.urlopen", return_value=self._capture()) as mo:
            notify_text("x")
        us = mo.call_args[0][0].data.decode("utf-8")
        set_market("uk")
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}), \
             patch("urllib.request.urlopen", return_value=self._capture()) as mo:
            notify_text("x")
        uk = mo.call_args[0][0].data.decode("utf-8")
        assert us != uk
        assert "US" in us and "LSE" in uk

    def test_t212_label(self):
        set_market("t212")
        assert "T212" in _notif._MARKET_PREFIX


class TestSendHtmlFallback:
    """A bare '&'/'<'/'>' in HTML mode makes Telegram return HTTP 400 and drop
    the message (the 'P&L' weekly-report bug). _send must degrade to plain text
    on a 400 so delivery still succeeds."""

    def teardown_method(self):
        _notif._MARKET_PREFIX = ""

    def _ok_resp(self):
        r = MagicMock()
        r.status = 200
        r.__enter__ = lambda s: s
        r.__exit__ = MagicMock(return_value=False)
        return r

    def test_400_falls_back_to_plain_text(self):
        import urllib.error
        err = urllib.error.HTTPError("u", 400, "Bad Request", {}, None)
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}), \
             patch("urllib.request.urlopen", side_effect=[err, self._ok_resp()]) as mo:
            result = _send("Total <b>P&amp;L</b>: $5")
        assert result is True
        assert mo.call_count == 2          # HTML failed, plain-text retry sent
        second = mo.call_args_list[1][0][0].data.decode("utf-8")
        assert "parse_mode" not in second  # plain text, no HTML parsing
        assert "<b>" not in second         # tags stripped
        assert "P&L" in second             # &amp; unescaped back to &

    def test_non_400_failure_does_not_retry(self):
        import urllib.error
        err = urllib.error.HTTPError("u", 500, "Server Error", {}, None)
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}), \
             patch("urllib.request.urlopen", side_effect=[err]) as mo:
            result = _send("hello")
        assert result is False
        assert mo.call_count == 1


class TestNotifyAlert:
    """Critical/'gone wrong' alerts route to the SEPARATE ops bot, with graceful
    fallback to the main bot if the ops bot isn't configured."""

    def teardown_method(self):
        _notif._MARKET_PREFIX = ""

    def _ok(self):
        r = MagicMock()
        r.status = 200
        r.__enter__ = lambda s: s
        r.__exit__ = MagicMock(return_value=False)
        return r

    def test_uses_ops_bot_when_configured(self):
        env = {"TELEGRAM_BOT_TOKEN": "MAINTOK", "TELEGRAM_CHAT_ID": "1",
               "TELEGRAM_ALERT_BOT_TOKEN": "OPSTOK", "TELEGRAM_ALERT_CHAT_ID": "9"}
        with patch.dict("os.environ", env, clear=True), \
             patch("urllib.request.urlopen", return_value=self._ok()) as mo:
            _notif.notify_alert("breaker tripped")
        req = mo.call_args[0][0]
        assert "botOPSTOK/" in req.full_url        # sent via the OPS bot, not main
        data = req.data.decode("utf-8")
        assert "breaker tripped" in data
        assert '"chat_id": "9"' in data

    def test_falls_back_to_main_bot_when_ops_unset(self):
        env = {"TELEGRAM_BOT_TOKEN": "MAINTOK", "TELEGRAM_CHAT_ID": "1"}
        with patch.dict("os.environ", env, clear=True), \
             patch("urllib.request.urlopen", return_value=self._ok()) as mo:
            _notif.notify_alert("breaker tripped")
        assert "botMAINTOK/" in mo.call_args[0][0].full_url   # degraded to main bot

    def test_ops_chat_id_defaults_to_main_chat(self):
        env = {"TELEGRAM_BOT_TOKEN": "MAINTOK", "TELEGRAM_CHAT_ID": "1",
               "TELEGRAM_ALERT_BOT_TOKEN": "OPSTOK"}   # no alert chat id
        with patch.dict("os.environ", env, clear=True), \
             patch("urllib.request.urlopen", return_value=self._ok()) as mo:
            _notif.notify_alert("x")
        data = mo.call_args[0][0].data.decode("utf-8")
        assert "botOPSTOK/" in mo.call_args[0][0].full_url
        assert '"chat_id": "1"' in data            # defaulted to the main chat


class TestIsConfigured:
    def test_not_configured_without_env(self):
        with patch.dict("os.environ", {}, clear=True):
            assert is_configured() is False

    def test_not_configured_with_token_only(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok"}, clear=True):
            assert is_configured() is False

    def test_configured_with_both(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"}):
            assert is_configured() is True


class TestSend:
    def test_noop_without_config(self):
        with patch.dict("os.environ", {}, clear=True):
            assert _send("test") is False

    def test_sends_via_urllib(self):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"}), \
             patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            result = _send("hello")

        assert result is True
        call_args = mock_open.call_args
        req = call_args[0][0]
        assert "tok" in req.full_url
        assert b"hello" in req.data

    def test_handles_network_error(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"}), \
             patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            result = _send("hello")
        assert result is False


class TestNotifyBuy:
    def setup_method(self):
        flush_buy_queue()

    def teardown_method(self):
        flush_buy_queue()

    def test_buy_is_queued_not_sent_immediately(self):
        with patch("switching.notifications._send") as mock:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}):
                notify_buy("AAPL", 150.0, 1.3333, 200.0, "ai_pivot", "Apple AI pivot", 0.8)
        mock.assert_not_called()
        assert pending_buy_count() == 1

    def test_flush_sends_single_buy(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}):
            notify_buy("AAPL", 150.0, 1.3333, 200.0, "ai_pivot", "Apple AI pivot", 0.8)
            with patch("switching.notifications._send") as mock:
                flush_buy_queue()
        mock.assert_called_once()
        text = mock.call_args[0][0]
        assert "BUY AAPL" in text
        assert "$150.00" in text
        assert "ai_pivot" in text

    def test_flush_batches_multiple_buys(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}):
            notify_buy("AAPL", 150.0, 1.0, 150.0, "ai_pivot", "h1", 0.8)
            notify_buy("MSFT", 300.0, 0.5, 150.0, "buyback", "h2", 0.7)
            notify_buy("NVDA", 500.0, 0.3, 150.0, "ai_pivot", "h3", 0.9)
            with patch("switching.notifications._send") as mock:
                flush_buy_queue()
        mock.assert_called_once()
        text = mock.call_args[0][0]
        assert "3 new positions" in text
        assert "AAPL" in text and "MSFT" in text and "NVDA" in text

    def test_includes_ai_score(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}):
            notify_buy("AAPL", 150.0, 1.0, 150.0, "ai_pivot", "headline", 0.8, ai_score=0.92)
            with patch("switching.notifications._send") as mock:
                flush_buy_queue()
        text = mock.call_args[0][0]
        assert "0.92" in text

    def test_flush_with_empty_queue_is_noop(self):
        with patch("switching.notifications._send") as mock:
            flush_buy_queue()
        mock.assert_not_called()


class TestNotifySell:
    def test_stop_loss_uses_red_icon(self):
        with patch("switching.notifications._send") as mock:
            notify_sell("QUBT", 3.8, -0.2, -0.05, "stop_loss", "ai_pivot")
        text = mock.call_args[0][0]
        assert "STOP LOSS" in text
        assert "QUBT" in text

    def test_profit_uses_money_icon(self):
        with patch("switching.notifications._send") as mock:
            notify_sell("AAPL", 153.0, 3.0, 0.02, "first_green", "ai_pivot")
        text = mock.call_args[0][0]
        assert "SELL AAPL" in text
        assert "+$3.00" in text or "$+3.00" in text


class TestNotifySkip:
    def test_sends_skip(self):
        with patch("switching.notifications._send") as mock:
            notify_skip("TSLA", "max positions", "ai_pivot", "Tesla headline")
        text = mock.call_args[0][0]
        assert "SKIP TSLA" in text
        assert "max positions" in text


class TestNotifyDailySummary:
    def test_sends_summary(self):
        with patch("switching.notifications._send") as mock:
            notify_daily_summary(
                cash=800.0,
                portfolio_value=950.0,
                positions=[{"ticker": "AAPL", "entry_price": 150.0, "days_held": 2, "hold_days": 5}],
                todays_trades=[{"ticker": "MSFT", "pnl": 5.0, "pct_return": 0.02, "exit_reason": "first_green"}],
                total_trades=10,
                total_wins=7,
                total_pnl=25.0,
            )
        text = mock.call_args[0][0]
        assert "Daily Summary" in text
        assert "$950.00" in text
        assert "70%" in text
        assert "MSFT" in text

    def test_no_trades_today(self):
        with patch("switching.notifications._send") as mock:
            notify_daily_summary(
                cash=1000.0, portfolio_value=1000.0,
                positions=[], todays_trades=[],
                total_trades=0, total_wins=0, total_pnl=0.0,
            )
        text = mock.call_args[0][0]
        assert "No trades today" in text


class TestNotifyStartup:
    def test_sends_startup(self):
        with patch("switching.notifications._send") as mock:
            notify_startup(
                cash=982.0, portfolio_value=982.0,
                open_positions=0, total_trades=13,
                detectors=["ai_pivot", "buyback"], scan_interval=10,
            )
        text = mock.call_args[0][0]
        assert "Paper Trader Started" in text
        assert "13 trades" in text
        assert "10m" in text
