"""Movers researcher — audit why big market movers were (or weren't) caught.

Each market day, pull the top movers (yfinance screener) and, for every mover we
did NOT act on, attribute WHY we missed it. The point isn't to *trade* movers (by
the time something is a top mover the move has largely happened) — it's to grade
the detector suite against real moves and surface what we're systematically missing:

  - caught       : we signalled/traded it (it's in our records) — not a miss
  - ticker_drop  : a detector classified the story but we couldn't resolve the
                   ticker, so it hit the detection funnel — a recall hole
  - feed_gap     : a detector WOULD classify the mover's news, but the story never
                   reached our feeds — the gap is the news SOURCE, not the detector
  - no_detector  : the mover has news but nothing we run classifies it — a catalyst
                   type we don't cover yet (a new-detector candidate)
  - no_news      : no headline found — likely flow / technical / squeeze, not our game

Log-only research: reads the live DB + our classifiers, writes a dated JSON report
that the dashboard "Movers" tab renders. yfinance only (no new deps).

ATTRIBUTION IS HEURISTIC: yfinance per-ticker news != exactly our feeds, so the
feed_gap/ticker_drop split is a best-effort inference. It points at the right
problem; it is not gospel.
"""

from __future__ import annotations

import importlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

_AUDIT_DIR = "movers_audit"
_REASONS = ("caught", "ticker_drop", "feed_gap", "no_detector", "no_news")

# How many headlines to retain per mover in the saved audit (bounded payload).
_MAX_KEPT_HEADLINES = 8

# Candidate catalyst themes for the `no_detector` bucket — the "which detector
# might we be missing?" instrument. A no_detector mover's headlines are tagged with
# any themes they hit, and run_audit tallies them per day so a recurring uncovered
# catalyst type surfaces over time (a build candidate). HEURISTIC + best-effort: on
# the yfinance news source most no_detector items are Yahoo *commentary* (valuation
# think-pieces, listicles) that match nothing — that's expected, and a theme only
# becomes trustworthy on a clean catalyst source (Benzinga WIIM). See the graduation
# rule in CLAUDE.md (Movers Researcher).
_CATALYST_THEMES: dict[str, str] = {
    "offering_dilution":   r"offering|dilut|priced|registered direct|\batm\b|convertible|private placement",
    "clinical_data":       r"\bphase\b|topline|trial|endpoint|\bdata\b|study results|results from",
    "fda_regulatory":      r"\bfda\b|approval|clearance|breakthrough|designation|\bema\b|chmp|crl",
    "partnership_license":  r"partner|collaborat|licens|alliance|teams up",
    "mna":                 r"acqui|merger|takeover|buyout|to be acquired|tender offer",
    "analyst":             r"upgrade|downgrade|price target|initiat|reiterat|coverage",
    "guidance_earnings":   r"guidance|outlook|preliminary|warns|warning|profit warning",
    "legal_settlement":    r"lawsuit|settle|verdict|ruling|patent|investigat|fine|antitrust",
    "exec_mgmt":           r"\bceo\b|\bcfo\b|resign|appoint|steps down|departure",
    "contract_award":      r"\bcontract\b|\baward\b|wins |deal worth|grant",
    "short_report":        r"short seller|hindenburg|muddy waters|fraud|accounting",
    "bankruptcy_distress": r"bankrupt|chapter 11|going concern|delist|default|restructur",
    "buyback":             r"buyback|repurchase|tender for its",
}
_THEME_RX = {name: re.compile(pat, re.I) for name, pat in _CATALYST_THEMES.items()}


def classify_themes(headlines: list[str]) -> list[str]:
    """Return the catalyst themes any of these headlines hit (deduped, ordered).
    Pure. Empty list = matched nothing (likely commentary, not a catalyst)."""
    hit: list[str] = []
    blob = " \n ".join(h for h in headlines if h)
    for name, rx in _THEME_RX.items():
        if rx.search(blob):
            hit.append(name)
    return hit


def aggregate_no_detector_themes(rows: list[dict]) -> dict[str, int]:
    """Tally catalyst themes across the no_detector movers in one audit.
    A mover can hit multiple themes; ``_uncategorised`` counts movers that hit none
    (the commentary-noise floor). Pure — drives the per-day decision view."""
    tally: dict[str, int] = {}
    uncat = 0
    for r in rows:
        if r.get("reason") != "no_detector":
            continue
        themes = r.get("themes") or []
        if not themes:
            uncat += 1
            continue
        for t in themes:
            tally[t] = tally.get(t, 0) + 1
    out = dict(sorted(tally.items(), key=lambda kv: -kv[1]))
    out["_uncategorised"] = uncat
    return out


def _norm(text: str) -> str:
    """Normalise a headline for fuzzy membership comparison."""
    return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()


def _norm_ticker(symbol: str) -> str:
    """US tickers are bare; LSE come back as 'VOD.L'. Compare on the bare upper root."""
    s = (symbol or "").upper().strip()
    return s[:-2] if s.endswith(".L") else s


# ---------------------------------------------------------------------------
# Classifiers — collect every detector's module-level classify(title, summary)
# ---------------------------------------------------------------------------
def _load_classifiers() -> list[tuple[str, Callable]]:
    from switching.registry import all_detectors, load_builtin_detectors
    load_builtin_detectors()
    out: list[tuple[str, Callable]] = []
    for name, cls in all_detectors().items():
        try:
            mod = importlib.import_module(cls.__module__)
            fn = getattr(mod, "classify", None)
            if callable(fn):
                out.append((name, fn))
        except Exception:   # a detector without a text classifier (e.g. EDGAR) is skipped
            continue
    return out


# ---------------------------------------------------------------------------
# Pure attribution — the testable core
# ---------------------------------------------------------------------------
def attribute(
    mover: dict,
    headlines: list[str],
    seen_tickers: set[str],
    ingested_titles: set[str],
    classifiers: list[tuple[str, Callable]],
) -> dict:
    """Classify ONE mover into a reason bucket. Pure: no IO."""
    sym = mover.get("symbol", "")
    base = {
        "symbol": sym,
        "name": mover.get("name", ""),
        "pct_change": round(mover.get("pct_change", 0.0), 2),
        "price": mover.get("price"),
        "vol_ratio": mover.get("vol_ratio"),
        "had_earnings": mover.get("had_earnings", False),
        "detector": None,
        "evidence": "",
    }

    if _norm_ticker(sym) in seen_tickers:
        return {**base, "status": "caught", "reason": "caught"}

    # Does any detector we run classify any of this mover's headlines?
    match = None
    for name, fn in classifiers:
        for h in headlines:
            try:
                if fn(h, "") is not None:
                    match = (name, h)
                    break
            except Exception:
                continue
        if match:
            break

    if match:
        det, headline = match
        # We have a detector for it. Did we INGEST the story (saw it, dropped ticker)
        # or never receive it (feed coverage gap)?
        reason = "ticker_drop" if _norm(headline) in ingested_titles else "feed_gap"
        return {**base, "status": "missed", "reason": reason,
                "detector": det, "evidence": headline[:200]}

    if headlines:
        # Keep ALL headlines (bounded) + theme tags, not just headlines[0] — the
        # real catalyst can sit below a commentary headline, and the themes feed the
        # per-day "what detector are we missing?" tally.
        kept = [h[:200] for h in headlines[:_MAX_KEPT_HEADLINES]]
        return {**base, "status": "missed", "reason": "no_detector",
                "evidence": headlines[0][:200],
                "headlines": kept,
                "themes": classify_themes(headlines)}
    return {**base, "status": "missed", "reason": "no_news"}


# ---------------------------------------------------------------------------
# IO — yfinance movers + per-ticker news, and our own records
# ---------------------------------------------------------------------------
def _fetch_movers(market: str, limit: int = 25) -> list[dict]:
    """Top movers for the market. US uses Yahoo's predefined screeners; UK a
    region=gb EquityQuery filtered to real LSE operating stocks (depositary lines
    like 0LC7.L are dropped — same rule as the Investegate scraper)."""
    import yfinance as yf

    def _row(it: dict) -> dict:
        vol = it.get("regularMarketVolume") or 0
        avg = it.get("averageDailyVolume3Month") or it.get("averageDailyVolume10Day") or 0
        return {
            "symbol": it.get("symbol", ""),
            "name": str(it.get("shortName") or it.get("longName") or "")[:40],
            "pct_change": float(it.get("regularMarketChangePercent") or 0.0),
            "price": it.get("regularMarketPrice"),
            "vol_ratio": round(vol / avg, 1) if avg else None,
            "had_earnings": bool(it.get("earningsTimestamp")),
        }

    rows: dict[str, dict] = {}
    if market == "uk":
        from yfinance import EquityQuery as Q
        q = Q("and", [Q("gt", ["percentchange", 3]), Q("eq", ["region", "gb"])])
        res = yf.screen(q, sortField="percentchange", sortAsc=False, size=100)
        quotes = res.get("quotes", []) if isinstance(res, dict) else (res or [])
        for it in quotes:
            sym = it.get("symbol", "")
            root = sym[:-2] if sym.endswith(".L") else sym
            # drop depositary / foreign cross-listings (digit-leading EPIC) and non-LSE
            if not sym.endswith(".L") or (root and root[0].isdigit()):
                continue
            rows.setdefault(sym, _row(it))
    else:
        for key in ("day_gainers", "most_actives"):
            try:
                res = yf.screen(key)
                quotes = res.get("quotes", []) if isinstance(res, dict) else (res or [])
                for it in quotes:
                    if it.get("symbol"):
                        rows.setdefault(it["symbol"], _row(it))
            except Exception as exc:
                log.warning("movers screen(%s) failed: %s", key, exc)

    out = sorted(rows.values(), key=lambda r: abs(r["pct_change"]), reverse=True)
    return out[:limit]


def _fetch_headlines(symbol: str, limit: int = 8, source: str = "yfinance") -> list[str]:
    """Recent headline titles for a ticker, from the chosen news source.

    'benzinga' uses the Benzinga News API (real catalyst headlines, pre-tagged
    tickers); 'yfinance' uses Yahoo's per-ticker .news (lagging commentary). The
    A/B point of the audit: compare which source actually surfaces the catalyst.
    """
    if source == "benzinga":
        from switching.sources import benzinga
        items = benzinga.fetch_news(tickers=[symbol], display_output="abstract", page_size=limit)
        return [it["title"] for it in items if it.get("title")][:limit]

    import yfinance as yf
    try:
        items = yf.Ticker(symbol).news or []
    except Exception as exc:
        log.warning("news fetch failed for %s: %s", symbol, exc)
        return []
    titles: list[str] = []
    for it in items[:limit]:
        t = (it.get("content") or {}).get("title") if isinstance(it.get("content"), dict) else None
        t = t or it.get("title")
        if t:
            titles.append(str(t))
    return titles


def _our_records(state_path: Path, service: str) -> tuple[set[str], set[str]]:
    """Return (seen_tickers, ingested_titles): tickers we signalled/traded, and the
    normalised titles of stories we actually INGESTED (funnel drops + last signals)."""
    from switching import detection_funnel, storage
    from switching.paper_trader import Portfolio

    seen: set[str] = set()
    titles: set[str] = set()
    try:
        p = Portfolio.load(state_path)
        for t in p.trades:
            seen.add(_norm_ticker(t.ticker))
        for pos in p.positions:
            seen.add(_norm_ticker(pos.ticker))
        for sig in (p.last_signals or []):
            if sig.get("ticker"):
                seen.add(_norm_ticker(sig["ticker"]))
            if sig.get("headline"):
                titles.add(_norm(sig["headline"]))
    except Exception as exc:
        log.warning("could not load portfolio %s: %s", state_path, exc)
    try:
        for d in detection_funnel.load_drops(state_path, limit=1000):
            if d.get("headline"):
                titles.add(_norm(d["headline"]))
    except Exception as exc:
        log.warning("could not load funnel drops: %s", exc)
    return seen, titles


# ---------------------------------------------------------------------------
# Orchestration + persistence
# ---------------------------------------------------------------------------
def run_audit(state_path: Path, market: str = "us", limit: int = 25,
              news_per_ticker: int = 8, news_source: str = "auto") -> dict:
    """Pull movers, attribute each, persist + return the report.

    news_source: 'yfinance' | 'benzinga' | 'auto' (benzinga if BENZINGA_API_KEY is
    set, else yfinance). Recorded in the report so A/B runs are distinguishable.
    """
    from switching import storage
    from switching.sources import benzinga
    if news_source == "auto":
        news_source = "benzinga" if benzinga.is_configured() else "yfinance"

    service = storage.service_from_path(state_path)
    classifiers = _load_classifiers()
    seen, titles = _our_records(state_path, service)

    movers = _fetch_movers(market, limit=limit)
    rows = []
    for m in movers:
        headlines = _fetch_headlines(m["symbol"], limit=news_per_ticker, source=news_source)
        rows.append(attribute(m, headlines, seen, titles, classifiers))

    summary = {r: sum(1 for x in rows if x["reason"] == r) for r in _REASONS}
    report = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "market": market,
        "news_source": news_source,
        "count": len(rows),
        "summary": summary,
        # Per-day catalyst-theme tally over the no_detector bucket — accumulates the
        # "which detector are we missing?" decision view. Trust it only on a clean
        # catalyst source (Benzinga); on yfinance expect a high _uncategorised floor.
        "no_detector_themes": aggregate_no_detector_themes(rows),
        "movers": rows,
    }
    save_audit(state_path.parent, market, report)
    return report


_KEEP_DAYS = 90   # bound disk growth — keep ~a quarter of daily audits per market


def _market_dir(state_dir: Path, market: str) -> Path:
    return state_dir / _AUDIT_DIR / market


def save_audit(state_dir: Path, market: str, report: dict) -> Path:
    """Persist one day's audit to ``<state_dir>/movers_audit/<market>/<YYYY-MM-DD>.json``.
    One file per trading day so history accumulates (we never overwrite a prior day)."""
    day = (report.get("generated_at") or datetime.now(tz=timezone.utc).isoformat())[:10]
    out_dir = _market_dir(state_dir, market)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{day}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    # prune oldest beyond _KEEP_DAYS
    files = sorted(out_dir.glob("*.json"), reverse=True)
    for stale in files[_KEEP_DAYS:]:
        try:
            stale.unlink()
        except OSError:
            pass
    return out


def audit_dates(state_dir: Path, market: str = "us") -> list[str]:
    """Available audit dates (YYYY-MM-DD), newest first."""
    d = _market_dir(state_dir, market)
    if not d.exists():
        return []
    return sorted((p.stem for p in d.glob("*.json")), reverse=True)


def load_audit(state_dir: Path, market: str = "us", date: str | None = None) -> dict | None:
    """Load one day's audit (newest if ``date`` is None)."""
    d = _market_dir(state_dir, market)
    if not d.exists():
        return None
    if date:
        p = d / f"{date}.json"
    else:
        files = sorted(d.glob("*.json"), reverse=True)
        p = files[0] if files else None
    if not p or not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
