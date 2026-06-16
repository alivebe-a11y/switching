"""Benzinga News API client — a primary US news source.

Why it matters: Benzinga PRE-TAGS tickers (`stocks[].name`), so a Benzinga-sourced
item needs no `extract_ticker()` guess — the ticker-resolution problem (the whole
reason the detection funnel exists) largely disappears for this source. It also gives
full article body, near-real-time `updatedSince` polling, and catalyst `channels`
(incl. WIIMs / Press Releases). US-only (UK still = RNS).

Config: `BENZINGA_API_KEY` env. If unset, every call is a silent no-op returning []
so callers fall back to their existing source (nothing breaks). Single attempt + log
on failure (no retry storm); bounded timeout. Endpoint/params confirmed against the
OpenAPI spec + a live probe (2026-06): GET /api/v2/news, auth via `token` query param.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_BASE = "https://api.benzinga.com"
_TIMEOUT = 15


def is_configured() -> bool:
    return bool(os.environ.get("BENZINGA_API_KEY"))


def _csv(v) -> str:
    return ",".join(str(x) for x in v) if isinstance(v, (list, tuple, set)) else str(v)


def _normalise(it: dict) -> dict:
    """Map a raw Benzinga NewsItem to our internal shape. Pure."""
    return {
        "id": it.get("id"),
        "title": (it.get("title") or "").strip(),
        "body": it.get("body") or "",
        "teaser": it.get("teaser") or "",
        # what classify() reads as the summary — prefer teaser, fall back to body
        "summary": (it.get("teaser") or it.get("body") or "").strip(),
        # PRE-TAGGED tickers — no resolution needed
        "tickers": [s.get("name") for s in (it.get("stocks") or []) if isinstance(s, dict) and s.get("name")],
        "channels": [c.get("name") for c in (it.get("channels") or []) if isinstance(c, dict) and c.get("name")],
        "created": it.get("created") or "",
        "url": it.get("url") or "",
        "importance": it.get("importance_rank"),
    }


def fetch_news(*, tickers=None, channels=None, updated_since: int | None = None,
               display_output: str = "abstract", page_size: int = 50,
               timeout: int = _TIMEOUT) -> list[dict]:
    """Fetch news items, normalised. Returns [] if unconfigured or on any error.

    display_output: 'headline' | 'abstract' (headline+teaser) | 'full' (+body).
    updated_since: unix ts → poll everything new since the last check.
    """
    key = os.environ.get("BENZINGA_API_KEY")
    if not key:
        return []
    params: dict = {
        "token": key,
        "pageSize": max(1, min(int(page_size), 100)),
        "displayOutput": display_output,
    }
    if tickers:
        params["tickers"] = _csv(tickers)
    if channels:
        params["channels"] = _csv(channels)
    if updated_since is not None:
        params["updatedSince"] = int(updated_since)

    url = _BASE + "/api/v2/news?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"accept": "application/json", "User-Agent": "switching"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # 429 = rate limited (generous limits, but be loud if it ever happens)
        log.warning("Benzinga news HTTP %s %s", exc.code, exc.reason)
        return []
    except Exception as exc:  # network/timeout/parse — degrade, never raise
        log.warning("Benzinga news fetch failed: %s", exc)
        return []

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        log.warning("Benzinga news: non-JSON response (%d bytes)", len(payload))
        return []

    items = data if isinstance(data, list) else (data.get("news") or data.get("data") or [])
    return [_normalise(it) for it in items if isinstance(it, dict)]
