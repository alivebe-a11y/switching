"""Alpaca Markets broker integration.

Uses Alpaca's REST API directly (no SDK) to avoid dependency conflicts.

Required env vars:
  ALPACA_API_KEY      — API key from alpaca.markets
  ALPACA_SECRET_KEY   — secret key
  ALPACA_PAPER        — "true" (default) for paper trading, "false" for live

Alpaca paper-trading uses a separate base URL from live trading. Both
use the same API shape.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

_PAPER_URL = "https://paper-api.alpaca.markets"
_LIVE_URL = "https://api.alpaca.markets"
_DATA_URL = "https://data.alpaca.markets"


class AlpacaAuthError(Exception):
    pass


@dataclass(frozen=True)
class AlpacaAccount:
    cash: float
    portfolio_value: float
    buying_power: float
    equity: float


@dataclass(frozen=True)
class AlpacaPosition:
    ticker: str
    qty: float
    avg_entry_price: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float


@dataclass(frozen=True)
class AlpacaOrder:
    id: str
    ticker: str
    qty: float
    side: str
    type: str
    status: str
    filled_avg_price: float | None
    submitted_at: str


class AlpacaClient:
    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        paper: bool | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("ALPACA_API_KEY", "")
        self.secret_key = secret_key or os.environ.get("ALPACA_SECRET_KEY", "")
        if not self.api_key or not self.secret_key:
            raise AlpacaAuthError(
                "Set ALPACA_API_KEY and ALPACA_SECRET_KEY env vars. "
                "Sign up free at https://alpaca.markets"
            )
        if paper is None:
            paper = os.environ.get("ALPACA_PAPER", "true").lower() != "false"
        self.paper = paper
        self.base_url = _PAPER_URL if paper else _LIVE_URL
        self._session = requests.Session()
        self._session.headers.update({
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
        })

    def _get(self, path: str, **kwargs) -> dict | list:
        r = self._session.get(f"{self.base_url}{path}", **kwargs)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, json_data: dict) -> dict:
        r = self._session.post(f"{self.base_url}{path}", json=json_data)
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str) -> dict | None:
        r = self._session.delete(f"{self.base_url}{path}")
        r.raise_for_status()
        if r.content:
            return r.json()
        return None

    def get_account(self) -> AlpacaAccount:
        data = self._get("/v2/account")
        return AlpacaAccount(
            cash=float(data["cash"]),
            portfolio_value=float(data["portfolio_value"]),
            buying_power=float(data["buying_power"]),
            equity=float(data["equity"]),
        )

    def get_positions(self) -> list[AlpacaPosition]:
        data = self._get("/v2/positions")
        return [
            AlpacaPosition(
                ticker=p["symbol"],
                qty=float(p["qty"]),
                avg_entry_price=float(p["avg_entry_price"]),
                current_price=float(p["current_price"]),
                market_value=float(p["market_value"]),
                unrealized_pnl=float(p["unrealized_pl"]),
                unrealized_pnl_pct=float(p["unrealized_plpc"]),
            )
            for p in data
        ]

    def get_position(self, ticker: str) -> AlpacaPosition | None:
        try:
            p = self._get(f"/v2/positions/{ticker}")
            return AlpacaPosition(
                ticker=p["symbol"],
                qty=float(p["qty"]),
                avg_entry_price=float(p["avg_entry_price"]),
                current_price=float(p["current_price"]),
                market_value=float(p["market_value"]),
                unrealized_pnl=float(p["unrealized_pl"]),
                unrealized_pnl_pct=float(p["unrealized_plpc"]),
            )
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise

    def buy_market(self, ticker: str, notional: float) -> AlpacaOrder:
        data = self._post("/v2/orders", {
            "symbol": ticker,
            "notional": str(round(notional, 2)),
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
        })
        return _parse_order(data)

    def buy_shares(self, ticker: str, qty: float) -> AlpacaOrder:
        data = self._post("/v2/orders", {
            "symbol": ticker,
            "qty": str(qty),
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
        })
        return _parse_order(data)

    def sell_all(self, ticker: str) -> AlpacaOrder:
        pos = self.get_position(ticker)
        if pos is None:
            raise ValueError(f"no position in {ticker}")
        data = self._post("/v2/orders", {
            "symbol": ticker,
            "qty": str(pos.qty),
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
        })
        return _parse_order(data)

    def sell_stop(self, ticker: str, qty: float, stop_price: float) -> AlpacaOrder:
        data = self._post("/v2/orders", {
            "symbol": ticker,
            "qty": str(qty),
            "side": "sell",
            "type": "stop",
            "stop_price": str(round(stop_price, 2)),
            "time_in_force": "gtc",
        })
        return _parse_order(data)

    def cancel_orders_for(self, ticker: str) -> int:
        orders = self._get("/v2/orders", params={"status": "open", "symbols": ticker})
        cancelled = 0
        for o in orders:
            try:
                self._delete(f"/v2/orders/{o['id']}")
                cancelled += 1
            except requests.HTTPError:
                pass
        return cancelled

    def get_quote(self, ticker: str) -> float | None:
        try:
            r = self._session.get(
                f"{_DATA_URL}/v2/stocks/{ticker}/quotes/latest",
                headers={
                    "APCA-API-KEY-ID": self.api_key,
                    "APCA-API-SECRET-KEY": self.secret_key,
                },
            )
            r.raise_for_status()
            data = r.json()
            ask = float(data["quote"]["ap"])
            bid = float(data["quote"]["bp"])
            return (ask + bid) / 2 if ask > 0 and bid > 0 else None
        except Exception as exc:
            log.warning("quote failed for %s: %s", ticker, exc)
            return None

    def is_market_open(self) -> bool:
        data = self._get("/v2/clock")
        return data.get("is_open", False)


def _parse_order(data: dict) -> AlpacaOrder:
    return AlpacaOrder(
        id=data["id"],
        ticker=data["symbol"],
        qty=float(data.get("qty") or data.get("notional") or 0),
        side=data["side"],
        type=data["type"],
        status=data["status"],
        filled_avg_price=float(data["filled_avg_price"]) if data.get("filled_avg_price") else None,
        submitted_at=data["submitted_at"],
    )
