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
import time
from typing import Any

log = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"

# ---------------------------------------------------------------------------
# Circuit breaker (Release-It!: fail fast + loud, don't hammer a dead dependency)
# ---------------------------------------------------------------------------
# A dead/invalid ANTHROPIC_API_KEY (or an Anthropic outage) used to throw a 401
# on EVERY signal EVERY scan — silent except for log spam, only noticed a month
# later by spotting £0 spend. The breaker trips after N consecutive failures:
# stops calling the API, fires ONE Telegram alert + error log, then probes once
# per cooldown (half-open) and alerts again on recovery. Scoring is log-only, so
# tripping never affects trades — it just stops the waste and makes the failure loud.
_BREAKER_THRESHOLD = 3          # consecutive failures before tripping
_BREAKER_COOLDOWN_S = 1800.0    # 30 min between half-open probe attempts

_consec_failures = 0
_breaker_open = False
_breaker_opened_at = 0.0


def _reset_breaker() -> None:
    """Reset breaker state (used by tests)."""
    global _consec_failures, _breaker_open, _breaker_opened_at
    _consec_failures = 0
    _breaker_open = False
    _breaker_opened_at = 0.0


def _breaker_blocks() -> bool:
    """True when the breaker is open and still cooling down → skip the API call.
    When the cooldown has elapsed, returns False to allow ONE half-open probe."""
    if not _breaker_open:
        return False
    return (time.time() - _breaker_opened_at) < _BREAKER_COOLDOWN_S


def _alert(text: str) -> None:
    """Log loudly + best-effort Telegram. Never raises out of the breaker path."""
    log.error(text)
    try:
        from switching import notifications
        notifications.notify_alert(text)   # ops bot — a failure alert, not a routine note
    except Exception:   # notifications unconfigured / import issue must not crash scoring
        pass


def _record_failure(ticker: str, exc: Exception) -> None:
    """Count a failed call; trip + alert ONCE when the threshold is crossed."""
    global _consec_failures, _breaker_open, _breaker_opened_at
    _consec_failures += 1
    if not _breaker_open and _consec_failures >= _BREAKER_THRESHOLD:
        _breaker_open = True
        _breaker_opened_at = time.time()
        _alert(
            f"🤖 AI filter DISABLED — Anthropic API failing after {_consec_failures} "
            f"consecutive errors ({type(exc).__name__}: {str(exc)[:140]}). "
            f"Retrying every {int(_BREAKER_COOLDOWN_S // 60)} min. Trades are unaffected "
            "(scoring is log-only) — check ANTHROPIC_API_KEY / Anthropic status."
        )
    elif _breaker_open:
        # a half-open probe failed → restart the cooldown, stay quiet (no re-alert spam)
        _breaker_opened_at = time.time()


def _record_success() -> None:
    """A successful call clears the breaker; alert once on recovery."""
    global _consec_failures, _breaker_open, _breaker_opened_at
    if _breaker_open:
        _alert("✅ AI filter RECOVERED — Anthropic API responding again; scoring resumed.")
    _consec_failures = 0
    _breaker_open = False
    _breaker_opened_at = 0.0


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
    if _breaker_blocks():
        return None   # API is known-bad; skip the doomed call until the next probe window

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
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        text = _extract_json(text)
        result = json.loads(text)
        result["score"] = max(0.0, min(1.0, float(result["score"])))
        _record_success()
        return result
    except Exception as exc:
        _record_failure(ticker, exc)
        log.warning("AI filter failed for %s: %s (raw: %r)", ticker, exc,
                     response.content[0].text[:200] if 'response' in dir() else "no response")
        return None


def _extract_json(text: str) -> str:
    """Strip markdown code fences and extract JSON object from response."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start:end + 1]
    return text


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
