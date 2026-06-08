"""Trading 212 REST API client.

Wraps the Trading 212 v0 equity API for market order execution and
portfolio queries. Supports both demo and live environments.

Environment variables:
  T212_API_KEY    — API key from Trading 212 app → Settings → API
  T212_API_SECRET — API secret from Trading 212 app → Settings → API
  T212_DEMO       — "true" (default) uses demo.trading212.com
                    "false" uses live.trading212.com (real money!)

Authentication: HTTP Basic Auth — base64(API_KEY:API_SECRET)
Both key and secret are required for every request.

Ticker format: Trading 212 uses market-suffixed instrument IDs internally:
  - US equities: "AAPL_US_EQ"        (NYSE/NASDAQ)
  - UK equities: "MKSL_EQ"           (LSE — {TICKER}L_EQ, capital L)
This client accepts plain symbols ("AAPL", "MKS.L") and converts automatically
based on the client's market ("us" / "uk", set at construction time).

Dual-listing safety: VOD is listed on both Nasdaq (VOD_US_EQ, the ADR in USD)
and the LSE (VODL_EQ, primary in GBX). A client constructed with market="uk"
will never resolve "VOD.L" to the US ADR and vice versa — the mapper is
strict, so paper-vs-T212 slippage analysis is never poisoned by currency
mismatch.

Rate limits (T212 docs — PER ENDPOINT, stricter than a flat req/s):
  - /equity/positions is the tightest we hit (~1 request / 5s)
  - /equity/account/summary and order placement ~1 / 2s
This client throttles each endpoint to a conservative minimum interval
(_ENDPOINT_MIN_INTERVAL) and retries HTTP 429 with Retry-After/backoff,
so callers don't have to manage spacing themselves.

Reference: https://docs.trading212.com/api
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from datetime import time as dt_time

import requests

log = logging.getLogger(__name__)

_DEMO_BASE = "https://demo.trading212.com/api/v0"
_LIVE_BASE = "https://live.trading212.com/api/v0"

# ---------------------------------------------------------------------------
# Client-side rate limiting
# ---------------------------------------------------------------------------
# T212's public API is rate-limited PER ENDPOINT and PER ACCOUNT (not per key
# or IP) — so the US + UK T212 services share one budget. The numbers below
# are taken directly from the official skill manifest vendored at
# docs/vendor/t212-api.md. We add a small ~+10% safety margin on the strict
# endpoints to absorb scheduling jitter without crossing the line.
#
# When the limit changes upstream, `python scripts/refresh_t212_docs.py
# --check` flags the diff and we update this table to match.
_ENDPOINT_MIN_INTERVAL: dict[str, float] = {
    # GETs
    "/equity/account/summary":        5.0,    # 1 req/5s
    "/equity/positions":              1.0,    # 1 req/1s
    "/equity/metadata/instruments":  50.0,    # 1 req/50s (~5 MB response)
    "/equity/metadata/exchanges":    30.0,    # 1 req/30s
    # Orders — POSTs
    "/equity/orders/market":          1.3,    # 50 req/min (~1.2s) + margin
    "/equity/orders/limit":           2.0,    # 1 req/2s
    "/equity/orders/stop":            2.0,    # 1 req/2s
    "/equity/orders/stop_limit":      2.0,    # 1 req/2s
}
_DEFAULT_MIN_INTERVAL = 1.0      # any endpoint not listed above

# 429 (Too Many Requests) handling: respect Retry-After when present, else use
# an escalating backoff. After this many retries we give up and raise.
_MAX_RETRIES_429 = 4
_DEFAULT_BACKOFF_SECONDS = 5.0

# Catalogue cache TTL — the instruments endpoint is rate-limited to 1 req/50s
# and returns ~5MB. New instruments appear infrequently, so a long cache is
# safe. 1 hour gives the preflight check sub-millisecond lookups after a
# single cold fetch.
_INSTRUMENT_CACHE_TTL_SECONDS = 3600.0

# Instrument types that T212's /equity/orders/* endpoints will accept. From
# the catalogue field `type` documented in T212's skill manifest. We only
# trade STOCK and ETF — everything else (CRYPTOCURRENCY / FUTURES / INDEX /
# WARRANT / CVR / CORPACT) either isn't a real position the bot supports or
# isn't orderable via the equity API (the CFD side is not exposed at all).
_TRADEABLE_INSTRUMENT_TYPES = frozenset({"STOCK", "ETF"})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class T212Account:
    """Snapshot of account cash state."""
    free: float       # Buying power available now
    total: float      # Total account value (cash + positions)
    invested: float   # Amount deployed in open positions
    ppl: float        # Unrealised P&L across all open positions
    currency: str = "USD"   # account base currency (e.g. GBP for UK demo accounts)


# Account-currency -> display symbol. Falls back to the ISO code when unknown.
_CURRENCY_SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€"}


def currency_symbol(code: str | None) -> str:
    return _CURRENCY_SYMBOLS.get((code or "").upper(), code or "$")


def _to_major_units(price: float, market: str) -> float:
    """Convert a T212 instrument price to its MAJOR currency unit.

    T212 quotes LSE (UK) instruments in GBX (pence) — the same convention as
    yfinance — so divide by 100 to get GBP. Doing this once at the broker
    boundary means every downstream consumer (position sizing, stored entry/
    exit prices, the dashboard's cached prices, ghost-reconciliation P&L) works
    in major units, exactly like the internal paper trader's `_normalise_price`.
    US prices are already in their major unit (USD) and pass through untouched.
    """
    return price / 100.0 if (market or "").lower() == "uk" else price


@dataclass
class T212Position:
    """A single open position returned by the positions endpoint."""
    symbol: str              # Clean ticker e.g. "AAPL"
    t212_ticker: str         # T212 internal format e.g. "AAPL_US_EQ"
    quantity: float
    avg_entry_price: float   # T212 average fill price
    current_price: float     # Live price from T212 feed
    unrealized_pnl: float    # £/$ P&L
    unrealized_pnl_pct: float  # as fraction e.g. 0.05 = +5%


@dataclass
class T212Order:
    """Minimal order record returned after placement."""
    id: str
    t212_ticker: str
    status: str   # "CONFIRMED", "PENDING", "REJECTED", ...


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class T212AuthError(RuntimeError):
    """Raised when the API key is missing or rejected."""


class T212OrderError(RuntimeError):
    """Raised when an order is rejected by Trading 212."""


class T212RateLimitError(T212OrderError):
    """Raised when 429 retries are exhausted (subclass so existing
    broad ``except T212OrderError`` handlers still catch it)."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class Trading212Client:
    """Minimal Trading 212 REST client.

    Only the methods needed by the paper-trading loop are implemented:
    get_account, get_positions, buy_market, sell_all, is_market_open.
    """

    def __init__(self, market: str = "us") -> None:
        """Construct a Trading 212 client scoped to one market.

        Args:
            market: "us" (default, NYSE/NASDAQ → _US_EQ suffix) or
                    "uk" (LSE → L_EQ suffix). Sets the ticker-mapping
                    convention AND the market-hours gate AND the
                    position filter (the bulkhead: a UK client sees
                    only UK positions in get_positions(), and vice
                    versa, so US ghost-reconciliation cannot close
                    UK positions and vice versa).

        Raises:
            ValueError: if market is not "us" or "uk".
            T212AuthError: if API credentials are missing.
        """
        market = (market or "us").lower()
        if market not in ("us", "uk"):
            raise ValueError(
                f"Unsupported T212 market: {market!r} (expected 'us' or 'uk')"
            )
        self.market: str = market

        api_key = os.environ.get("T212_API_KEY", "").strip()
        api_secret = os.environ.get("T212_API_SECRET", "").strip()
        if not api_key:
            raise T212AuthError(
                "T212_API_KEY is not set. "
                "Generate a key in the Trading 212 app: Settings → API."
            )
        if not api_secret:
            raise T212AuthError(
                "T212_API_SECRET is not set. "
                "Generate a key in the Trading 212 app: Settings → API."
            )
        demo_env = os.environ.get("T212_DEMO", "true").strip().lower()
        self.demo: bool = demo_env != "false"
        self._base = _DEMO_BASE if self.demo else _LIVE_BASE
        self._session = requests.Session()
        self._session.verify = True   # always verify TLS — never override
        # T212 uses HTTP Basic Auth: base64(API_KEY:API_SECRET)
        self._session.auth = (api_key, api_secret)
        self._session.headers.update({"Content-Type": "application/json"})
        # Per-endpoint last-call timestamps (monotonic) for client-side throttling
        self._last_call: dict[str, float] = {}
        # Instrument-catalogue cache: (monotonic_timestamp, {t212_ticker: entry}).
        # Populated lazily on first can_buy() / get_instrument_catalogue() call.
        # Per-instance, so each service does ONE 5MB fetch per hour — fits the
        # 1 req/50s endpoint limit comfortably for the two US+UK services.
        self._instrument_cache: tuple[float, dict[str, dict]] | None = None
        # Per-ticker accepted quantity precision (decimals). Learned on first
        # order from T212's 'quantity-precision-mismatch' errors so we don't
        # keep re-submitting a too-precise quantity. See _place_market_order.
        self._qty_precision: dict[str, int] = {}
        # Never log the key itself — only mode, market, and base URL
        mode = "DEMO" if self.demo else "LIVE"
        log.info(
            "Trading212Client initialised (%s, market=%s) base=%s",
            mode, self.market.upper(), self._base,
        )
        # Scrub credentials from local scope after session is configured
        del api_key, api_secret

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_account(self) -> T212Account:
        """Return a snapshot of account cash / equity.

        Uses GET /equity/account/summary which returns:
          { totalValue, currency,
            cash: { availableToTrade, inPies, reservedForOrders },
            investments: { currentValue, totalCost,
                           unrealizedProfitLoss, realizedProfitLoss } }
        """
        data = self._get("/equity/account/summary")
        cash = data.get("cash", {})
        investments = data.get("investments", {})
        return T212Account(
            free=_safe_float(cash.get("availableToTrade")),
            total=_safe_float(data.get("totalValue")),
            invested=_safe_float(investments.get("currentValue")),
            ppl=_safe_float(investments.get("unrealizedProfitLoss")),
            currency=(data.get("currency") or "USD").upper(),
        )

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_positions(self) -> list[T212Position]:
        """Return all open positions in this client's market.

        The endpoint is paginated but for typical retail portfolios a single
        page (up to 50 items) is sufficient.  We fetch page 1 only; extend
        if needed.

        Bulkhead: positions are filtered to ``self.market`` before being
        returned. So a UK client never sees US positions (and vice versa),
        even though both services share one T212 account. Without this
        filter, the US service's ghost-reconciliation would treat every
        UK position as an orphan and close it (and vice versa).
        """
        data = self._get("/equity/positions")
        # Response is either a bare list or {"items": [...], "nextPagePath": ...}
        items: list[dict] = data if isinstance(data, list) else data.get("items", [])
        result: list[T212Position] = []
        filtered_other_market = 0
        for item in items:
            # ticker is nested: item["instrument"]["ticker"] e.g. "AAPL_US_EQ"
            instrument = item.get("instrument") or {}
            t212_ticker = instrument.get("ticker", "")
            # Bulkhead — drop positions belonging to the other market(s)
            if not _matches_market(t212_ticker, self.market):
                filtered_other_market += 1
                continue
            symbol = _from_t212_ticker(t212_ticker, self.market)
            qty = _safe_float(item.get("quantity"))
            # field is "averagePricePaid", not "averagePrice"
            avg = _safe_float(item.get("averagePricePaid"))
            cur = _safe_float(item.get("currentPrice"), default=avg)
            # P&L is nested: item["walletImpact"]["unrealizedProfitLoss"]
            wallet = item.get("walletImpact") or {}
            ppl = _safe_float(wallet.get("unrealizedProfitLoss"))
            # pnl_pct from RAW prices — a ratio, so unit-invariant (pence or
            # major, the % is the same). Compute it BEFORE converting units.
            pnl_pct = (cur - avg) / avg if avg > 0 else 0.0
            # Convert GBX (pence) -> GBP for UK so callers see major units.
            avg = _to_major_units(avg, self.market)
            cur = _to_major_units(cur, self.market)
            log.debug(
                "T212[%s] position %s qty=%.4f avg=%.4f cur=%.4f ppl=%.2f pct=%+.4f",
                self.market, symbol, qty, avg, cur, ppl, pnl_pct,
            )
            result.append(T212Position(
                symbol=symbol,
                t212_ticker=t212_ticker,
                quantity=qty,
                avg_entry_price=avg,
                current_price=cur,
                unrealized_pnl=ppl,
                unrealized_pnl_pct=pnl_pct,
            ))
        if filtered_other_market:
            # INFO not DEBUG — this is the bulkhead announcing it did its job.
            # We want it visible in the default service log level so we can
            # confirm the US and UK T212 services aren't seeing each other's
            # positions.
            log.info(
                "T212 get_positions(market=%s): kept %d, filtered %d (other market)",
                self.market, len(result), filtered_other_market,
            )
        return result

    def get_position(self, symbol: str) -> T212Position | None:
        """Return a single position by symbol, or None if not held."""
        for pos in self.get_positions():
            if pos.symbol == symbol.upper():
                return pos
        return None

    # ------------------------------------------------------------------
    # Instrument catalogue (preflight tradability check)
    # ------------------------------------------------------------------

    def get_instrument_catalogue(self) -> dict[str, dict]:
        """Return the full T212 catalogue keyed by ``ticker``.

        Cached for ``_INSTRUMENT_CACHE_TTL_SECONDS`` (default 1h). The
        catalogue is ~5MB / ~17k instruments and the endpoint is rate-limited
        to 1 req/50s, so the cache is a hard requirement — we want to look
        up every buy candidate against it without hitting T212 each time.

        Failure modes (release-it: "preserve core service in degraded mode"):
          - Network/HTTP error → log warning, return empty dict. Callers
            using ``can_buy`` will then fail OPEN (allow the buy attempt),
            which is the right default: better to attempt a real order and
            learn from the order endpoint's response than to block trading
            because the catalogue is briefly unreachable.
          - Bad shape → same as above.
        """
        now = time.monotonic()
        cache = self._instrument_cache
        if cache is not None:
            cached_at, by_ticker = cache
            if now - cached_at < _INSTRUMENT_CACHE_TTL_SECONDS:
                return by_ticker

        log.info("T212[%s] fetching instrument catalogue (~5MB, cold cache)", self.market)
        try:
            data = self._get("/equity/metadata/instruments")
        except Exception as exc:
            log.warning(
                "T212[%s] catalogue fetch failed (%s) — preflight checks "
                "will fail OPEN until next retry",
                self.market, exc,
            )
            return {}

        items = data if isinstance(data, list) else data.get("instruments", [])
        if not isinstance(items, list):
            log.warning(
                "T212[%s] catalogue returned unexpected shape %s — failing open",
                self.market, type(items).__name__,
            )
            return {}

        by_ticker: dict[str, dict] = {}
        for entry in items:
            if not isinstance(entry, dict):
                continue
            tkr = entry.get("ticker")
            if isinstance(tkr, str) and tkr:
                by_ticker[tkr.upper()] = entry

        log.info(
            "T212[%s] catalogue cached: %d instruments (TTL %.0fs)",
            self.market, len(by_ticker), _INSTRUMENT_CACHE_TTL_SECONDS,
        )
        self._instrument_cache = (now, by_ticker)
        return by_ticker

    def can_buy(self, symbol: str) -> tuple[bool, str | None]:
        """Preflight: is *symbol* orderable via this client's market?

        Returns ``(True, None)`` if the resulting T212 instrument ID is in
        the catalogue and has a tradeable ``type`` (STOCK or ETF). Returns
        ``(False, reason)`` for anything we recognise as non-orderable —
        the reason is suitable for a SKIP log + detection_funnel record.

        Fails OPEN on catalogue-fetch failure (returns ``(True, None)``)
        so a brief T212 outage can't grind buys to a halt; the order
        endpoint becomes the final arbiter.
        """
        try:
            t212_id = _to_t212_ticker(symbol, self.market)
        except ValueError as exc:
            # Bad ticker format — never going to work; surface and skip.
            return False, f"invalid_ticker_format: {exc}"

        catalogue = self.get_instrument_catalogue()
        if not catalogue:
            return True, None    # fail open — catalogue temporarily unavailable

        # Catalogue keys are upper-cased on load, but LSE order tickers carry a
        # lowercase 'l' (BARCl_EQ) — look up case-insensitively so we don't
        # falsely reject a tradeable UK instrument.
        entry = catalogue.get(t212_id) or catalogue.get(t212_id.upper())
        if entry is None:
            return False, f"not_in_t212_catalogue: {t212_id}"

        itype = (entry.get("type") or "").upper()
        if itype in _TRADEABLE_INSTRUMENT_TYPES:
            return True, None

        return False, f"t212_type_not_tradeable: {t212_id} type={itype or 'UNKNOWN'}"

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def _place_market_order(self, t212_ticker: str, quantity: float, side: str) -> T212Order:
        """Place a market order, auto-reducing decimal precision on a T212
        'quantity-precision-mismatch'.

        T212 caps the number of decimals in the order quantity PER instrument
        (some allow 2, some 1, some whole-shares only) and exposes no precision
        field in the catalogue — so we discover it by trying. We submit at the
        requested precision and, on a precision error, step down 4 -> 3 -> 2 ->
        1 -> 0, **flooring** (never rounding UP past available cash / the held
        quantity). The accepted precision is memoised per ticker so we don't
        burn a rejected call next time.

        ``quantity`` is signed: positive = buy, negative = sell.
        """
        base = round(abs(quantity), 4)
        if base < 0.0001:
            raise T212OrderError(f"Quantity too small to {side}: {base} {t212_ticker}")
        sign = 1.0 if quantity > 0 else -1.0
        learned = self._qty_precision.get(t212_ticker)
        precs = ([learned] if learned is not None else []) + [p for p in (4, 3, 2, 1, 0) if p != learned]
        last_exc: Exception | None = None
        for prec in precs:
            qabs = float(int(base)) if prec == 0 else math.floor(base * 10 ** prec) / (10.0 ** prec)
            if qabs < 0.0001:
                continue   # this precision zeroes the order — try a finer one
            signed = qabs if sign > 0 else -qabs
            log.info("T212[%s] %s submit %s qty=%s (prec=%d)", self.market, side.upper(), t212_ticker, signed, prec)
            try:
                data = self._post("/equity/orders/market", {"ticker": t212_ticker, "quantity": signed})
            except T212OrderError as exc:
                if "precision" in str(exc).lower():
                    last_exc = exc
                    continue   # T212 rejected the decimals — coarsen and retry
                raise          # any other error (404, market closed, …) is not ours to retry
            order = _parse_order(data, t212_ticker)
            self._qty_precision[t212_ticker] = prec
            log.info("T212[%s] %s accepted %s id=%s status=%s qty=%s",
                     self.market, side.upper(), t212_ticker, order.id, order.status, signed)
            return order
        raise last_exc or T212OrderError(f"{side} {t212_ticker}: no acceptable quantity precision")

    def buy_market(self, symbol: str, quantity: float) -> T212Order:
        """Market BUY of *quantity* shares (fractional OK). Auto-handles T212's
        per-instrument quantity-precision limits (see _place_market_order)."""
        return self._place_market_order(_to_t212_ticker(symbol, self.market), abs(quantity), "buy")

    def sell_all(self, symbol: str, quantity: float) -> T212Order:
        """Market SELL of *quantity* shares (T212 takes a negative quantity).
        Floors to the instrument's allowed precision so we never oversell."""
        return self._place_market_order(_to_t212_ticker(symbol, self.market), -abs(quantity), "sell")

    # ------------------------------------------------------------------
    # Market hours
    # ------------------------------------------------------------------

    def is_market_open(self) -> bool:
        """Return True during this client's market regular session.

        Dispatches on self.market:
          - "us" → NYSE via market_calendar.is_market_hours() (America/New_York,
                   DST-correct, NYSE holiday list + half-day calendar honoured)
          - "uk" → LSE via market_calendar.is_lse_hours() (Europe/London,
                   BST/GMT correct, LSE half-day calendar honoured)

        Previously this was hardcoded to 14:30–21:00 UTC which is correct
        for EST (Nov–Mar) but one hour late during EDT (Mar–Nov), causing
        the T212 loop to skip the 9:30–10:30 AM EDT window every day.
        """
        if self.market == "uk":
            from switching.market_calendar import is_lse_hours
            return is_lse_hours()
        from switching.market_calendar import is_market_hours
        return is_market_hours()

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _throttle(self, path: str) -> None:
        """Sleep just enough to keep *path* under its per-endpoint min interval.

        Staggers bursts (e.g. repeated /equity/positions calls in one cycle) so
        they don't all hit at once. Per-endpoint, so calls to different endpoints
        don't block each other.
        """
        min_interval = _ENDPOINT_MIN_INTERVAL.get(path, _DEFAULT_MIN_INTERVAL)
        last = self._last_call.get(path)
        if last is not None:
            wait = min_interval - (time.monotonic() - last)
            if wait > 0:
                log.debug("T212 throttle %s: sleeping %.2fs", path, wait)
                time.sleep(wait)
        self._last_call[path] = time.monotonic()

    def _request(self, method: str, path: str, *, json_body: dict | None = None) -> dict | list:
        """Issue a throttled request, retrying on HTTP 429 with backoff."""
        url = self._base + path
        for attempt in range(_MAX_RETRIES_429 + 1):
            self._throttle(path)
            if method == "GET":
                resp = self._session.get(url, timeout=15)
            else:
                resp = self._session.post(url, json=json_body, timeout=15)

            if resp.status_code == 429:
                if attempt >= _MAX_RETRIES_429:
                    raise T212RateLimitError(
                        f"T212 rate limit on {path}: exhausted {_MAX_RETRIES_429} retries"
                    )
                backoff = _retry_after_seconds(resp)
                if backoff is None:
                    backoff = _DEFAULT_BACKOFF_SECONDS * (attempt + 1)  # 5s,10s,15s,20s
                log.warning(
                    "T212 429 on %s — backing off %.1fs (retry %d/%d)",
                    path, backoff, attempt + 1, _MAX_RETRIES_429,
                )
                # Treat the forced wait as the endpoint's last call so the next
                # _throttle() doesn't pile an extra sleep on top.
                time.sleep(backoff)
                self._last_call[path] = time.monotonic()
                continue

            _check_response(resp, path, method=method, payload=json_body, market=self.market)
            return resp.json()
        # Loop always returns or raises above; this is unreachable.
        raise T212RateLimitError(f"T212 rate limit on {path}")  # pragma: no cover

    def _get(self, path: str) -> dict | list:
        return self._request("GET", path)

    def _post(self, path: str, payload: dict) -> dict:
        return self._request("POST", path, json_body=payload)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Response headers we surface in error logs. Allowlist (not allowing through
# anything we don't recognise) so we never accidentally log auth-bearing
# request echoes or cookies T212 might one day return.
_LOG_RESPONSE_HEADERS = (
    "Content-Type", "Content-Length", "Retry-After",
    "X-Request-Id", "X-Correlation-Id", "X-Trace-Id",
    "Date", "Server",
)

# Max body length to surface — bigger than before (200) so the FULL T212 error
# response makes it into logs. Order-rejection bodies are usually small JSON
# objects ({"code": "...", "context": {...}}) — clipping them was killing the
# whole point of "tell me why it failed".
_ERROR_BODY_MAX_LEN = 4000


def _check_response(
    resp: requests.Response,
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    market: str = "us",
) -> None:
    """Inspect a T212 response and raise the right exception on failure.

    On any 4xx/5xx we log.error a structured diagnostic line FIRST (so it
    lands in Dockge logs even if the caller swallows the exception), then
    raise. The diagnostic includes:
      - market scope (us/uk) — which client made the call
      - HTTP method + path + status
      - the request payload we sent (for order failures, this is the most
        useful field — tells us the T212 ticker we tried to trade)
      - response headers from the allowlist (Retry-After, X-Request-Id,
        Content-Type, etc.)
      - full response body, sanitised (auth tokens stripped) and capped at
        _ERROR_BODY_MAX_LEN (4000 chars — generous, T212 error bodies are
        normally ~100 chars of JSON).

    The exception message keeps the salient info inline so existing
    handlers and Telegram alerts still get useful text without grepping
    container logs.
    """
    if 200 <= resp.status_code < 300:
        return

    body = _sanitise_error_body(resp.text or "", max_len=_ERROR_BODY_MAX_LEN)
    headers = {k: v for k, v in resp.headers.items() if k in _LOG_RESPONSE_HEADERS}
    payload_str = _sanitise_payload(payload) if payload is not None else None

    # One structured line that captures everything we know about the failure.
    log.error(
        "T212 FAIL [%s] %s %s -> %s %s | headers=%s | payload=%s | body=%s",
        market.upper(),
        method,
        path,
        resp.status_code,
        resp.reason or "",
        headers,
        payload_str,
        body,
    )

    if resp.status_code == 401:
        raise T212AuthError(
            "Trading 212 API key invalid or expired. "
            "Regenerate at Settings → API in the app. "
            f"(body: {body[:200]})"
        )

    # 4xx/5xx — surface as T212OrderError with the request payload inline so
    # downstream handlers (BUY FAILED log in run_loop_t212, detection_funnel
    # drop record, Telegram alert) all carry the diagnostic data without
    # having to dig through container logs.
    payload_inline = f" payload={payload_str}" if payload_str else ""
    raise T212OrderError(
        f"T212 HTTP {resp.status_code} {resp.reason or ''} [{path}]:{payload_inline} body={body}"
    )


def _retry_after_seconds(resp: requests.Response) -> float | None:
    """Parse the Retry-After header (delta-seconds form) into a float.

    Returns None when the header is absent or in HTTP-date form (we fall back to
    escalating backoff in that case rather than parsing dates).
    """
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        secs = float(val)
    except (TypeError, ValueError):
        return None
    # Clamp to something sane so a hostile/huge header can't stall the loop.
    return max(0.0, min(secs, 120.0))


def _sanitise_error_body(text: str, max_len: int = 200) -> str:
    """Strip any auth-bearing line before logging.

    Previous regex only matched ONE token after the keyword, leaving
    multi-token auth values exposed (e.g. "Authorization: Basic <base64>"
    matched only "Basic", leaving the base64 in the log). The new regex
    redacts everything from the keyword to end-of-line — auth values
    don't legitimately span newlines, so this is safe.
    """
    import re
    # Greedy to end-of-line (. doesn't match \n by default).
    cleaned = re.sub(
        r"(?i)\b(authorization|bearer|api[_-]?key|api[_-]?secret|x-api-key)\b[:\s].*",
        "[REDACTED]",
        text,
    )
    return cleaned[:max_len]


# Payload keys that are safe to log. The only thing we ever POST to T212 is
# order JSON: {"ticker": ..., "quantity": ...} — both safe. The allowlist
# stops a future caller from accidentally smuggling a secret through the
# request payload into logs (defence in depth — the Basic Auth credentials
# travel in headers, never the body, so this is paranoia not necessity).
_LOG_PAYLOAD_KEYS = ("ticker", "quantity", "limitPrice", "stopPrice", "timeValidity")


def _sanitise_payload(payload: dict | None) -> str | None:
    """Format a request payload for logging, allowlisting safe keys."""
    if not payload:
        return None
    safe = {k: payload.get(k) for k in _LOG_PAYLOAD_KEYS if k in payload}
    extras = sorted(k for k in payload if k not in _LOG_PAYLOAD_KEYS)
    if extras:
        safe["_extra_keys"] = extras    # surface presence without values
    return str(safe)


def _to_t212_ticker(symbol: str, market: str = "us") -> str:
    """Convert plain ticker to Trading 212 instrument ID for the given market.

    US: AAPL       → AAPL_US_EQ
        AAPL_US_EQ → AAPL_US_EQ   (passthrough)
    UK: MKS.L      → MKSL_EQ      (strip .L, append L_EQ)
        MKS        → MKSL_EQ
        MKSL_EQ    → MKSL_EQ      (passthrough)

    Only alphanumerics, dot and underscore are allowed in the input —
    rejects anything else (path traversal / injection guard).

    Raises ValueError for unknown markets or invalid input.
    """
    import re
    market = (market or "us").lower()
    if market not in ("us", "uk"):
        raise ValueError(f"Unsupported market: {market!r}")

    raw = symbol.strip()
    symbol = raw.upper()
    # Allow ".L" for UK input; alphanumerics + underscore otherwise.
    if not re.fullmatch(r"[A-Z0-9_.]{1,20}", symbol):
        raise ValueError(f"Invalid ticker symbol: {symbol!r}")

    # Already a T212 instrument ID — passthrough PRESERVING case (LSE ids carry a
    # lowercase 'l', e.g. BARCl_EQ; upper-casing them would 404 on the order API).
    if symbol.endswith("_EQ"):
        return raw

    if market == "uk":
        # Accept "MKS" or "MKS.L"; both map to MKSl_EQ.
        # CRITICAL: T212's LSE suffix is a LOWERCASE 'l' (BARCl_EQ, VODl_EQ) —
        # verified from live demo positions. An uppercase 'L' 404s on the
        # (case-sensitive) order endpoint even though our catalogue lookup
        # (which upper-cases keys) appeared to match.
        if symbol.endswith(".L"):
            symbol = symbol[:-2]
        if "." in symbol:
            raise ValueError(f"Invalid UK ticker symbol: {symbol!r}")
        if not symbol:
            raise ValueError("Empty UK ticker after stripping .L")
        return f"{symbol}l_EQ"

    # US
    if "." in symbol:
        raise ValueError(f"Invalid US ticker symbol: {symbol!r}")
    return f"{symbol}_US_EQ"


def _from_t212_ticker(t212_ticker: str, market: str = "us") -> str:
    """Extract plain ticker from T212 instrument ID, market-aware.

    US: AAPL_US_EQ → AAPL
    UK: MKSL_EQ    → MKS.L   (strip L_EQ, append .L)
        VODL_EQ    → VOD.L
        BARCL_EQ   → BARC.L

    For unknown formats falls back to the leading alphanumeric run
    (US convention). Caller should not pass a foreign-market ID after
    the get_positions bulkhead.
    """
    market = (market or "us").lower()
    if market == "uk":
        # LSE suffix is a lowercase 'l' + '_EQ' (BARCl_EQ -> BARC.L).
        if t212_ticker.endswith("l_EQ"):
            core = t212_ticker[:-4]   # strip the 'l_EQ' suffix
            return f"{core.upper()}.L"
        # Unexpected format — best-effort
        return t212_ticker.split("_")[0].upper() + ".L"
    # US
    return t212_ticker.split("_")[0].upper()


def _matches_market(t212_ticker: str, market: str) -> bool:
    """True if the T212 instrument ID belongs to the given market.

    Used by get_positions() to filter positions to this client's market
    — the bulkhead that prevents the US service from ghost-reconciling
    a UK position (and vice versa) when both services share one T212
    account.

    Suffix conventions (verified empirically from T212's instrument
    catalogue):
      US:  {TICKER}_US_EQ        (e.g. AAPL_US_EQ)
      UK:  {TICKER}L_EQ          (e.g. MKSL_EQ, capital L, NO underscore
                                  between ticker and L)
      EU:  {TICKER}_XX_EQ        (e.g. SAP_DE_EQ, MC_FR_EQ, ASML_NL_EQ —
                                  always has an underscore before the
                                  two-letter country code)

    The UK and EU patterns both contain "L_EQ" if the country code ends
    in L (e.g. Netherlands ASML_NL_EQ). They are distinguished by the
    presence of an underscore between the ticker and the suffix: UK has
    NONE, EU has one. So the UK match is "ends with L_EQ AND no '_' in
    the ticker portion".

    Other markets match neither and are filtered out of both services.
    """
    market = (market or "us").lower()
    if not t212_ticker:
        return False
    if market == "us":
        return t212_ticker.endswith("_US_EQ")
    if market == "uk":
        # LSE tickers end with a LOWERCASE 'l' + '_EQ' (BARCl_EQ, VODl_EQ).
        # EU listings use an uppercase 2-letter code (ASML_NL_EQ) and won't match.
        if not t212_ticker.endswith("l_EQ"):
            return False
        core = t212_ticker[:-4]   # strip the 'l_EQ' suffix
        # If core has an underscore it's a foreign _XX_EQ (defensive).
        return bool(core) and "_" not in core
    return False


def _parse_order(data: dict, fallback_ticker: str) -> T212Order:
    return T212Order(
        id=str(data.get("id", "")),
        t212_ticker=data.get("ticker", fallback_ticker),
        status=data.get("status", "UNKNOWN"),
    )


def _safe_float(value: object, *, default: float = 0.0) -> float:
    """Convert API value to float, returning default on None / bad data."""
    if value is None:
        return default
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
