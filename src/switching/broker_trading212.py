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

Ticker format: Trading 212 uses "AAPL_US_EQ" internally.
This client accepts plain symbols ("AAPL") and converts automatically.

Rate limits (T212 docs):
  - 10 req/s general
  - 1 req/s for POST /session
  - 1 per 100ms for order creation

Reference: https://docs.trading212.com/api
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from datetime import time as dt_time

import requests

log = logging.getLogger(__name__)

_DEMO_BASE = "https://demo.trading212.com/api/v0"
_LIVE_BASE = "https://live.trading212.com/api/v0"


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


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class Trading212Client:
    """Minimal Trading 212 REST client.

    Only the methods needed by the paper-trading loop are implemented:
    get_account, get_positions, buy_market, sell_all, is_market_open.
    """

    def __init__(self) -> None:
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
        # Never log the key itself — only mode and base URL
        mode = "DEMO" if self.demo else "LIVE"
        log.info("Trading212Client initialised (%s) base=%s", mode, self._base)
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
        )

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_positions(self) -> list[T212Position]:
        """Return all open positions.

        The endpoint is paginated but for typical retail portfolios a single
        page (up to 50 items) is sufficient.  We fetch page 1 only; extend
        if needed.
        """
        data = self._get("/equity/positions")
        # Response is either a bare list or {"items": [...], "nextPagePath": ...}
        items: list[dict] = data if isinstance(data, list) else data.get("items", [])
        result: list[T212Position] = []
        for item in items:
            # ticker is nested: item["instrument"]["ticker"] e.g. "AAPL_US_EQ"
            instrument = item.get("instrument") or {}
            t212_ticker = instrument.get("ticker", "")
            symbol = _from_t212_ticker(t212_ticker)
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
        """Place a fractional market buy order.

        Args:
            symbol:   Plain ticker e.g. "AAPL".
            quantity: Number of shares (fractional OK). Must be > 0.

        Returns:
            T212Order with id, ticker, status.
        """
        t212_ticker = _to_t212_ticker(symbol)
        qty = round(abs(quantity), 4)
        if qty < 0.0001:
            raise T212OrderError(f"Quantity too small to buy: {qty} {symbol}")
        payload = {"ticker": t212_ticker, "quantity": qty}
        log.info("T212 BUY %s qty=%.4f", t212_ticker, qty)
        data = self._post("/equity/orders/market", payload)
        return _parse_order(data, t212_ticker)

    def sell_all(self, symbol: str, quantity: float) -> T212Order:
        """Place a market sell order liquidating the full position.

        T212 uses a negative quantity to denote sells.

        Args:
            symbol:   Plain ticker e.g. "AAPL".
            quantity: Positive number of shares currently held.

        Returns:
            T212Order with id, ticker, status.
        """
        t212_ticker = _to_t212_ticker(symbol)
        qty = round(abs(quantity), 4)
        if qty < 0.0001:
            raise T212OrderError(f"Quantity too small to sell: {qty} {symbol}")
        payload = {"ticker": t212_ticker, "quantity": -qty}
        log.info("T212 SELL %s qty=%.4f", t212_ticker, qty)
        data = self._post("/equity/orders/market", payload)
        return _parse_order(data, t212_ticker)

    # ------------------------------------------------------------------
    # Market hours
    # ------------------------------------------------------------------

    def is_market_open(self) -> bool:
        """Return True during NYSE core session Mon-Fri 14:30–21:00 UTC.

        Trading 212 has no dedicated market-hours endpoint, so we use the
        NYSE schedule.  Pre-market / after-hours orders are not supported
        by the Invest account type.
        """
        now = datetime.now(tz=timezone.utc)
        if now.weekday() >= 5:        # Saturday=5, Sunday=6
            return False
        t = now.time()
        return dt_time(14, 30) <= t < dt_time(21, 0)

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str) -> dict | list:
        url = self._base + path
        resp = self._session.get(url, timeout=15)
        _check_response(resp, path)
        return resp.json()

    def _post(self, path: str, payload: dict) -> dict:
        url = self._base + path
        resp = self._session.post(url, json=payload, timeout=15)
        _check_response(resp, path)
        return resp.json()


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


def _sanitise_error_body(text: str, max_len: int = 200) -> str:
    """Strip any line that looks like an auth header before logging."""
    import re
    # Remove any "Authorization: ..." or "Bearer ..." fragments
    cleaned = re.sub(r"(?i)(authorization|bearer)\s*[:\s]\s*\S+", "[REDACTED]", text)
    return cleaned[:max_len]


def _to_t212_ticker(symbol: str) -> str:
    """Convert plain ticker to Trading 212 instrument ID.

    AAPL      → AAPL_US_EQ
    AAPL_US_EQ → AAPL_US_EQ  (passthrough if already formatted)

    Only alphanumerics and underscores are allowed — rejects anything
    that could be a path traversal or injection attempt.
    """
    import re
    symbol = symbol.upper().strip()
    if not re.fullmatch(r"[A-Z0-9_]{1,20}", symbol):
        raise ValueError(f"Invalid ticker symbol: {symbol!r}")
    if "_" in symbol:
        return symbol
    return f"{symbol}_US_EQ"


def _from_t212_ticker(t212_ticker: str) -> str:
    """Extract plain ticker from T212 instrument ID.

    AAPL_US_EQ → AAPL
    """
    return t212_ticker.split("_")[0].upper()


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
