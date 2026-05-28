"""Check whether specific tickers are valid T212 instrument IDs.

Calls GET /equity/metadata/instruments to get the full T212 instrument
catalogue and checks each requested ticker against it. This confirms
whether TICKER_US_EQ is the correct format or if T212 uses a different ID.

Usage (run on the server where T212 credentials are in .env):
    python scripts/check_t212_tickers.py
    python scripts/check_t212_tickers.py --tickers UTHR ESLT PODC ADC MIST

Reads T212_API_KEY, T212_API_SECRET, T212_DEMO from environment / .env
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Load .env from project root
_ROOT = Path(__file__).resolve().parent.parent
_ENV = _ROOT / ".env"
if _ENV.exists():
    for line in _ENV.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v

_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from switching.broker_trading212 import Trading212Client, T212AuthError  # noqa: E402

# The 5 tickers reported as missing from T212
DEFAULT_TICKERS = ["UTHR", "ESLT", "PODC", "ADC", "MIST"]


def check(tickers: list[str]) -> None:
    print("\n=== T212 Ticker Format Check ===\n")

    try:
        client = Trading212Client()
    except T212AuthError as e:
        print("AUTH FAILED: " + str(e))
        print("Check T212_API_KEY and T212_API_SECRET are set in .env")
        print("Run this script on the server where the .env lives")
        sys.exit(1)

    mode = "DEMO" if client.demo else "LIVE"
    print("Connected (" + mode + ")\n")

    # Fetch all instruments from T212
    print("Fetching instrument catalogue from T212 (may take a few seconds)...")
    try:
        instruments = client._get("/equity/metadata/instruments")
    except Exception as e:
        print("Failed to fetch instruments: " + str(e))
        print("Note: this endpoint may only be available on live accounts.")
        _check_via_account(client, tickers)
        return

    # instruments is a list of dicts with 'ticker', 'name', 'shortName', etc.
    if not isinstance(instruments, list):
        instruments = instruments.get("instruments", []) if isinstance(instruments, dict) else []

    print("Catalogue has " + str(len(instruments)) + " instruments.\n")

    # Build lookup: ticker -> instrument dict
    by_ticker: dict[str, dict] = {}
    for inst in instruments:
        t = inst.get("ticker", "")
        if t:
            by_ticker[t.upper()] = inst

    print("{:<8}  {:<20}  {:<8}  {}".format("Ticker", "US_EQ format", "Found?", "T212 name / note"))
    print("-" * 80)

    all_ok = True
    for sym in tickers:
        sym = sym.upper()
        t212_id = sym + "_US_EQ"

        if t212_id in by_ticker:
            inst = by_ticker[t212_id]
            name = inst.get("name", "") or inst.get("longName", "") or inst.get("shortName", "")
            print("  {:<8}  {:<20}  OK        {}".format(sym, t212_id, name))
        else:
            all_ok = False
            # Search for any ticker containing the symbol
            candidates = [t for t in by_ticker if sym in t]
            if candidates:
                note = "NOT _US_EQ -- candidates: " + str(candidates[:4])
            else:
                note = "NOT FOUND in T212 catalogue at all"
            print("  {:<8}  {:<20}  MISSING   {}".format(sym, t212_id, note))

    print()
    if all_ok:
        print("All tickers confirmed in T212 as TICKER_US_EQ format.")
        print("Ticker format is NOT the cause of missing trades.")
        print("Root cause was the DST gap (fixed in commit e60c84c).")
    else:
        print("One or more tickers NOT found as TICKER_US_EQ.")
        print("These would cause T212OrderError 'InstrumentNotFound' on every buy attempt.")
        print("They would appear in the detection funnel as t212_rejected.")
        print()
        print("Check the dashboard /api/drops for 't212_rejected' entries per ticker.")


def _check_via_account(client: Trading212Client, tickers: list[str]) -> None:
    """Fallback: verify auth works by fetching account, then show what we know."""
    print("\nFallback: checking account connectivity...")
    try:
        acct = client.get_account()
        cs = "GBP" if acct.currency == "GBP" else "USD"
        print("  Account OK: free=" + cs + str(round(acct.free, 2)) +
              "  total=" + cs + str(round(acct.total, 2)))
        print("  Auth is working.")
        print("  Cannot verify ticker format without instruments endpoint.")
        print("  Standard T212 convention for US stocks: TICKER_US_EQ")
        print()
        for sym in tickers:
            sym = sym.upper()
            print("  " + sym + " -> " + sym + "_US_EQ  (assumed correct)")
    except Exception as e:
        print("  Account fetch also failed: " + str(e))
        print("  The T212 service cannot reach the API -- check connectivity and keys.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check T212 ticker format for given symbols")
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS,
                        help="Symbols to check (default: UTHR ESLT PODC ADC MIST)")
    args = parser.parse_args()
    check(args.tickers)
