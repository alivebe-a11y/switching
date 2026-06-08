#!/usr/bin/env python3
"""Reddit mention-momentum backtest (Option A) — does a WSB attention spike pay?

The decisive Reddit experiment. Needs NO good individual callers — it tests the
crowd: when mentions of a ticker SPIKE, what does the stock do over the next few
days, and is there a rideable window before the reversal?

Pipeline (all streaming / stdlib only, runs in a bare python:3.11-slim container):
  1. Read the dump folder(s) -> per-ticker daily mention counts (upvote-weighted,
     bot-filtered, crypto/slang-filtered; optional --tickers whitelist).
  2. Detect SPIKE days per ticker (count >= --min-mentions AND >= --spike-ratio x
     trailing mean over --baseline-days).
  3. Price each spiked ticker from Stooq (free daily OHLC CSV, cached). Fake/crypto
     tickers return no data and self-drop -> acts as a real-ticker filter.
  4. Forward returns from the NEXT trading day's OPEN (realistic: you only know the
     day's full mention count after it closes) at +1/+3/+5/+10 days, plus the
     max-favorable (peak) and max-adverse (trough) over 10 days.
  5. Compare spike-day forward returns to an all-days baseline for the same tickers
     -> the EXCESS return attributable to the spike. Print a verdict.

Usage (server, throwaway container):
  docker run --rm -it -v /mnt/Pool_1/Configs/reddit:/data python:3.11-slim \
    python /data/reddit_momentum_backtest.py /data/discovery \
      --price-cache /data/prices --out /data/momentum_report.txt

  # tighter spikes only, or a whitelist for precision:
  ... --min-mentions 8 --spike-ratio 4 --tickers /data/us_tickers.txt

Honest caveats baked into the read: a backtest FLATTERS this (survivorship — dead
microcaps missing from Stooq; no slippage/liquidity; close-to-open fills assumed
clean). Real pump names are illiquid and gap through stops. Treat a positive result
as a ceiling, not a promise.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

_CASHTAG_RX = re.compile(r"\$([A-Za-z]{1,5})\b")
_DUMP_EXTS = (".jsonl", ".ndjson", ".json", ".zst")
_BOT_NAMES = {"automoderator", "wsbapp", "visualmod", "remindmebot"}
_NOISE = {
    "BTC", "ETH", "XRP", "BNB", "SOL", "ADA", "DOGE", "SHIB", "DOT", "LINK", "UNI",
    "SUSHI", "CRV", "AAVE", "COMP", "MKR", "REN", "BAL", "OP", "ARB", "PEPE", "FTM",
    "MATIC", "AVAX", "LTC", "XLM", "ATOM", "ALGO", "NEAR", "FIL", "ICP", "HBAR",
    "VET", "SAND", "MANA", "BCH", "TRX", "EOS", "XMR", "USDT", "USDC", "DAI",
    "YOLO", "FD", "FDS", "OTM", "ITM", "IV", "ATH", "ATL", "EOD", "EOW", "FOMO",
    "HODL", "WSB", "CPI", "FED", "GDP", "PPI", "IPO", "USD", "EPS", "PE", "ROPE",
    "DJI", "DJIA", "DJT", "DJTA", "SPX", "XX", "XXX",  # index/noise/placeholders
}
_HORIZONS = (1, 3, 5, 10)
_WINDOW = 10  # days for peak/trough


# ---------------------------------------------------------------------------
# dump reading
# ---------------------------------------------------------------------------
def read_lines(path: str):
    if path.endswith(".zst"):
        import zstandard
        with open(path, "rb") as fh:
            r = zstandard.ZstdDecompressor(max_window_size=2 ** 31).stream_reader(fh)
            buf = ""
            while True:
                chunk = r.read(2 ** 27).decode("utf-8", errors="ignore")
                if not chunk:
                    break
                lines = (buf + chunk).split("\n")
                for ln in lines[:-1]:
                    yield ln
                buf = lines[-1]
            if buf:
                yield buf
            r.close()
        return
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for ln in fh:
            yield ln


def expand(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for n in sorted(os.listdir(p)):
                if n.lower().endswith(_DUMP_EXTS):
                    out.append(os.path.join(p, n))
        else:
            out.append(p)
    return out


def is_bot(a: str) -> bool:
    al = a.lower()
    return al in _BOT_NAMES or al.endswith("bot") or "moderator" in al


# ---------------------------------------------------------------------------
# stooq prices (free daily OHLC CSV, cached)
# ---------------------------------------------------------------------------
def fetch_prices(ticker: str, cache_dir: str, sleep: float):
    """Return ordered list of (date, open, high, low, close) or [] if unavailable.
    Cached to <cache_dir>/<TICKER>.csv (a literal 'NODATA' marker file if Stooq has none)."""
    safe = re.sub(r"[^A-Za-z0-9]", "", ticker).upper()
    if not safe:
        return []
    path = os.path.join(cache_dir, f"{safe}.csv")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            head = fh.readline()
            if head.startswith("NODATA"):
                return []
        return _parse_price_csv(path)
    url = f"https://stooq.com/q/d/l/?s={safe.lower()}.us&i=d"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "switching-research/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
    except (urllib.error.URLError, TimeoutError):
        text = ""
    time.sleep(sleep)
    if (not text) or ("Date,Open" not in text) or ("N/D" in text[:50]):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("NODATA\n")
        return []
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return _parse_price_csv(path)


def _parse_price_csv(path):
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                rows.append((
                    datetime.strptime(r["Date"], "%Y-%m-%d").date(),
                    float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"]),
                ))
            except (KeyError, ValueError):
                continue
    rows.sort(key=lambda x: x[0])
    return rows


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reddit mention-momentum forward-return backtest.")
    ap.add_argument("paths", nargs="+", help="dump files/folders (posts and/or comments)")
    ap.add_argument("--price-cache", required=True, help="dir to cache Stooq CSVs")
    ap.add_argument("--tickers", default=None, help="optional whitelist file (UPPER, one per line)")
    ap.add_argument("--min-mentions", type=int, default=5, help="min mentions on a day to be a spike")
    ap.add_argument("--spike-ratio", type=float, default=3.0, help="day count >= ratio x trailing mean")
    ap.add_argument("--baseline-days", type=int, default=20, help="trailing calendar days for the mean")
    ap.add_argument("--min-price", type=float, default=1.0, help="skip spikes where entry price < this")
    ap.add_argument("--weight", choices=["count", "upvotes"], default="count",
                    help="spike metric: raw mention count or upvote-weighted")
    ap.add_argument("--sleep", type=float, default=0.3, help="seconds between Stooq fetches")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    os.makedirs(args.price_cache, exist_ok=True)

    whitelist = None
    if args.tickers:
        with open(args.tickers, "r", encoding="utf-8", errors="ignore") as fh:
            whitelist = {ln.strip().upper() for ln in fh if ln.strip()}

    def tickers_in(text: str):
        out = set()
        for raw in _CASHTAG_RX.findall(text):
            t = raw.upper()
            if whitelist is not None:
                if t in whitelist:
                    out.add(t)
            elif t not in _NOISE:
                out.add(t)
        return out

    # ---- Pass 1: per-ticker daily mention metric -------------------------
    # mentions[ticker][date] = float (count or upvote-weighted)
    mentions: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    total_items = 0
    files = expand(args.paths)
    print(f"reading {len(files)} file(s)...", flush=True)
    for path in files:
        for line in read_lines(path):
            if not line.strip():
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            total_items += 1
            author = (o.get("author") or "").strip()
            if not author or author in ("[deleted]", "[removed]") or is_bot(author):
                continue
            created = o.get("created_utc")
            try:
                d = datetime.fromtimestamp(int(float(created)), tz=timezone.utc).date()
            except (TypeError, ValueError):
                continue
            text = (o.get("title") or "") + " " + (o.get("selftext") or "") + " " + (o.get("body") or "")
            tks = tickers_in(text)
            if not tks:
                continue
            w = 1.0 if args.weight == "count" else float(max(int(o.get("score") or 0), 1))
            for t in tks:
                mentions[t][d] += w
            if total_items % 1_000_000 == 0:
                print(f"  ...{total_items:,} items", flush=True)

    print(f"parsed {total_items:,} items; {len(mentions):,} candidate tickers", flush=True)

    # ---- Pass 2: spike detection ----------------------------------------
    spikes: list[tuple[str, date, float]] = []  # (ticker, day, magnitude vs baseline)
    for t, series in mentions.items():
        days = sorted(series)
        for d in days:
            c = series[d]
            if c < args.min_mentions:
                continue
            base = sum(series.get(d - timedelta(days=i), 0.0)
                       for i in range(1, args.baseline_days + 1)) / args.baseline_days
            if c >= args.spike_ratio * max(base, 0.1):
                spikes.append((t, d, c / max(base, 0.1)))
    spikes.sort()
    spiked_tickers = sorted({t for t, _, _ in spikes})
    print(f"{len(spikes):,} spike-days across {len(spiked_tickers):,} tickers; pricing...", flush=True)

    # ---- Pass 3: prices + forward returns -------------------------------
    price_index: dict[str, tuple] = {}  # ticker -> (rows, {date: idx})
    priced = 0
    for i, t in enumerate(spiked_tickers):
        rows = fetch_prices(t, args.price_cache, args.sleep)
        if rows:
            price_index[t] = (rows, {r[0]: idx for idx, r in enumerate(rows)})
            priced += 1
        if (i + 1) % 50 == 0:
            print(f"  priced {i+1}/{len(spiked_tickers)} ({priced} have data)", flush=True)

    def entry_idx_after(rows_dates_idx, d: date):
        rows, _ = rows_dates_idx
        # first trading row strictly AFTER day d
        lo, hi = 0, len(rows)
        while lo < hi:
            mid = (lo + hi) // 2
            if rows[mid][0] <= d:
                lo = mid + 1
            else:
                hi = mid
        return lo if lo < len(rows) else None

    # spike-day forward returns
    fwd = {h: [] for h in _HORIZONS}
    peaks, troughs = [], []
    used_events = 0
    for t, d, _mag in spikes:
        pi = price_index.get(t)
        if not pi:
            continue
        rows, _ = pi
        e = entry_idx_after(pi, d)
        if e is None or e + _WINDOW >= len(rows):   # need full forward window (maturity buffer)
            continue
        entry = rows[e][1]  # open
        if entry < args.min_price:
            continue
        used_events += 1
        for h in _HORIZONS:
            fwd[h].append(rows[e + h - 1][4] / entry - 1.0)        # close after h days
        window = rows[e:e + _WINDOW]
        peaks.append(max(r[2] for r in window) / entry - 1.0)      # max high
        troughs.append(min(r[3] for r in window) / entry - 1.0)    # min low

    # all-days baseline (same priced tickers): mean +5d close-to-close drift
    base5 = []
    for t, (rows, _idx) in price_index.items():
        for e in range(0, len(rows) - 5):
            base5.append(rows[e + 5][4] / rows[e][4] - 1.0)

    # ---- report ----------------------------------------------------------
    def stats(xs):
        if not xs:
            return (0, 0.0, 0.0, 0.0)
        return (len(xs), statistics.mean(xs) * 100, statistics.median(xs) * 100,
                sum(1 for x in xs if x > 0) / len(xs) * 100)

    L = []
    L.append("#" * 72)
    L.append("Reddit mention-momentum backtest")
    L.append(f"weight={args.weight}  min_mentions={args.min_mentions}  spike_ratio={args.spike_ratio}  "
             f"baseline_days={args.baseline_days}  min_price=${args.min_price}")
    L.append("#" * 72)
    L.append(f"items parsed        : {total_items:,}")
    L.append(f"spike-days detected : {len(spikes):,} across {len(spiked_tickers):,} tickers")
    L.append(f"tickers with prices : {priced:,}/{len(spiked_tickers):,}")
    L.append(f"usable spike events : {used_events:,}  (priced, >${args.min_price}, full {_WINDOW}d forward)")
    L.append("")
    L.append("Forward return from NEXT-DAY OPEN after a spike:")
    L.append(f"  {'horizon':<10}{'n':>8}{'mean%':>9}{'median%':>9}{'win%':>8}")
    for h in _HORIZONS:
        n, mean, med, win = stats(fwd[h])
        L.append(f"  +{h}d{'':<7}{n:>8}{mean:>9.2f}{med:>9.2f}{win:>8.1f}")
    pk = stats(peaks); tr = stats(troughs)
    L.append("")
    L.append(f"Over {_WINDOW} days after entry:")
    L.append(f"  mean PEAK (max-favorable) : {pk[1]:+.2f}%   <- the rideable upside")
    L.append(f"  mean TROUGH (max-adverse) : {tr[1]:+.2f}%   <- the gap/dump risk")
    bn, bmean, bmed, bwin = stats(base5)
    s5 = stats(fwd[5])
    L.append("")
    L.append("Baseline (same tickers, ALL days, +5d close-to-close):")
    L.append(f"  baseline +5d mean : {bmean:+.2f}%   (n={bn:,})")
    L.append(f"  spike    +5d mean : {s5[1]:+.2f}%   (n={s5[0]:,})")
    L.append(f"  EXCESS (spike - baseline) : {s5[1]-bmean:+.2f}%  <- the actual edge (if any)")
    L.append("")
    edge = s5[1] - bmean
    if used_events < 100:
        L.append("VERDICT: too few usable events to conclude — widen the data (comments / more subs).")
    elif edge > 0.5 and s5[3] > 52:
        L.append(f"VERDICT: POSITIVE signal (+{edge:.2f}% excess, {s5[3]:.0f}% win) — but discount HEAVILY")
        L.append("         for survivorship + slippage + illiquid gap-through-stops before trusting it.")
    else:
        L.append(f"VERDICT: NO usable edge ({edge:+.2f}% excess vs baseline) — you're late / it's noise.")

    report = "\n".join(L)
    print("\n" + report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        print(f"\n[report written to {args.out}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
