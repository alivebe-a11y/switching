#!/usr/bin/env python3
"""Reddit caller DISCOVERY — rank authors by upvote-weighted ticker activity.

Companion to reddit_backtest.py / reddit_profile_user.py. Point it at one or more
subreddit dumps (posts and/or comments, any subs) and it produces a LEADERBOARD of
candidate "callers" — ranked not by raw post count (that surfaces bots) but by the
crowd's own credibility signal: the sum of upvote score across their ticker-bearing
items. Bots and crypto/slang cashtag noise are filtered out.

Use it to find users worth deep-diving with reddit_profile_user.py.

    # a folder of fresh WSB + r/smallstreetbets + r/stocks dumps:
    python reddit_find_callers.py /data/discovery --out /data/callers.txt
    # with a real-ticker whitelist for precision (recommended — see note):
    python reddit_find_callers.py /data/discovery --tickers /data/us_tickers.txt

Whitelist: a plain text file, one uppercase symbol per line. Without it, a built-in
crypto/slang stoplist is used (good enough for discovery; a whitelist is better for
the eventual backtest). Build one from SEC, e.g. (run once, needs a UA header):
    curl -s -H "User-Agent: you@example.com" https://www.sec.gov/files/company_tickers.json \
      | python -c "import json,sys;print('\\n'.join(sorted({v['ticker'].upper() for v in json.load(sys.stdin).values()})))" > us_tickers.txt

Streams every file (.jsonl/.ndjson/.json or .zst); size is a non-issue.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

_CASHTAG_RX = re.compile(r"\$([A-Za-z]{1,5})\b")
_DUMP_EXTS = (".jsonl", ".ndjson", ".json", ".zst")

# Authors to always skip (automation). Plus: anything ending in "bot" or containing
# "moderator" is treated as a bot.
_BOT_NAMES = {"automoderator", "wsbapp", "visualmod", "bottoperator", "remindmebot"}

# Cashtag tokens that are (almost) never the US equity being implied — mostly crypto
# tickers + WSB slang. We DON'T stoplist ambiguous real tickers (DD, ALL, ON, AI, SE):
# a --tickers whitelist is the right tool for precision; this just removes obvious junk.
_NOISE = {
    # crypto
    "BTC", "ETH", "XRP", "BNB", "SOL", "ADA", "DOGE", "SHIB", "DOT", "LINK", "UNI",
    "SUSHI", "CRV", "AAVE", "COMP", "MKR", "REN", "BAL", "OP", "ARB", "PEPE", "FTM",
    "MATIC", "AVAX", "LTC", "XLM", "ATOM", "ALGO", "NEAR", "FIL", "ICP", "HBAR",
    "VET", "SAND", "MANA", "BCH", "TRX", "EOS", "XMR", "USDT", "USDC", "DAI",
    # slang / acronyms
    "YOLO", "FD", "FDS", "OTM", "ITM", "IV", "ATH", "ATL", "EOD", "EOW", "FOMO",
    "HODL", "WSB", "CPI", "FED", "GDP", "PPI", "IPO", "USD", "EPS", "PE",
}


def read_lines(path: str):
    if path.endswith(".zst"):
        import zstandard
        with open(path, "rb") as fh:
            reader = zstandard.ZstdDecompressor(max_window_size=2 ** 31).stream_reader(fh)
            buffer = ""
            while True:
                chunk = reader.read(2 ** 27).decode("utf-8", errors="ignore")
                if not chunk:
                    break
                lines = (buffer + chunk).split("\n")
                for line in lines[:-1]:
                    yield line
                buffer = lines[-1]
            if buffer:
                yield buffer
            reader.close()
        return
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            yield line


def _expand(paths: list[str]) -> list[str]:
    files: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if name.lower().endswith(_DUMP_EXTS):
                    files.append(os.path.join(p, name))
        else:
            files.append(p)
    return files


def _is_bot(author: str) -> bool:
    a = author.lower()
    return a in _BOT_NAMES or a.endswith("bot") or "moderator" in a


@dataclass
class Caller:
    ticker_items: int = 0
    score_sum: int = 0           # sum of upvotes across ticker-bearing items
    total_items: int = 0
    tickers: Counter = field(default_factory=Counter)
    subreddits: Counter = field(default_factory=Counter)
    ts_min: int | None = None
    ts_max: int | None = None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rank candidate Reddit stock-callers (upvote-weighted).")
    ap.add_argument("paths", nargs="+", help="files and/or folders of subreddit dumps")
    ap.add_argument("--tickers", default=None, help="optional whitelist file (one UPPER symbol per line)")
    ap.add_argument("--min-ticker-items", type=int, default=15,
                    help="ignore authors with fewer than this many ticker-bearing items")
    ap.add_argument("--top", type=int, default=40, help="how many callers to list")
    ap.add_argument("--out", default=None, help="also write the leaderboard to this path")
    args = ap.parse_args(argv)

    whitelist: set[str] | None = None
    if args.tickers:
        with open(args.tickers, "r", encoding="utf-8", errors="ignore") as fh:
            whitelist = {ln.strip().upper() for ln in fh if ln.strip()}

    def valid_tickers(text: str) -> set[str]:
        out = set()
        for raw in _CASHTAG_RX.findall(text):
            t = raw.upper()
            if whitelist is not None:
                if t in whitelist:
                    out.add(t)
            elif t not in _NOISE:
                out.add(t)
        return out

    callers: dict[str, Caller] = {}
    files = _expand(args.paths)

    for path in files:
        for line in read_lines(path):
            if not line.strip():
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            author = (o.get("author") or "").strip()
            if not author or author in ("[deleted]", "[removed]") or _is_bot(author):
                continue

            text = (o.get("title") or "") + " " + (o.get("selftext") or "") + " " + (o.get("body") or "")
            tk = valid_tickers(text)

            c = callers.get(author)
            if c is None:
                c = callers[author] = Caller()
            c.total_items += 1
            if not tk:
                continue
            c.ticker_items += 1
            c.score_sum += int(o.get("score") or 0)
            c.subreddits[o.get("subreddit") or "?"] += 1
            for t in tk:
                c.tickers[t] += 1
            created = o.get("created_utc")
            if created is not None:
                try:
                    ci = int(float(created))
                    c.ts_min = ci if c.ts_min is None else min(c.ts_min, ci)
                    c.ts_max = ci if c.ts_max is None else max(c.ts_max, ci)
                except (TypeError, ValueError):
                    pass

    def _fmt(ts: int | None) -> str:
        return "?" if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")

    ranked = [(a, c) for a, c in callers.items() if c.ticker_items >= args.min_ticker_items]
    # Rank by the crowd's credibility signal: total upvotes across ticker-bearing items.
    ranked.sort(key=lambda kv: kv[1].score_sum, reverse=True)
    ranked = ranked[: args.top]

    L: list[str] = []
    L.append("#" * 78)
    L.append(f"Caller leaderboard — {len(files)} file(s), {len(callers):,} authors, "
             f"{len(ranked)} shown (>= {args.min_ticker_items} ticker items)")
    L.append(f"ticker filter: {'whitelist of ' + str(len(whitelist)) + ' symbols' if whitelist else 'built-in crypto/slang stoplist'}")
    L.append("#" * 78)
    L.append(f"{'author':<24}{'tkItems':>8}{'upvotes':>9}{'avg':>7}  span        top tickers / subs")
    for author, c in ranked:
        avg = c.score_sum / c.ticker_items if c.ticker_items else 0
        toptk = ",".join(f"{t}({n})" for t, n in c.tickers.most_common(6))
        topsub = ",".join(f"r/{s}" for s, _ in c.subreddits.most_common(2))
        L.append(
            f"{author:<24}{c.ticker_items:>8}{c.score_sum:>9}{avg:>7.0f}  "
            f"{_fmt(c.ts_min)}->{_fmt(c.ts_max)}  {toptk}  [{topsub}]"
        )

    report = "\n".join(L)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        print(f"\n[leaderboard written to {args.out}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
