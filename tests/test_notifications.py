"""Tests for Telegram notifications."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

from switching.notifications import (
    _send,
    flush_buy_queue,
    is_configured,
    notify_buy,
    notify_sell,
    notify_skip,
    notify_daily_summary,
    notify_startup,
    pending_buy_count,
)


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
