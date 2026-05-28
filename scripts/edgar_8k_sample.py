"""One-off edgartools 8-K data quality check.

Pulls recent 8-K filings from SEC EDGAR current feed, extracts the press
release headline+body from EX-99.1, runs through our detector classifiers,
and prints a breakdown by item code and detector match.

Usage:
    python scripts/edgar_8k_sample.py               # today's filings (100)
    python scripts/edgar_8k_sample.py --limit 200   # up to 200 filings
    python scripts/edgar_8k_sample.py --item 7.01   # guidance-only

No API key required. SEC polite-crawl: <= 10 req/s (edgartools handles it).
"""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# edgartools with system certs (needed on corporate/home networks)
# ---------------------------------------------------------------------------
try:
    import edgar
    from edgar import Company, configure_http, get_current_filings, set_identity
    configure_http(use_system_certs=True)
except ImportError:
    print("edgartools not installed.  Run: pip install edgartools")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Pull in our detector classify() functions
# ---------------------------------------------------------------------------
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from switching.detectors.mna_target import classify as mna_classify          # noqa: E402
from switching.detectors.guidance_raise import classify as guidance_classify  # noqa: E402
from switching.detectors.dividend_surprise import classify as div_classify    # noqa: E402

# ---------------------------------------------------------------------------
# 8-K item codes
# ---------------------------------------------------------------------------
ITEM_LABELS = {
    "1.01": "Material agreement entered",
    "1.02": "Material agreement terminated",
    "2.01": "Acquisition/disposition completed",
    "2.02": "Results of operations (earnings)",
    "3.01": "Delisting notice",
    "4.01": "Auditor change",
    "5.02": "Officer departure/appointment",
    "5.03": "Articles amendment",
    "7.01": "Regulation FD (guidance/investor update)",
    "8.01": "Other events",
    "9.01": "Financial statements / exhibits",
}

_TABLE_JUNK = re.compile(r"[|+\-─━═╌┄]{3,}")


def _clean_headline(raw: str, limit: int = 160) -> str:
    """Extract a clean single-line headline from messy EX-99.1 text."""
    lines = []
    for line in raw.split("\n"):
        line = _TABLE_JUNK.sub("", line).strip()
        line = re.sub(r"\s+", " ", line)
        # Skip common boilerplate
        if not line:
            continue
        if re.match(r"(?i)^(exhibit|pursuant|united states|securities and exchange|washington|form 8-k|current report)", line):
            continue
        if re.match(r"(?i)^(www\.|http|for immediate release|contact:|investor|media)", line):
            continue
        # Location datelines like "MENOMONEE FALLS, Wis.—(BUSINESS WIRE)—"
        if re.match(r"[A-Z][A-Z ,]+(?:—|\(BUSINESS WIRE\)|\(PR NEWSWIRE\))", line):
            continue
        lines.append(line)
        if len(lines) >= 3:
            break
    headline = " ".join(lines)[:limit]
    return textwrap.shorten(headline, width=limit, placeholder="…")


def _item_codes(obj) -> list[str]:
    """Normalise item codes from a CurrentReport to bare '7.01' format."""
    raw = getattr(obj, "items", []) or []
    codes = []
    for item in raw:
        # "Item 7.01" -> "7.01"
        m = re.search(r"(\d+\.\d+)", str(item))
        if m:
            codes.append(m.group(1))
    return codes


def _get_pr_text(obj) -> tuple[str, str]:
    """Return (headline, body) from EX-99.1 if present, else (company_header, '')."""
    try:
        ex = obj.get_exhibit("EX-99.1")
        if ex:
            txt = str(ex.text() or "")
            headline = _clean_headline(txt)
            body = txt[len(headline):].strip()[:800]
            return headline, body
    except Exception:
        pass
    return "", ""


def _get_ticker(filing) -> str:
    """Best-effort ticker from Company(cik).tickers."""
    try:
        co = Company(filing.cik)
        tickers = co.tickers or []
        if tickers:
            return tickers[0]
    except Exception:
        pass
    return ""


def run(limit: int = 100, item_filter: str | None = None) -> None:
    set_identity("switching-bot research@example.com")

    print(f"\n{'='*72}")
    print(f"  edgartools 8-K quality check  —  up to {limit} current filings")
    if item_filter:
        print(f"  filtering to item {item_filter}")
    print(f"{'='*72}\n")

    print("Fetching 8-K current feed from SEC EDGAR …", flush=True)
    try:
        filings = get_current_filings(form="8-K", page_size=limit)
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    filing_list = list(filings)
    print(f"Got {len(filing_list)} filings.  Processing …\n")

    item_counts: Counter[str] = Counter()
    detector_hits: Counter[str] = Counter()
    detector_directions: dict[str, Counter] = defaultdict(Counter)
    no_pr: int = 0
    total_processed: int = 0

    hits: list[dict] = []
    misses: list[dict] = []   # has target items but no detector match

    for filing in filing_list:
        total_processed += 1
        try:
            obj = filing.obj()
        except Exception:
            continue

        codes = _item_codes(obj)
        for c in codes:
            item_counts[c] += 1

        if item_filter and item_filter not in codes:
            continue

        # Skip pure boilerplate / admin filings (only 9.01 or 5.02/4.01)
        useful_items = set(codes) - {"9.01", "4.01", "3.01", "5.03"}
        if not useful_items:
            continue

        headline, body = _get_pr_text(obj)
        if not headline:
            no_pr += 1
            # Fallback: use company name + items as synthetic headline
            headline = f"{filing.company} — {', '.join(codes)}"

        # Run classifiers
        mna_m = mna_classify(headline, body)
        guidance_m = guidance_classify(headline, body)
        div_m = div_classify(headline, body)

        matched = []
        for det, m in [("mna_target", mna_m), ("guidance_raise", guidance_m), ("dividend_surprise", div_m)]:
            if m:
                matched.append((det, m["direction"], m["severity"]))
                detector_hits[det] += 1
                detector_directions[det][m["direction"]] += 1

        row = {
            "company": filing.company,
            "cik": filing.cik,
            "date": str(getattr(filing, "filing_date", "")),
            "codes": codes,
            "headline": headline,
            "matched": matched,
        }

        if matched:
            # Lazy-load ticker only for hits (to stay within rate limits)
            row["ticker"] = _get_ticker(filing)
            hits.append(row)
        elif any(c in {"7.01", "8.01", "1.01", "2.01", "2.02"} for c in codes):
            misses.append(row)

    # -----------------------------------------------------------------------
    # Report
    # -----------------------------------------------------------------------
    print(f"Filings processed      : {total_processed}")
    print(f"Without press release  : {no_pr}")
    print(f"Detector hits          : {sum(detector_hits.values())}  ({len(hits)} unique filings)")
    print(f"Interesting non-matches: {len(misses)}")
    print()

    print("-- 8-K item breakdown (all filings) --")
    for code, cnt in item_counts.most_common(12):
        label = ITEM_LABELS.get(code, "")
        print(f"  {code:6s}  {cnt:4d}  {label}")
    print()

    print("-- Detector hits --")
    if not detector_hits:
        print("  (none)")
    for det, cnt in detector_hits.most_common():
        print(f"  {det:25s}  {cnt:4d}")
        for direction, n in detector_directions[det].most_common():
            print(f"    {'':25s}  {direction}: {n}")
    print()

    print("-- Matched filings --")
    if not hits:
        print("  (none)")
    for row in hits:
        codes_str = ", ".join(row["codes"])
        tk = row.get("ticker") or "?"
        print(f"  {row['date']:10s}  [{tk:6s}]  items=[{codes_str}]")
        print(f"    {row['headline'][:100]}")
        for det, direction, sev in row["matched"]:
            print(f"    -> {det:<25s}  {direction}  sev={sev:.3f}")
        print()

    print("-- Sample non-matches (items 7.01/8.01/2.02 not caught by detectors) --")
    for row in misses[:15]:
        codes_str = ", ".join(row["codes"])
        print(f"  {row['date']:10s}  [{row['company'][:30]:30s}]  items=[{codes_str}]")
        print(f"    {row['headline'][:100]}")
        print()

    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="8-K data quality sample")
    parser.add_argument("--limit", type=int, default=100, help="Max filings (default 100)")
    parser.add_argument("--item", type=str, default=None, help="Filter to item code, e.g. 7.01")
    args = parser.parse_args()
    run(limit=args.limit, item_filter=args.item)
