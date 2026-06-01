#!/usr/bin/env python3
"""Reddit per-USER profiler — scout candidate "trusted callers".

Companion to reddit_backtest.py. Profiles users' full history (posts + comments,
across ALL subreddits) so you can eyeball: are they credible, thesis-driven
ticker-callers, and WHERE do their calls live (post titles? comments? other subs?).

Accepts files AND/OR folders, and GROUPS output by the `author` field — so you can
dump every user's files into one folder and run it once; each user gets their own
profile section (it won't merge them).

Get user data from the Arctic Shift download tool in USER mode
(https://arctic-shift.photon-reddit.com/download-tool): all posts + comments for a
username across every subreddit.

    # one folder holding all your users' files:
    python reddit_profile_user.py /data/users --out /data/user_profiles.txt
    # or explicit files:
    python reddit_profile_user.py /data/plebbit0rz_posts.jsonl /data/plebbit0rz_comments.jsonl

Streams every file (.jsonl/.ndjson/.json plain, or .zst), so size is a non-issue.
QUALITATIVE scouting — not an edge test.
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
_CASHTAG_STOP = {"USD", "DD", "CEO", "ATH", "EOD", "IV", "OTM", "ITM", "FD", "FDS", "YOLO"}
_DUMP_EXTS = (".jsonl", ".ndjson", ".json", ".zst")


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


def _cashtags(text: str) -> set[str]:
    return {t.upper() for t in _CASHTAG_RX.findall(text) if t.upper() not in _CASHTAG_STOP}


def _expand(paths: list[str]) -> list[str]:
    """Expand any directories into the dump files they contain."""
    files: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if name.lower().endswith(_DUMP_EXTS):
                    files.append(os.path.join(p, name))
        else:
            files.append(p)
    return files


@dataclass
class AuthorStat:
    posts: int = 0
    comments: int = 0
    with_ticker: int = 0
    score_sum: int = 0
    ts_min: int | None = None
    ts_max: int | None = None
    subreddits: Counter = field(default_factory=Counter)
    tickers: Counter = field(default_factory=Counter)
    months: Counter = field(default_factory=Counter)
    samples: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.posts + self.comments


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Profile Reddit users' posting + ticker calls (grouped by author).")
    ap.add_argument("paths", nargs="+", help="files and/or folders of .jsonl/.ndjson/.zst dumps")
    ap.add_argument("--min-items", type=int, default=10, help="only report authors with >= this many items")
    ap.add_argument("--top-authors", type=int, default=25, help="cap number of author profiles printed")
    ap.add_argument("--samples", type=int, default=12, help="ticker-bearing items to show per author")
    ap.add_argument("--out", default=None, help="also write the full report to this path")
    args = ap.parse_args(argv)

    stats: dict[str, AuthorStat] = {}
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
            if not author or author in ("[deleted]", "[removed]"):
                continue
            st = stats.get(author)
            if st is None:
                st = stats[author] = AuthorStat()

            is_post = "title" in o
            if is_post:
                st.posts += 1
                text = f"{o.get('title') or ''} {o.get('selftext') or ''}"
            else:
                st.comments += 1
                text = o.get("body") or ""

            st.subreddits[o.get("subreddit") or "?"] += 1
            st.score_sum += int(o.get("score") or 0)

            created = o.get("created_utc")
            c = None
            if created is not None:
                try:
                    c = int(float(created))
                    st.ts_min = c if st.ts_min is None else min(st.ts_min, c)
                    st.ts_max = c if st.ts_max is None else max(st.ts_max, c)
                    st.months[datetime.fromtimestamp(c, tz=timezone.utc).strftime("%Y-%m")] += 1
                except (TypeError, ValueError):
                    pass

            tk = _cashtags(text)
            if tk:
                st.with_ticker += 1
                for t in tk:
                    st.tickers[t] += 1
                if len(st.samples) < args.samples:
                    d = datetime.fromtimestamp(c, tz=timezone.utc).strftime("%Y-%m-%d") if c else "?"
                    snippet = re.sub(r"\s+", " ", text).strip()[:110]
                    kind = "post" if is_post else "comment"
                    st.samples.append((d, o.get("subreddit") or "?", kind, f"[{','.join(sorted(tk))}] {snippet}"))

    def _fmt(ts: int | None) -> str:
        return "?" if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    reported = [(a, s) for a, s in stats.items() if s.total >= args.min_items]
    reported.sort(key=lambda kv: kv[1].total, reverse=True)
    reported = reported[: args.top_authors]

    L: list[str] = []
    L.append("#" * 64)
    L.append(f"User profiles — {len(files)} file(s), {len(stats):,} authors seen, "
             f"{len(reported)} reported (>= {args.min_items} items)")
    L.append("#" * 64)

    for author, s in reported:
        tick_pct = (s.with_ticker / s.total * 100) if s.total else 0
        L.append("")
        L.append("=" * 60)
        L.append(f"u/{author}")
        L.append("=" * 60)
        L.append(f"items            : {s.total:,}  ({s.posts:,} posts, {s.comments:,} comments)")
        L.append(f"date span        : {_fmt(s.ts_min)} -> {_fmt(s.ts_max)}")
        L.append(f"items w/ cashtag : {s.with_ticker:,}  ({tick_pct:.1f}%)")
        L.append(f"avg score        : {(s.score_sum / s.total if s.total else 0):.1f}")
        L.append(f"subreddits       : " + ", ".join(f"r/{sub}({n})" for sub, n in s.subreddits.most_common(8)))
        L.append(f"top tickers      : " + ", ".join(f"${t}({n})" for t, n in s.tickers.most_common(12)))
        L.append(f"active months    : " + ", ".join(f"{m}:{s.months[m]}" for m in sorted(s.months)))
        if s.samples:
            L.append("sample ticker items:")
            for d, sub, kind, txt in s.samples:
                L.append(f"  {d} r/{sub} ({kind}): {txt}")

    report = "\n".join(L)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        print(f"\n[report written to {args.out}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
