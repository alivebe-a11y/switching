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
        return {**base, "status": "missed", "reason": "no_detector",
                "evidence": headlines[0][:200]}
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


def _fetch_headlines(symbol: str, limit: int = 8) -> list[str]:
    """Recent headline titles for a ticker (handles old flat + new content-nested shapes)."""
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
              news_per_ticker: int = 8) -> dict:
    """Pull movers, attribute each, persist + return the report."""
    from switching import storage
    service = storage.service_from_path(state_path)
    classifiers = _load_classifiers()
    seen, titles = _our_records(state_path, service)

    movers = _fetch_movers(market, limit=limit)
    rows = []
    for m in movers:
        headlines = _fetch_headlines(m["symbol"], limit=news_per_ticker)
        rows.append(attribute(m, headlines, seen, titles, classifiers))

    summary = {r: sum(1 for x in rows if x["reason"] == r) for r in _REASONS}
    report = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "market": market,
        "count": len(rows),
        "summary": summary,
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
