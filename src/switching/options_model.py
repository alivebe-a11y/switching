"""Black-Scholes options P&L modelling for strategy comparison.

Answers the question: if we had bought near-the-money call options instead of
stock on the same signals, what would our P&L have been?

No live options chain data is required — we estimate entry premium via
Black-Scholes with a user-supplied assumed IV, then revalue at exit using the
actual stock price move recorded in each ClosedTrade.

Limitations (be aware when interpreting results):
  - Assumes European-style pricing (no early exercise premium vs American)
  - IV is constant and user-supplied — real IV varies by strike, expiry, and date
  - Uses theoretical mid-price (no bid-ask spread, no slippage)
  - Does not model dividends, early assignment, or pin risk

Typical usage::

    from switching.options_model import compare_options_vs_stock
    from switching.paper_trader import Portfolio

    p = Portfolio.load(path)
    result = compare_options_vs_stock(p.trades, assumed_iv=0.35, dte=14)
    print(f"Stock total P&L  : ${result.total_stock_pnl:+.2f}")
    print(f"Options total P&L: ${result.total_options_pnl:+.2f}")
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime


# ---------------------------------------------------------------------------
# Core Black-Scholes maths
# ---------------------------------------------------------------------------


def _norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function via math.erf."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def black_scholes_call(
    S: float,
    K: float,
    T: float,
    r: float = 0.05,
    sigma: float = 0.30,
) -> float:
    """Black-Scholes European call option price.

    Args:
        S:     Underlying spot price.
        K:     Strike price.
        T:     Time to expiry in **years** (e.g. ``14/365`` for a 2-week option).
        r:     Continuously-compounded risk-free rate (default 5 %).
        sigma: Implied volatility, annualised (e.g. ``0.30`` for 30 %).

    Returns:
        Call premium per share in the same currency as *S* and *K*.
        Returns intrinsic value ``max(0, S - K)`` when *T* ≤ 0.
    """
    if T <= 0.0:
        return max(0.0, S - K)
    if sigma <= 0.0 or S <= 0.0 or K <= 0.0:
        return max(0.0, S - K * math.exp(-r * T))
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


# ---------------------------------------------------------------------------
# Trade-level result
# ---------------------------------------------------------------------------


@dataclass
class OptionTradeResult:
    """Side-by-side comparison for one actual stock trade vs a modelled ATM call."""

    ticker: str
    detector: str
    exit_reason: str
    entry_price: float
    exit_price: float
    cost_basis: float            # actual dollars committed to the stock trade
    hold_days: int

    # Actual stock outcome (from the ClosedTrade record)
    stock_pnl: float
    stock_pct_return: float

    # Hypothetical options outcome (same $ allocated to call premium)
    option_pnl: float
    option_pct_return: float
    premium_per_share: float     # ATM call premium at entry (Black-Scholes)
    exit_value_per_share: float  # option value at exit (Black-Scholes)

    assumed_iv: float
    dte_entry: int               # days to expiry when the option was entered


# ---------------------------------------------------------------------------
# Portfolio-level result
# ---------------------------------------------------------------------------


@dataclass
class OptionsCompareResult:
    """Portfolio-level comparison: actual stock trades vs hypothetical options."""

    trades: list[OptionTradeResult] = field(default_factory=list)
    assumed_iv: float = 0.30
    dte: int = 14

    # ---- aggregates -------------------------------------------------------

    @property
    def total_stock_pnl(self) -> float:
        return sum(t.stock_pnl for t in self.trades)

    @property
    def total_options_pnl(self) -> float:
        return sum(t.option_pnl for t in self.trades)

    @property
    def stock_wins(self) -> int:
        return sum(1 for t in self.trades if t.stock_pnl > 0)

    @property
    def options_wins(self) -> int:
        return sum(1 for t in self.trades if t.option_pnl > 0)

    @property
    def stock_win_rate(self) -> float:
        return self.stock_wins / len(self.trades) if self.trades else 0.0

    @property
    def options_win_rate(self) -> float:
        return self.options_wins / len(self.trades) if self.trades else 0.0

    @property
    def options_better_count(self) -> int:
        """Number of trades where options returned more than stock."""
        return sum(1 for t in self.trades if t.option_pnl > t.stock_pnl)

    @property
    def stock_better_count(self) -> int:
        return sum(1 for t in self.trades if t.stock_pnl >= t.option_pnl)

    def by_detector(self) -> dict[str, dict]:
        """Aggregate comparison per detector (sorted by options P&L delta)."""
        groups: dict[str, dict] = {}
        for t in self.trades:
            g = groups.setdefault(t.detector, {
                "trades": 0,
                "stock_pnl": 0.0,
                "options_pnl": 0.0,
                "stock_wins": 0,
                "options_wins": 0,
            })
            g["trades"] += 1
            g["stock_pnl"] += t.stock_pnl
            g["options_pnl"] += t.option_pnl
            if t.stock_pnl > 0:
                g["stock_wins"] += 1
            if t.option_pnl > 0:
                g["options_wins"] += 1
        for g in groups.values():
            n = g["trades"]
            g["stock_win_rate"] = round(g["stock_wins"] / n, 3) if n else 0.0
            g["options_win_rate"] = round(g["options_wins"] / n, 3) if n else 0.0
            g["stock_pnl"] = round(g["stock_pnl"], 2)
            g["options_pnl"] = round(g["options_pnl"], 2)
        return groups


# ---------------------------------------------------------------------------
# Modelling helpers
# ---------------------------------------------------------------------------


def _infer_hold_days(entry_dt: str, exit_dt: str) -> int:
    """Calendar days held, derived from ISO datetime strings."""
    try:
        e = datetime.fromisoformat(entry_dt.replace("Z", "+00:00"))
        x = datetime.fromisoformat(exit_dt.replace("Z", "+00:00"))
        return max(1, (x.date() - e.date()).days)
    except Exception:
        return 3  # safe fallback


def model_call_trade(
    *,
    ticker: str,
    detector: str,
    exit_reason: str,
    entry_price: float,
    exit_price: float,
    cost_basis: float,
    entry_dt: str,
    exit_dt: str,
    assumed_iv: float = 0.30,
    dte: int = 14,
    risk_free_rate: float = 0.05,
) -> OptionTradeResult:
    """Model an ATM call option trade using the same capital as the stock trade.

    We buy ATM calls (strike = entry_price) expiring in *dte* days.  The
    number of notional option shares is ``cost_basis / premium``.  At exit we
    revalue using Black-Scholes with the actual stock price and remaining time.

    This gives a fair capital-efficiency comparison — same dollars in, very
    different risk/return profile because of leverage and time decay.
    """
    hold_days = _infer_hold_days(entry_dt, exit_dt)
    T_entry = dte / 365.0
    T_exit = max(0.0, (dte - hold_days) / 365.0)

    premium = black_scholes_call(entry_price, entry_price, T_entry, risk_free_rate, assumed_iv)
    exit_val = black_scholes_call(exit_price, entry_price, T_exit, risk_free_rate, assumed_iv)

    if premium > 0:
        shares_covered = cost_basis / premium   # notional shares
        option_pnl = (exit_val - premium) * shares_covered
        option_pct = exit_val / premium - 1.0
    else:
        shares_covered = 0.0
        option_pnl = -cost_basis  # total loss if premium is degenerate/zero
        option_pct = -1.0

    stock_shares = cost_basis / entry_price if entry_price > 0 else 0.0
    stock_pnl = (exit_price - entry_price) * stock_shares
    stock_pct = exit_price / entry_price - 1.0 if entry_price > 0 else 0.0

    return OptionTradeResult(
        ticker=ticker,
        detector=detector,
        exit_reason=exit_reason,
        entry_price=entry_price,
        exit_price=exit_price,
        cost_basis=cost_basis,
        hold_days=hold_days,
        stock_pnl=round(stock_pnl, 2),
        stock_pct_return=round(stock_pct, 4),
        option_pnl=round(option_pnl, 2),
        option_pct_return=round(option_pct, 4),
        premium_per_share=round(premium, 4),
        exit_value_per_share=round(exit_val, 4),
        assumed_iv=assumed_iv,
        dte_entry=dte,
    )


# ---------------------------------------------------------------------------
# Main comparison entry point
# ---------------------------------------------------------------------------


def compare_options_vs_stock(
    trades,  # Sequence[ClosedTrade] — avoid circular import by not type-hinting
    *,
    assumed_iv: float = 0.30,
    dte: int = 14,
    risk_free_rate: float = 0.05,
    detectors: set[str] | None = None,
) -> OptionsCompareResult:
    """Compare what options would have returned vs the actual stock trades.

    Args:
        trades:        List of ``ClosedTrade`` objects from ``Portfolio.trades``.
        assumed_iv:    Implied volatility to assume (0.30 = 30 %, typical for
                       large-cap US stocks). Use higher values (0.50+) for
                       biotech / small caps.
        dte:           Days to expiry when the option is entered (14 = 2-week,
                       30 = monthly).  Shorter DTE = more gamma, more decay.
        risk_free_rate: Risk-free rate for Black-Scholes (default 5 %).
        detectors:     If given, only include trades from these detectors.

    Returns:
        An :class:`OptionsCompareResult` with per-trade and aggregate comparisons.
    """
    result = OptionsCompareResult(assumed_iv=assumed_iv, dte=dte)

    for t in trades:
        if detectors and t.detector not in detectors:
            continue
        cost_basis = t.entry_price * t.shares
        if cost_basis <= 0 or t.entry_price <= 0:
            continue
        item = model_call_trade(
            ticker=t.ticker,
            detector=t.detector,
            exit_reason=t.exit_reason,
            entry_price=t.entry_price,
            exit_price=t.exit_price,
            cost_basis=cost_basis,
            entry_dt=t.entry_dt,
            exit_dt=t.exit_dt,
            assumed_iv=assumed_iv,
            dte=dte,
            risk_free_rate=risk_free_rate,
        )
        result.trades.append(item)

    return result
