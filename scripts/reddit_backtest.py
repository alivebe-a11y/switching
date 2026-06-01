#!/usr/bin/env python3
"""Reddit dump backtest — PHASE 1: streaming reader + cashtag extraction (sanity pass).

This is an ISOLATED research spike. It does NOT import or touch the trading bot.
Its only job right now is to prove we can read a (large) Pushshift / Arctic-Shift
`.zst` dump on the server WITHOUT blowing up memory, and to show what's actually in
it (ticker mentions, authors, date span). Sentiment scoring, per-author trust scores,
and forward-return joins come in later phases once this reads cleanly.

Why streaming: these dumps are many GB compressed (and far larger decompressed). We
NEVER decompress the whole thing — we read it in chunks and parse one JSON line at a
time, keeping only small counters in memory. That's what lets it run on a NAS (or even
a laptop) without crashing.

Usage:
    pip install zstandard
    python reddit_backtest.py /data/wallstreetbets_submissions.zst
    python reddit_backtest.py /data/wallstreetbets_submissions.zst --limit 100000   # quick test on a slice
    python reddit_backtest.py /data/wallstreetbets_comments.zst --field body        # comments use 'body'

Run on the server in a throwaway container (no host changes):
    docker run --rm -it -v /mnt/Pool_1/reddit:/data python:3.11-slim \
        bash -c "pip install -q zstandard && python /data/reddit_backtest.py /data/wallstreetbets_submissions.zst --limit 200000"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone

# $TICKER cashtags only for v1. Bare-uppercase extraction ("GME") is a false-positive
# minefield (A, ALL, ON, DD, CEO, YOLO...) — defer it to a later phase with a
# known-ticker universe to validate against. Cashtags are deliberate, so low-noise.
_CASHTAG_RX = re.compile(r"\$([A-Za-z]{1,5})\b")

# Obvious non-tickers people still prefix with $ (currency/options slang). Small stop
# list to keep the v1 summary readable; NOT a substitute for real ticker validation.
_CASHTAG_STOP = {"USD", "DD", "CEO", "ATH", "EOD", "IV", "OTM", "ITM", "FD", "FDS", "YOLO"}


def read_lines_zst(path: str):
    """Yield decompressed text lines from a zstandard .zst dump, streaming.

    Handles the large-window dumps (max_window_size) and reconstructs lines across
    chunk boundaries. Memory stays flat regardless of file size.
    """
    import zstandard

    with open(path, "rb") as fh:
        reader = zstandard.ZstdDecompressor(max_window_size=2 ** 31).stream_reader(fh)
        buffer = ""
        while True:
            chunk = reader.read(2 ** 27).decode("utf-8", errors="ignore")  # 128 MB
            if not chunk:
                break
            lines = (buffer + chunk).split("\n")
            for line in lines[:-1]:
                yield line
            buffer = lines[-1]
        if buffer:
            yield buffer
        reader.close()


def read_lines(path: str):
    """Yield text lines from a dump, auto-detecting format.

    - ``.zst`` (Pushshift / Academic-Torrents dumps): stream-decompressed.
    - anything else (``.jsonl`` / ``.ndjson`` / ``.json`` — what the Arctic Shift
      web download tool hands you): read as plain UTF-8 text, line by line.

    Either way it streams one line at a time, so file size is a non-issue.
    """
    if path.endswith(".zst"):
        yield from read_lines_zst(path)
        return
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            yield line


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase-1 Reddit dump reader / cashtag tally.")
    ap.add_argument("path", help="path to a .zst dump (submissions or comments)")
    ap.add_argument("--field", default="title+selftext",
                    help="which text field(s) to scan: 'title+selftext' (posts) or 'body' (comments)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N lines (0 = all)")
    ap.add_argument("--top", type=int, default=25, help="how many top tickers/authors to print")
    ap.add_argument("--out", default=None,
                    help="also write the summary report to this path (put it under the "
                         "mounted volume, e.g. /data/wsb_report.txt, so it survives a --rm container)")
    args = ap.parse_args(argv)

    fields = args.field.split("+")
    ticker_mentions: Counter[str] = Counter()
    author_posts: Counter[str] = Counter()
    author_with_ticker: Counter[str] = Counter()

    total = 0
    parse_errors = 0
    with_ticker = 0
    ts_min: int | None = None
    ts_max: int | None = None

    for line in read_lines(args.path):
        if not line.strip():
            continue
        total += 1
        if args.limit and total > args.limit:
            total -= 1
            break
        try:
            obj = json.loads(line)
        except Exception:
            parse_errors += 1
            continue

        # timestamp span
        created = obj.get("created_utc")
        if created is not None:
            try:
                c = int(float(created))
                ts_min = c if ts_min is None else min(ts_min, c)
                ts_max = c if ts_max is None else max(ts_max, c)
            except (TypeError, ValueError):
                pass

        author = (obj.get("author") or "").strip()
        if author and author not in ("[deleted]", "[removed]"):
            author_posts[author] += 1

        text = " ".join(str(obj.get(f) or "") for f in fields)
        tickers = {
            t.upper() for t in _CASHTAG_RX.findall(text)
            if t.upper() not in _CASHTAG_STOP
        }
        if tickers:
            with_ticker += 1
            for t in tickers:
                ticker_mentions[t] += 1
            if author and author not in ("[deleted]", "[removed]"):
                author_with_ticker[author] += 1

        if total % 500_000 == 0:
            print(f"  ...{total:,} lines read", file=sys.stderr)

    def _fmt(ts: int | None) -> str:
        if ts is None:
            return "?"
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    lines: list[str] = []
    lines.append("=" * 60)
    lines.append(f"Reddit dump sanity pass: {args.path}")
    lines.append("=" * 60)
    lines.append(f"records parsed     : {total:,}  (parse errors: {parse_errors:,})")
    lines.append(f"date span          : {_fmt(ts_min)} -> {_fmt(ts_max)}")
    lines.append(f"records w/ cashtag : {with_ticker:,}  ({(with_ticker/total*100 if total else 0):.1f}%)")
    lines.append(f"distinct authors   : {len(author_posts):,}")
    lines.append(f"distinct tickers   : {len(ticker_mentions):,}")
    lines.append(f"\nTop {args.top} tickers by mentions:")
    for tkr, n in ticker_mentions.most_common(args.top):
        lines.append(f"  ${tkr:<6} {n:>8,}")
    lines.append(f"\nTop {args.top} authors by posts (candidate high-frequency callers):")
    for auth, n in author_posts.most_common(args.top):
        wt = author_with_ticker.get(auth, 0)
        lines.append(f"  {auth:<22} {n:>7,} posts  ({wt:,} w/ ticker)")
    lines.append("\n[phase 1 only: extraction + tallies. Next: sentiment scoring, per-author")
    lines.append(" trust scores, and forward-return joins — added once this reads cleanly.]")

    report = "\n".join(lines)
    print("\n" + report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        print(f"\n[report written to {args.out}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
