"""AI signal filter using Claude Haiku.

Scores each signal 0-1 with a brief reasoning. In log-only mode (default),
the score is recorded in the signal's extra dict but does NOT block trades.
This lets us measure Haiku's accuracy against actual outcomes before
turning on filtering.

Cost: ~$0.30/month at 10-min scan intervals with typical signal volume.
Requires ANTHROPIC_API_KEY env var.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"


def _get_client():
    """Lazy-load anthropic client. Returns None if not configured."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
        return anthropic.Anthropic(api_key=api_key)
    except ImportError:
        log.warning("anthropic package not installed — AI filter disabled")
        return None


def score_signal(
    headline: str,
    detector: str,
    ticker: str,
    severity: float,
    evidence: str,
    memory: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Ask Haiku to score a signal. Returns dict with score + reasoning, or None."""
    client = _get_client()
    if client is None:
        return None

    memory_context = ""
    if memory and memory.get("total_trades", 0) >= 5:
        det_stats = memory.get("by_detector", {}).get(detector, {})
        if det_stats:
            memory_context = (
                f"\nHistorical performance for {detector}: "
                f"{det_stats.get('trades', 0)} trades, "
                f"{det_stats.get('win_rate', 0):.0%} win rate, "
                f"{det_stats.get('avg_return', 0):+.2%} avg return."
            )
        patterns = memory.get("patterns", [])
        if patterns:
            memory_context += f"\nKnown patterns: {'; '.join(patterns[:5])}"

    prompt = f"""Score this stock signal from 0.0 (avoid) to 1.0 (strong buy).

Signal:
- Detector: {detector}
- Ticker: {ticker}
- Headline: {headline}
- Evidence: {evidence}
- Detector severity: {severity}
{memory_context}

Consider:
1. Is the headline a genuine catalyst or noise?
2. Does the company/ticker seem legitimate (not a micro-cap pump)?
3. How likely is a positive price reaction in the next 1-5 days?

Reply with ONLY valid JSON: {{"score": 0.XX, "reasoning": "one sentence"}}"""

    try:
        response = client.messages.create(
            model=_MODEL,
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        result = json.loads(text)
        result["score"] = max(0.0, min(1.0, float(result["score"])))
        return result
    except Exception as exc:
        log.warning("AI filter failed for %s: %s", ticker, exc)
        return None


def score_signals(signals: list, memory: dict[str, Any] | None = None) -> list:
    """Score a batch of signals, adding ai_score to each signal's extra dict.

    Returns the same signal list (unfiltered). Scores are logged and stored
    for later analysis — no trades are blocked.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return signals

    for sig in signals:
        result = score_signal(
            headline=sig.headline,
            detector=sig.detector,
            ticker=sig.ticker,
            severity=sig.severity,
            evidence=sig.evidence,
            memory=memory,
        )
        if result:
            sig.extra["ai_score"] = result.get("score")
            sig.extra["ai_reasoning"] = result.get("reasoning", "")
            log.info(
                "AI score for %s (%s): %.2f — %s",
                sig.ticker, sig.detector,
                result["score"], result.get("reasoning", ""),
            )
    return signals
