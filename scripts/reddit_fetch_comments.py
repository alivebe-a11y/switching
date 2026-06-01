#!/usr/bin/env python3
"""Resumable Arctic Shift downloader — grind a big subreddit's comments (or posts).

The web download tool times out on huge subreddits (WSB comments = millions of rows).
This walks the history MONTH BY MONTH via the Arctic Shift API, writing one .jsonl
file per month. It is RESUMABLE: a month is only marked done once fully written, so if
it breaks (network, rate limit, reboot) you just re-run the SAME command and it skips
finished months and continues. Stdlib only — no pip, runs in a bare python container.

API (https://arctic-shift.photon-reddit.com):
  GET /api/comments/search?subreddit=..&after=..&before=..&sort=asc&limit=auto
  - dates: epoch seconds; limit=auto returns 100-1000; results sorted by created_utc.
  - no cursor: we advance `after` to the last created_utc of each page.

Usage (run DETACHED on the server so it grinds unattended):
  docker run -d --name wsb_dl -v /mnt/Pool_1/Configs/reddit:/data python:3.11-slim \
    python /data/reddit_fetch_comments.py --kind comments --subreddit wallstreetbets \
    --start 2024-01 --end 2026-04 --out-dir /data/discovery/wsb_comments
  docker logs -f wsb_dl        # watch; re-run the same command to resume after any break

Then read every month file at once (the other scripts already accept a folder):
  python reddit_find_callers.py /data/discovery/wsb_comments ...

NOTE: it's polite/rate-limited, so a multi-year pull can take hours — but it's
unattended and resumable, which is the whole point. Start narrow (recent year) and
widen with --start if you want.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

_BASE = "https://arctic-shift.photon-reddit.com"


def _month_starts(start: str, end: str):
    """Yield (label, start_epoch, end_epoch) for each month in [start, end] inclusive.
    start/end are 'YYYY-MM'. end_epoch is the first second of the NEXT month (exclusive)."""
    y, m = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    while (y, m) <= (ey, em):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        s = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp())
        e = int(datetime(ny, nm, 1, tzinfo=timezone.utc).timestamp())
        yield f"{y:04d}-{m:02d}", s, e
        y, m = ny, nm


def _get(path: str, params: dict, ua: str, max_retries: int = 6) -> list:
    """GET with retries/backoff. Returns the list of items (handles {'data':[...]} or bare list)."""
    url = f"{_BASE}{path}?{urllib.parse.urlencode(params)}"
    backoff = 5.0
    for attempt in range(max_retries):
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="ignore"))
                remaining = resp.headers.get("X-RateLimit-Remaining")
                if remaining is not None:
                    try:
                        if int(remaining) <= 1:
                            time.sleep(5.0)
                    except ValueError:
                        pass
            if isinstance(body, dict):
                return body.get("data", body.get("items", []))
            return body if isinstance(body, list) else []
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                wait = float(exc.headers.get("Retry-After") or backoff)
                print(f"    429 rate-limited; sleeping {wait:.0f}s", flush=True)
                time.sleep(wait)
            elif 500 <= exc.code < 600:
                print(f"    HTTP {exc.code}; retry in {backoff:.0f}s", flush=True)
                time.sleep(backoff)
            else:
                raise
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"    net error ({exc}); retry in {backoff:.0f}s", flush=True)
            time.sleep(backoff)
        backoff = min(backoff * 2, 120.0)
    raise RuntimeError(f"giving up after {max_retries} retries: {url}")


def fetch_window(path: str, subreddit: str, start: int, end: int, ua: str, sleep: float, fh, label: str = "") -> int:
    """Page through [start, end) ascending, writing each item as a JSON line. Returns count."""
    cursor = start
    seen: set[str] = set()
    written = 0
    requests = 0
    while cursor < end:
        items = _get(path, {
            "subreddit": subreddit, "after": cursor, "before": end,
            "sort": "asc", "limit": "auto",
        }, ua)
        requests += 1
        if not items:
            break
        max_c = cursor
        wrote_this_page = 0
        for it in items:
            try:
                c = int(float(it.get("created_utc")))
            except (TypeError, ValueError):
                continue
            max_c = max(max_c, c)
            iid = it.get("id") or f"{it.get('author')}|{c}"
            if iid in seen:
                continue
            seen.add(iid)
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")
            wrote_this_page += 1
            written += 1
        # heartbeat so a long month doesn't look hung
        if requests % 25 == 0:
            at = datetime.fromtimestamp(cursor, tz=timezone.utc).strftime("%Y-%m-%d")
            print(f"    {label}: ~{written:,} items so far (reached {at}, {requests} reqs)", flush=True)
        # advance — force progress even if a whole page was duplicates
        cursor = (max_c + 1) if wrote_this_page == 0 else max_c
        time.sleep(sleep)
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Resumable Arctic Shift subreddit downloader.")
    ap.add_argument("--subreddit", default="wallstreetbets")
    ap.add_argument("--kind", choices=["comments", "posts"], default="comments")
    ap.add_argument("--start", required=True, help="first month, YYYY-MM")
    ap.add_argument("--end", required=True, help="last month, YYYY-MM (inclusive)")
    ap.add_argument("--out-dir", required=True, help="directory for the per-month .jsonl files")
    ap.add_argument("--sleep", type=float, default=1.0, help="seconds between API requests (politeness)")
    ap.add_argument("--user-agent", default="switching-research/1.0 (personal backtest)")
    ap.add_argument("--dry-run", action="store_true", help="list months it would fetch and exit")
    args = ap.parse_args(argv)

    path = "/api/comments/search" if args.kind == "comments" else "/api/posts/search"
    os.makedirs(args.out_dir, exist_ok=True)

    months = list(_month_starts(args.start, args.end))
    print(f"{args.kind} r/{args.subreddit}: {len(months)} month(s) {args.start}..{args.end} -> {args.out_dir}", flush=True)

    for label, s, e in months:
        final = os.path.join(args.out_dir, f"{args.subreddit}_{args.kind}_{label}.jsonl")
        done = final + ".done"
        if os.path.exists(done):
            print(f"  {label}: already done, skip", flush=True)
            continue
        if args.dry_run:
            print(f"  {label}: would fetch [{s}, {e})", flush=True)
            continue
        tmp = final + ".partial"
        t0 = time.time()
        print(f"  {label}: starting...", flush=True)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                n = fetch_window(path, args.subreddit, s, e, args.user_agent, args.sleep, fh, label)
            os.replace(tmp, final)
            open(done, "w").close()
            print(f"  {label}: {n:,} items in {time.time()-t0:.0f}s", flush=True)
        except Exception as exc:
            print(f"  {label}: FAILED ({exc}) — leaving incomplete, re-run to resume", flush=True)
            return 1
    print("done.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
