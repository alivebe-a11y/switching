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
# T212's public API is rate-limited PER ENDPOINT and is much stricter than a
# flat "10 req/s". We throttle each endpoint to a conservative minimum interval
# so bursts (e.g. several position lookups in one buy cycle) get staggered
# instead of fired at once. Verify the live numbers at docs.trading212.com —
# these are deliberately conservative.
_ENDPOINT_MIN_INTERVAL: dict[str, float] = {
    "/equity/account/summary": 2.0,
    "/equity/positions": 5.0,
    "/equity/orders/market": 2.0,
}
_DEFAULT_MIN_INTERVAL = 1.0      # any endpoint not listed above

# 429 (Too Many Requests) handling: respect Retry-After when present, else use
# an escalating backoff. After this many retries we give up and raise.
_MAX_RETRIES_429 = 4
_DEFAULT_BACKOFF_SECONDS = 5.0


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
            pnl_pct = (cur - avg) / avg if avg > 0 else 0.0
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
            log.debug(
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
    # Orders
    # ------------------------------------------------------------------

    def buy_market(self, symbol: str, quantity: float) -> T212Order:
        """Place a fractional market buy order in this client's market.

        Args:
            symbol:   Plain ticker e.g. "AAPL" (US) or "MKS.L" (UK).
            quantity: Number of shares (fractional OK). Must be > 0.

        Returns:
            T212Order with id, ticker, status.
        """
        t212_ticker = _to_t212_ticker(symbol, self.market)
        qty = round(abs(quantity), 4)
        if qty < 0.0001:
            raise T212OrderError(f"Quantity too small to buy: {qty} {symbol}")
        payload = {"ticker": t212_ticker, "quantity": qty}
        log.info("T212[%s] BUY %s qty=%.4f", self.market, t212_ticker, qty)
        data = self._post("/equity/orders/market", payload)
        return _parse_order(data, t212_ticker)

    def sell_all(self, symbol: str, quantity: float) -> T212Order:
        """Place a market sell order liquidating the full position.

        T212 uses a negative quantity to denote sells.

        Args:
            symbol:   Plain ticker e.g. "AAPL" (US) or "MKS.L" (UK).
            quantity: Positive number of shares currently held.

        Returns:
            T212Order with id, ticker, status.
        """
        t212_ticker = _to_t212_ticker(symbol, self.market)
        qty = round(abs(quantity), 4)
        if qty < 0.0001:
            raise T212OrderError(f"Quantity too small to sell: {qty} {symbol}")
        payload = {"ticker": t212_ticker, "quantity": -qty}
        log.info("T212[%s] SELL %s qty=%.4f", self.market, t212_ticker, qty)
        data = self._post("/equity/orders/market", payload)
        return _parse_order(data, t212_ticker)

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

            _check_response(resp, path)
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


def _check_response(resp: requests.Response, path: str) -> None:
    if resp.status_code == 401:
        raise T212AuthError(
            "Trading 212 API key invalid or expired. "
            "Regenerate at Settings → API in the app."
        )
    if resp.status_code == 400:
        # Sanitise response body before surfacing — T212 may echo request
        # fields (including auth headers) in error responses.
        body = _sanitise_error_body(resp.text, max_len=200)
        raise T212OrderError(f"T212 bad request [{path}]: {body}")
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        body = _sanitise_error_body(resp.text, max_len=150)
        raise T212OrderError(
            f"T212 HTTP error [{path}] {resp.status_code}: {body}"
        ) from exc


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
    """Strip any line that looks like an auth header before logging."""
    import re
    # Remove any "Authorization: ..." or "Bearer ..." fragments
    cleaned = re.sub(r"(?i)(authorization|bearer)\s*[:\s]\s*\S+", "[REDACTED]", text)
    return cleaned[:max_len]


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

    symbol = symbol.upper().strip()
    # Allow ".L" for UK input; alphanumerics + underscore otherwise.
    if not re.fullmatch(r"[A-Z0-9_.]{1,20}", symbol):
        raise ValueError(f"Invalid ticker symbol: {symbol!r}")

    # Already a T212 instrument ID — passthrough (caller should know what
    # they're doing; the bulkhead in get_positions handles cross-market leak).
    if symbol.endswith("_EQ"):
        return symbol

    if market == "uk":
        # Accept "MKS" or "MKS.L"; both map to MKSL_EQ
        if symbol.endswith(".L"):
            symbol = symbol[:-2]
        if "." in symbol:
            raise ValueError(f"Invalid UK ticker symbol: {symbol!r}")
        if not symbol:
            raise ValueError("Empty UK ticker after stripping .L")
        return f"{symbol}L_EQ"

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
        if t212_ticker.endswith("L_EQ"):
            core = t212_ticker[:-4]   # strip "L_EQ"
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
        if not t212_ticker.endswith("L_EQ"):
            return False
        core = t212_ticker[:-4]   # strip "L_EQ"
        # If core has an underscore it's a foreign _XX_EQ (e.g. _NL_EQ).
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
