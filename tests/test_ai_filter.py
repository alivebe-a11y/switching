"""Tests for AI signal filter (Haiku scoring)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import switching.ai_filter as aif
from switching.ai_filter import score_signal, score_signals


@pytest.fixture(autouse=True)
def _reset_breaker():
    """Breaker state is module-global — reset it before/after each test."""
    aif._reset_breaker()
    yield
    aif._reset_breaker()


@dataclass
class FakeSignal:
    headline: str
    detector: str
    ticker: str
    severity: float
    evidence: str
    extra: dict[str, Any] = field(default_factory=dict)


class TestScoreSignal:
    def test_returns_none_without_api_key(self):
        with patch.dict("os.environ", {}, clear=True):
            result = score_signal("headline", "ai_pivot", "AAPL", 0.8, "evidence")
        assert result is None

    def test_returns_score_with_mock_client(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"score": 0.85, "reasoning": "Strong catalyst"}')]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}), \
             patch("switching.ai_filter._get_client", return_value=mock_client):
            result = score_signal("NVDA beats estimates", "earnings_surprise", "NVDA", 0.8, "evidence")

        assert result is not None
        assert result["score"] == 0.85
        assert "Strong catalyst" in result["reasoning"]

    def test_score_clamped_to_0_1(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"score": 1.5, "reasoning": "test"}')]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}), \
             patch("switching.ai_filter._get_client", return_value=mock_client):
            result = score_signal("headline", "ai_pivot", "TEST", 0.5, "evidence")

        assert result["score"] == 1.0

    def test_handles_api_error(self):
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("API error")

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}), \
             patch("switching.ai_filter._get_client", return_value=mock_client):
            result = score_signal("headline", "ai_pivot", "TEST", 0.5, "evidence")

        assert result is None

    def test_includes_memory_context(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"score": 0.7, "reasoning": "ok"}')]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        memory = {
            "total_trades": 10,
            "by_detector": {
                "ai_pivot": {"trades": 5, "win_rate": 0.6, "avg_return": 0.01}
            },
            "patterns": ["ai_pivot: moderate performer"],
        }

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}), \
             patch("switching.ai_filter._get_client", return_value=mock_client):
            result = score_signal("headline", "ai_pivot", "TEST", 0.5, "evidence", memory=memory)

        call_args = mock_client.messages.create.call_args
        prompt = call_args[1]["messages"][0]["content"]
        assert "60%" in prompt
        assert "moderate performer" in prompt


class TestScoreSignals:
    def test_no_op_without_api_key(self):
        signals = [FakeSignal("test", "ai_pivot", "AAPL", 0.8, "evidence")]
        with patch.dict("os.environ", {}, clear=True):
            result = score_signals(signals)
        assert result is signals
        assert "ai_score" not in signals[0].extra

    def test_adds_score_to_extra(self):
        signals = [FakeSignal("test", "ai_pivot", "AAPL", 0.8, "evidence")]

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}), \
             patch("switching.ai_filter.score_signal", return_value={"score": 0.9, "reasoning": "good"}):
            result = score_signals(signals)

        assert result[0].extra["ai_score"] == 0.9
        assert result[0].extra["ai_reasoning"] == "good"


class TestCircuitBreaker:
    def test_single_failure_does_not_trip(self):
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("boom")
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k"}), \
             patch("switching.ai_filter._get_client", return_value=mock_client), \
             patch("switching.notifications.notify_text") as notify:
            score_signal("h", "ai_pivot", "AAA", 0.5, "e")
        assert not aif._breaker_open
        notify.assert_not_called()

    def test_trips_after_threshold_and_stops_calling(self):
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("401 invalid x-api-key")
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k"}), \
             patch("switching.ai_filter._get_client", return_value=mock_client), \
             patch("switching.notifications.notify_text") as notify:
            # 3 failures trip the breaker; the 4th call must be skipped entirely
            for _ in range(4):
                assert score_signal("h", "ai_pivot", "AAA", 0.5, "e") is None
        assert aif._breaker_open
        assert mock_client.messages.create.call_count == 3   # 4th was blocked, not called
        # exactly ONE alert (the trip), no spam
        assert notify.call_count == 1
        assert "DISABLED" in notify.call_args[0][0]

    def test_recovers_and_alerts_after_cooldown(self):
        good = MagicMock()
        good.content = [MagicMock(text='{"score": 0.7, "reasoning": "ok"}')]
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [Exception("x"), Exception("x"), Exception("x"), good]
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k"}), \
             patch("switching.ai_filter._get_client", return_value=mock_client), \
             patch("switching.notifications.notify_text") as notify:
            for _ in range(3):
                score_signal("h", "ai_pivot", "AAA", 0.5, "e")
            assert aif._breaker_open
            # simulate the cooldown elapsing so the next call is a half-open probe
            aif._breaker_opened_at = 0.0
            result = score_signal("h", "ai_pivot", "AAA", 0.5, "e")
        assert result is not None and result["score"] == 0.7
        assert not aif._breaker_open          # recovered
        assert aif._consec_failures == 0
        assert notify.call_count == 2         # trip + recovery
        assert "RECOVERED" in notify.call_args[0][0]
