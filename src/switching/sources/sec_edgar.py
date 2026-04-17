"""Thin SEC EDGAR wrapper.

Three endpoints are used:

- https://efts.sec.gov/LATEST/search-index   - full-text filing search
- https://data.sec.gov/submissions/CIK{cik}.json - issuer metadata / history
- https://www.sec.gov/files/company_tickers.json - CIK ↔ ticker map

SEC requires a descriptive ``User-Agent`` header and asks callers to keep
under 10 req/sec. We enforce 8 req/s by default and sleep on 429s.

The wrapper is deliberately small — each detector drives its own search
terms and post-filters the results.
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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from switching.pricing import _default_cache_path

log = logging.getLogger(__name__)

_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"

_UA_ENV = "SWITCHING_EDGAR_UA"


class EdgarAuthError(RuntimeError):
    """Raised when the required User-Agent is missing."""


def _require_user_agent(explicit: str | None) -> str:
    if explicit:
        return explicit
    env_val = os.environ.get(_UA_ENV)
    if env_val:
        return env_val
    raise EdgarAuthError(
        "SEC EDGAR requires a descriptive User-Agent. Set "
        f"${_UA_ENV} (e.g. 'YourName your.email@example.com') or pass user_agent=..."
    )


@dataclass(frozen=True)
class Filing:
    accession: str
    cik: str
    company: str
    form: str
    filed: date
    ticker: str | None
    filer: str | None = None         # for 13D/13G filings: the filer name
    reported_pct: float | None = None  # for 13D/13G: stake percentage
    url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class _RateLimiter:
    def __init__(self, rate_per_sec: float) -> None:
        self._min_gap = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self._min_gap <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._min_gap - (now - self._last)
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


class EdgarClient:
    def __init__(
        self,
        user_agent: str | None = None,
        *,
        rate_limit: float = 8.0,
        cache_path: Path | None = None,
        opener=None,
    ) -> None:
        self._ua = _require_user_agent(user_agent)
        self._limiter = _RateLimiter(rate_limit)
        self._cache_path = cache_path or _default_cache_path()
        # Tests inject a callable (url, headers) -> bytes so we never touch network.
        self._opener = opener
        self._ticker_cache: dict[str, str] | None = None

    # -------- HTTP core --------------------------------------------------

    def _fetch(self, url: str) -> bytes:
        self._limiter.wait()
        if self._opener is not None:
            return self._opener(url, {"User-Agent": self._ua})
        req = urllib.request.Request(url, headers={"User-Agent": self._ua})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                log.warning("EDGAR 429 — backing off 2s")
                time.sleep(2.0)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read()
            raise

    def _fetch_json(self, url: str) -> dict[str, Any]:
        return json.loads(self._fetch(url).decode("utf-8"))

    # -------- Ticker map -------------------------------------------------

    def ticker_for_cik(self, cik: str) -> str | None:
        norm = str(cik).lstrip("0") or "0"
        mapping = self._load_ticker_map()
        return mapping.get(norm)

    def _load_ticker_map(self) -> dict[str, str]:
        if self._ticker_cache is not None:
            return self._ticker_cache
        try:
            raw = self._fetch_json(_TICKER_MAP_URL)
        except Exception as exc:
            log.warning("failed to load EDGAR ticker map: %s", exc)
            self._ticker_cache = {}
            return self._ticker_cache
        # Upstream shape: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
        out: dict[str, str] = {}
        for entry in raw.values():
            cik = str(entry.get("cik_str", "")).lstrip("0") or "0"
            ticker = entry.get("ticker")
            if cik and ticker and cik not in out:
                out[cik] = ticker
        self._ticker_cache = out
        return out

    # -------- Filing search ---------------------------------------------

    def search_filings(
        self,
        *,
        forms: Iterable[str],
        since: date,
        until: date | None = None,
        query: str | None = None,
        max_pages: int = 5,
    ) -> list[Filing]:
        until = until or date.today()
        params = {
            "q": query or "",
            "dateRange": "custom",
            "startdt": since.isoformat(),
            "enddt": until.isoformat(),
            "forms": ",".join(forms),
        }
        results: list[Filing] = []
        for page in range(max_pages):
            params["from"] = str(page * 10)
            url = f"{_SEARCH_URL}?{urllib.parse.urlencode(params)}"
            try:
                payload = self._fetch_json(url)
            except Exception as exc:
                log.warning("EDGAR search failed on %s: %s", url, exc)
                break
            hits = payload.get("hits", {}).get("hits", [])
            if not hits:
                break
            for hit in hits:
                results.append(self._hit_to_filing(hit))
            if len(hits) < 10:
                break
        return results

    def _hit_to_filing(self, hit: dict[str, Any]) -> Filing:
        src = hit.get("_source", {})
        accession = (hit.get("_id") or "").split(":", 1)[0].replace("-", "")
        ciks = src.get("ciks") or []
        cik = str(ciks[0]).lstrip("0") if ciks else ""
        names = src.get("display_names") or [""]
        company = names[0].split(" (CIK")[0].strip() if names else ""
        form = src.get("form", "")
        filed = datetime.fromisoformat(src.get("file_date")).date() if src.get("file_date") else date.today()
        ticker = self.ticker_for_cik(cik) if cik else None
        accession_dashed = _format_accession(accession)
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik}/"
            f"{accession}/{accession_dashed}-index.htm"
            if cik and accession
            else ""
        )
        return Filing(
            accession=accession,
            cik=cik,
            company=company,
            form=form,
            filed=filed,
            ticker=ticker,
            filer=src.get("filer"),
            url=url,
            extra={"raw": src},
        )


def _format_accession(compact: str) -> str:
    # Compact "0000320193-24-000123" → dashed for URLs.
    if len(compact) == 18 and compact.isdigit():
        return f"{compact[:10]}-{compact[10:12]}-{compact[12:]}"
    if "-" in compact:
        return compact
    return compact
