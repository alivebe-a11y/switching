"""Bench three LLM/NER backends against the funnel's no_ticker drops.

Goal: produce real numbers so the build/no-build decision for ticker_resolver.py
is made on data, not vibes. Pulls a sample of headlines that classified+failed-ticker
in the last N days, asks each available backend to propose a ticker, validates
each proposal (must exist in SEC ticker list AND yfinance must return a price),
then uses Claude Sonnet as a *judge* to label the ground-truth ticker.

Outputs:
    bench/bench_results_<ts>.csv     per-headline per-backend results + truth
    bench/bench_summary_<ts>.txt     per-backend / per-detector recall + precision

Per-backend metrics:
    recall     = (TP) / (judge-found-truth)         # of headlines a truth exists for, how many did we recover
    precision  = (TP) / (TP + FP_validated)         # of our proposals that pass validation, how many match truth
    yield      = recall * precision                 # real signal recovery rate

Backends (auto-skip if dep missing):
    haiku    — Anthropic API, requires ANTHROPIC_API_KEY
    ollama   — local Qwen 2.5 7B via Ollama HTTP, requires Ollama running + model pulled
    finbert  — Hugging Face transformers NER + fuzzy match against SEC list

Usage:
    # Pull a fresh DB copy from the NAS first (or point at any switching.db):
    scp root@<nas>:/mnt/Pool_1/Configs/dockge2/Stacks/stocks/data/cache/switching.db .

    python scripts/bench_ticker_resolver.py --db ./switching.db --limit 100
    python scripts/bench_ticker_resolver.py --check          # availability check only
    python scripts/bench_ticker_resolver.py --backends haiku # one backend only
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


def _load_dotenv() -> tuple[Path | None, list[str], list[str]]:
    """Load KEY=VALUE pairs from .env next to the repo, if present.

    Shell env wins over .env (standard dotenv semantics — lets you still
    override on the command line). But shadowed keys are reported back so
    the caller can warn about the gotcha "I changed .env but the bench is
    still using my old shell value".

    Returns (env_path_or_None, applied_keys, shadowed_keys).
    """
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return None, [], []
    applied: list[str] = []
    shadowed: list[str] = []
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not key:
            continue
        if os.environ.get(key):
            # Shell already has a non-empty value — shell wins, but flag it
            # so we can print a visible warning later.
            if os.environ[key] != val:
                shadowed.append(key)
        else:
            os.environ[key] = val
            applied.append(key)
    return env_path, applied, shadowed


# Load .env eagerly so subsequent module-level reads (e.g. OllamaBackend._host)
# pick up the values.
_DOTENV_LOADED_FROM, _DOTENV_APPLIED, _DOTENV_SHADOWED = _load_dotenv()


# Per-call timeouts so a hung backend can't lock the bench
_HTTP_TIMEOUT = 15.0       # seconds
_MAX_CALL_ATTEMPTS = 2     # one retry on transient failure
_RETRY_BACKOFF = 1.5       # seconds between retries

log = logging.getLogger("bench_ticker_resolver")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class Drop:
    detector: str
    headline: str
    url: str
    summary: str
    ts: str
    service: str


@dataclass
class Proposal:
    """One backend's answer to one headline."""
    ticker: str | None        # raw proposal (None or 'UNKNOWN' -> None)
    validated: bool           # ticker exists in SEC list AND yfinance has data
    invalidation_reason: str  # '' if validated, else why not
    latency_s: float
    error: str = ""           # non-empty if the backend itself crashed


@dataclass
class Row:
    drop: Drop
    proposals: dict[str, Proposal] = field(default_factory=dict)  # backend_name -> Proposal
    truth: str | None = None       # judge's ticker (validated) or None
    truth_reason: str = ""         # judge's rationale (kept for spot-check)


# ---------------------------------------------------------------------------
# Validation — SEC ticker list + yfinance price check
# ---------------------------------------------------------------------------

_SEC_TICKER_SET: set[str] | None = None
_PRICE_CACHE: dict[str, bool] = {}    # ticker -> has-price


def _load_sec_ticker_set() -> set[str]:
    global _SEC_TICKER_SET
    if _SEC_TICKER_SET is not None:
        return _SEC_TICKER_SET
    try:
        from switching.sources import ticker_lookup
        _name_to_ticker, ticker_to_name = ticker_lookup._load_map()
        _SEC_TICKER_SET = {t.upper() for t in ticker_to_name.keys() if t}
        log.info("validation: loaded %d US tickers from SEC map", len(_SEC_TICKER_SET))
    except Exception as exc:
        log.warning("could not load SEC ticker map: %s", exc)
        _SEC_TICKER_SET = set()
    return _SEC_TICKER_SET


def _has_price(ticker: str) -> bool:
    if ticker in _PRICE_CACHE:
        return _PRICE_CACHE[ticker]
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="1d")
        ok = not h.empty
    except Exception:
        ok = False
    _PRICE_CACHE[ticker] = ok
    return ok


def validate_ticker(ticker: str | None) -> tuple[bool, str]:
    """Return (validated, reason_if_not).  reason is '' on success."""
    if not ticker or ticker.upper() in {"UNKNOWN", "NONE", ""}:
        return False, "no_proposal"
    t = ticker.strip().upper()
    # Permit UK suffix style
    bare = t[:-2] if t.endswith(".L") else t
    if t.endswith(".L"):
        # We don't yet have an LSE list — skip SEC check for UK; only yfinance
        # validates UK.
        if _has_price(t):
            return True, ""
        return False, "yfinance_no_data"
    sec = _load_sec_ticker_set()
    if sec and bare not in sec:
        return False, "not_in_sec_list"
    if not _has_price(t):
        return False, "yfinance_no_data"
    return True, ""


# ---------------------------------------------------------------------------
# Backends — each is responsible for its own availability + per-call timeout
# ---------------------------------------------------------------------------

class Backend:
    name: str = "base"

    def available(self) -> tuple[bool, str]:
        """Return (ok, reason_if_not)."""
        return False, "not implemented"

    def resolve(self, drop: Drop) -> Proposal:
        raise NotImplementedError

    def _attempt_with_retry(self, fn) -> tuple[str | None, str]:
        """Run *fn* with one retry on exception. Returns (result, error_msg)."""
        last_err = ""
        for attempt in range(_MAX_CALL_ATTEMPTS):
            try:
                return fn(), ""
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                if attempt + 1 < _MAX_CALL_ATTEMPTS:
                    time.sleep(_RETRY_BACKOFF * (attempt + 1))
        return None, last_err


_RESOLVER_PROMPT = """You are a financial entity resolver. Given a news headline,
return ONLY the stock ticker symbol of the COMPANY THIS HEADLINE IS ABOUT,
or the word UNKNOWN if you cannot identify it with high confidence.

Format:
- US listings: bare ticker, e.g. AAPL, MSFT, NVDA, DJT
- UK listings: ticker with .L suffix, e.g. VOD.L, GAMA.L, RKT.L
- If multiple companies, return the SUBJECT (target of acquisition, company raising
  guidance, etc), not acquirers or analysts mentioned.

Examples:
  "Cigna raises full-year guidance" -> CI
  "Truth Social parent expands platform offering" -> DJT
  "Reckitt completes Mead Johnson divestment" -> RKT.L
  "Arm Holdings beats Q3 estimates" -> ARM
  "Private equity to acquire generic manufacturer" -> UNKNOWN

Headline: {title}
Summary: {summary}

Ticker:"""


class HaikuBackend(Backend):
    name = "haiku"

    def available(self) -> tuple[bool, str]:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False, "ANTHROPIC_API_KEY not set"
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False, "pip install anthropic"
        return True, ""

    def resolve(self, drop: Drop) -> Proposal:
        from anthropic import Anthropic
        client = Anthropic()
        prompt = _RESOLVER_PROMPT.format(title=drop.headline, summary=drop.summary[:300])

        def _call():
            resp = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=20,
                timeout=_HTTP_TIMEOUT,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip().upper()

        t0 = time.monotonic()
        text, err = self._attempt_with_retry(_call)
        latency = time.monotonic() - t0
        if err:
            return Proposal(None, False, "backend_error", latency, error=err)
        ticker = None if not text or text.startswith("UNKNOWN") else text.split()[0]
        ok, reason = validate_ticker(ticker)
        return Proposal(ticker, ok, reason, latency)


class OllamaBackend(Backend):
    name = "ollama"
    _model = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
    _host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")

    def available(self) -> tuple[bool, str]:
        try:
            req = urllib.request.Request(f"{self._host}/api/tags")
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:
            return False, f"Ollama not reachable at {self._host}: {exc}"
        models = {m["name"].split(":")[0] for m in data.get("models", [])}
        wanted = self._model.split(":")[0]
        if wanted not in models:
            return False, f"model '{self._model}' not pulled (run: ollama pull {self._model})"
        return True, ""

    def resolve(self, drop: Drop) -> Proposal:
        prompt = _RESOLVER_PROMPT.format(title=drop.headline, summary=drop.summary[:300])
        body = json.dumps({
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": 12, "temperature": 0.0},
        }).encode()

        def _call():
            req = urllib.request.Request(
                f"{self._host}/api/generate", data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode())
            return (data.get("response") or "").strip().upper()

        t0 = time.monotonic()
        text, err = self._attempt_with_retry(_call)
        latency = time.monotonic() - t0
        if err:
            return Proposal(None, False, "backend_error", latency, error=err)
        ticker = None if not text or text.startswith("UNKNOWN") else text.split()[0]
        ok, reason = validate_ticker(ticker)
        return Proposal(ticker, ok, reason, latency)


class FinBertBackend(Backend):
    """NER-then-lookup: extract ORG entities from the headline with a finance-tuned
    transformer, then fuzzy-match against the SEC ticker name list."""
    name = "finbert"
    _model_id = os.environ.get("FINBERT_NER_MODEL", "Jean-Baptiste/roberta-large-ner-english")
    _pipeline = None

    def available(self) -> tuple[bool, str]:
        try:
            import transformers  # noqa: F401
            from rapidfuzz import process  # noqa: F401
        except ImportError as exc:
            return False, f"pip install transformers torch rapidfuzz  ({exc})"
        return True, ""

    def _ensure_pipeline(self):
        if self._pipeline is None:
            from transformers import pipeline
            log.info("finbert: loading %s (downloads ~440MB first run)", self._model_id)
            self._pipeline = pipeline("ner", model=self._model_id, aggregation_strategy="simple")
        return self._pipeline

    def resolve(self, drop: Drop) -> Proposal:
        from rapidfuzz import process, fuzz
        from switching.sources import ticker_lookup

        def _call():
            nlp = self._ensure_pipeline()
            entities = nlp(drop.headline)
            orgs = [e["word"].strip() for e in entities if e.get("entity_group") == "ORG"]
            if not orgs:
                return None
            n2t, _ = ticker_lookup._load_map()
            if not n2t:
                return None
            # Best fuzzy match across all detected ORGs
            best_ticker, best_score = None, 0
            choices = list(n2t.keys())
            for org in orgs:
                norm = ticker_lookup._normalize_name(org)
                if not norm:
                    continue
                match = process.extractOne(norm, choices, scorer=fuzz.WRatio, score_cutoff=80)
                if match and match[1] > best_score:
                    best_ticker = n2t[match[0]]
                    best_score = match[1]
            return best_ticker.upper() if best_ticker else None

        t0 = time.monotonic()
        ticker, err = self._attempt_with_retry(_call)
        latency = time.monotonic() - t0
        if err:
            return Proposal(None, False, "backend_error", latency, error=err)
        ok, reason = validate_ticker(ticker)
        return Proposal(ticker, ok, reason, latency)


BACKENDS: list[type[Backend]] = [HaikuBackend, OllamaBackend, FinBertBackend]


# ---------------------------------------------------------------------------
# Judge — Claude Sonnet labels the ground-truth ticker
# ---------------------------------------------------------------------------

_JUDGE_PROMPT = """You are a strict financial analyst. Given a news headline,
identify the listed stock ticker of the company the headline is PRIMARILY ABOUT.

Rules:
- US tickers: bare (AAPL, MSFT). UK: with .L suffix (VOD.L, RKT.L).
- If the subject is private, non-public, or you can't identify with HIGH confidence,
  return UNKNOWN.
- Acquirers and analysts mentioned in passing are NOT the subject — the target /
  guidance-raiser / contract-winner is.
- Output JSON only, no other text: {{"ticker": "<TICKER or UNKNOWN>", "reason": "<one sentence>"}}

Headline: {title}
Summary: {summary}"""


def judge(drop: Drop) -> tuple[str | None, str]:
    """Use Claude Sonnet to label the ground-truth ticker. Returns (ticker, reason).
    Validates the ticker; unvalidated truth is treated as None (judge hallucinated).
    """
    try:
        from anthropic import Anthropic
    except ImportError:
        return None, "anthropic not installed"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None, "ANTHROPIC_API_KEY not set"
    client = Anthropic()
    prompt = _JUDGE_PROMPT.format(title=drop.headline, summary=drop.summary[:500])
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=120,
            timeout=_HTTP_TIMEOUT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        # Strip code fences if the judge adds them
        if text.startswith("```"):
            text = "\n".join(line for line in text.splitlines() if not line.startswith("```"))
        data = json.loads(text)
        t = (data.get("ticker") or "").strip().upper()
        reason = (data.get("reason") or "")[:200]
    except Exception as exc:
        return None, f"judge_error: {exc}"
    if not t or t == "UNKNOWN":
        return None, reason
    ok, _ = validate_ticker(t)
    return (t if ok else None), reason


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_drops(db_path: Path, limit: int, days: int, seed: int) -> list[Drop]:
    if not db_path.exists():
        raise FileNotFoundError(f"db not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now(tz=timezone.utc).timestamp() - days * 86400)
    cur = conn.execute(
        "SELECT service, detector, reason, ts, headline, url, summary "
        "FROM dropped_signals WHERE reason = 'no_ticker' "
        "ORDER BY id DESC LIMIT 5000"
    )
    rows: list[Drop] = []
    for r in cur.fetchall():
        try:
            ts_dt = datetime.fromisoformat(r["ts"].rstrip("Z"))
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.replace(tzinfo=timezone.utc)
            if ts_dt.timestamp() < cutoff:
                continue
        except Exception:
            pass
        rows.append(Drop(
            detector=r["detector"] or "",
            headline=r["headline"] or "",
            url=r["url"] or "",
            summary=r["summary"] or "",
            ts=r["ts"] or "",
            service=r["service"] or "",
        ))
    if not rows:
        return []
    # Stratified sample by detector so a single chatty detector doesn't dominate.
    rng = random.Random(seed)
    by_det: dict[str, list[Drop]] = {}
    for d in rows:
        by_det.setdefault(d.detector, []).append(d)
    target_per = max(1, limit // max(1, len(by_det)))
    sample: list[Drop] = []
    for det, items in by_det.items():
        rng.shuffle(items)
        sample.extend(items[:target_per])
    rng.shuffle(sample)
    return sample[:limit]


# ---------------------------------------------------------------------------
# Run + report
# ---------------------------------------------------------------------------

def run_bench(rows: list[Row], backends: list[Backend], do_judge: bool) -> None:
    n = len(rows)
    for i, row in enumerate(rows, 1):
        for b in backends:
            row.proposals[b.name] = b.resolve(row.drop)
        if do_judge:
            row.truth, row.truth_reason = judge(row.drop)
        log.info("[%d/%d] %s | %s | truth=%s | %s",
                 i, n, row.drop.detector,
                 (row.drop.headline[:70] + "…") if len(row.drop.headline) > 70 else row.drop.headline,
                 row.truth or "?",
                 " ".join(f"{name}={(p.ticker or '?')}{'✓' if p.validated else ''}"
                          for name, p in row.proposals.items()))


def summarise(rows: list[Row], backends: list[Backend]) -> str:
    out: list[str] = []
    out.append(f"Bench results: {len(rows)} headlines\n")
    truths = sum(1 for r in rows if r.truth)
    out.append(f"Judge identified a truth for {truths}/{len(rows)} headlines ({truths*100//max(1,len(rows))}%)\n")
    out.append("=" * 78)

    # Per-backend metrics
    for b in backends:
        out.append(f"\n## Backend: {b.name}")
        proposed = sum(1 for r in rows if r.proposals.get(b.name) and r.proposals[b.name].ticker)
        validated = sum(1 for r in rows if r.proposals.get(b.name) and r.proposals[b.name].validated)
        errors = sum(1 for r in rows if r.proposals.get(b.name) and r.proposals[b.name].error)
        tp = sum(
            1 for r in rows
            if r.truth and r.proposals.get(b.name) and r.proposals[b.name].validated
            and r.proposals[b.name].ticker and r.proposals[b.name].ticker.upper() == r.truth.upper()
        )
        # Recall = of headlines truth could find, how many did we get right
        recall = tp / truths if truths else 0
        # Precision = of validated proposals, how many matched truth
        precision = tp / validated if validated else 0
        avg_latency = (
            sum(r.proposals[b.name].latency_s for r in rows if r.proposals.get(b.name))
            / max(1, len(rows))
        )
        out.append(f"  proposed:      {proposed}/{len(rows)}")
        out.append(f"  validated:     {validated}/{len(rows)}")
        out.append(f"  matched truth: {tp}")
        out.append(f"  recall:        {recall*100:.1f}%   (vs truths-found)")
        out.append(f"  precision:     {precision*100:.1f}%   (of validated proposals)")
        out.append(f"  yield:         {recall*precision*100:.1f}%   (effective recovery rate)")
        out.append(f"  errors:        {errors}")
        out.append(f"  avg latency:   {avg_latency:.2f}s")

        # Invalidation breakdown
        reasons: dict[str, int] = {}
        for r in rows:
            p = r.proposals.get(b.name)
            if p and p.ticker and not p.validated:
                reasons[p.invalidation_reason] = reasons.get(p.invalidation_reason, 0) + 1
        if reasons:
            out.append("  invalidation breakdown:")
            for reason, n in sorted(reasons.items(), key=lambda x: -x[1]):
                out.append(f"    {reason:<20} {n}")

        # Per-detector breakdown
        by_det: dict[str, list[Row]] = {}
        for r in rows:
            by_det.setdefault(r.drop.detector, []).append(r)
        out.append("  per-detector:")
        for det, det_rows in sorted(by_det.items()):
            det_truths = sum(1 for r in det_rows if r.truth)
            det_tp = sum(
                1 for r in det_rows
                if r.truth and r.proposals.get(b.name) and r.proposals[b.name].validated
                and r.proposals[b.name].ticker and r.proposals[b.name].ticker.upper() == r.truth.upper()
            )
            det_recall = det_tp / det_truths if det_truths else 0
            out.append(f"    {det:<22} truths={det_truths:>3}  matched={det_tp:>3}  recall={det_recall*100:>5.1f}%")
    return "\n".join(out)


def write_csv(rows: list[Row], backends: list[Backend], path: Path) -> None:
    headers = ["service", "detector", "headline", "url", "truth", "truth_reason"]
    for b in backends:
        headers.extend([f"{b.name}_ticker", f"{b.name}_validated", f"{b.name}_reason", f"{b.name}_latency_s"])
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            row_data = [r.drop.service, r.drop.detector, r.drop.headline, r.drop.url,
                        r.truth or "", r.truth_reason]
            for b in backends:
                p = r.proposals.get(b.name)
                if p:
                    row_data.extend([p.ticker or "", "Y" if p.validated else "N",
                                     p.invalidation_reason or p.error, f"{p.latency_s:.2f}"])
                else:
                    row_data.extend(["", "", "skipped", ""])
            w.writerow(row_data)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="./switching.db", help="path to switching.db (default ./switching.db)")
    ap.add_argument("--days", type=int, default=14, help="look back N days of drops (default 14)")
    ap.add_argument("--limit", type=int, default=100, help="max headlines to bench (default 100)")
    ap.add_argument("--seed", type=int, default=42, help="rng seed for sampling (default 42)")
    ap.add_argument("--backends", default="all", help="comma-separated subset: haiku,ollama,finbert (default: all available)")
    ap.add_argument("--no-judge", action="store_true", help="skip Sonnet truth-labelling (saves $$ but no recall numbers)")
    ap.add_argument("--check", action="store_true", help="just print which backends are available and exit")
    ap.add_argument("--out-dir", default="./bench", help="output directory (default ./bench)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(message)s", datefmt="%H:%M:%S",
    )

    # Resolve which backends to run
    wanted = {b.strip() for b in args.backends.split(",")} if args.backends != "all" else None
    candidates: list[Backend] = []
    if _DOTENV_LOADED_FROM:
        print(f"\nFound .env at {_DOTENV_LOADED_FROM}")
        if _DOTENV_APPLIED:
            print(f"  applied:  {', '.join(_DOTENV_APPLIED)}")
        if _DOTENV_SHADOWED:
            print(f"  SHADOWED by shell (shell value wins): {', '.join(_DOTENV_SHADOWED)}")
            print("  To use the .env value instead, clear the shell var first:")
            for k in _DOTENV_SHADOWED:
                print(f"      PowerShell:  Remove-Item Env:{k}")
                print(f"      bash:        unset {k}")
            print("  …then re-run.")
    print("\nBackend availability:")
    for cls in BACKENDS:
        b = cls()
        ok, reason = b.available()
        marker = "OK " if ok else "-- "
        print(f"  {marker} {b.name:<8} {reason}")
        if ok and (wanted is None or b.name in wanted):
            candidates.append(b)
    print()

    if args.check:
        return 0
    if not candidates:
        print("No backends available. Install at least one and try again.")
        return 1

    # Pre-load + report SEC ticker map status — validation is much weaker without it
    sec = _load_sec_ticker_set()
    if len(sec) < 1000:
        print(f"WARNING: SEC ticker map has only {len(sec)} entries — validation will be too "
              "permissive and the precision number will be misleading. Common cause: "
              "ssl/proxy or first-run cache fetch failed. Bench will still run, but "
              "treat its 'validated' numbers as a CEILING, not the truth.\n")
    else:
        print(f"SEC ticker list loaded: {len(sec)} symbols.\n")

    db_path = Path(args.db)
    print(f"Loading drops from {db_path}  (last {args.days} days, sample {args.limit}) ...")
    try:
        drops = load_drops(db_path, args.limit, args.days, args.seed)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 2
    if not drops:
        print("No no_ticker drops found in window — nothing to bench.")
        return 0
    print(f"Sampled {len(drops)} headlines across {len({d.detector for d in drops})} detectors.\n")

    do_judge = not args.no_judge
    if do_judge:
        from anthropic import Anthropic   # noqa: F401  — fail fast if not installed
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("WARNING: ANTHROPIC_API_KEY missing — judge will produce no truths; pass --no-judge to silence")

    rows = [Row(drop=d) for d in drops]
    t_start = time.monotonic()
    run_bench(rows, candidates, do_judge)
    print(f"\nBench done in {time.monotonic()-t_start:.1f}s")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    csv_path = out_dir / f"bench_results_{ts}.csv"
    txt_path = out_dir / f"bench_summary_{ts}.txt"

    write_csv(rows, candidates, csv_path)
    summary = summarise(rows, candidates)
    txt_path.write_text(summary, encoding="utf-8")

    print("\n" + summary)
    print(f"\nWrote {csv_path}")
    print(f"Wrote {txt_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
