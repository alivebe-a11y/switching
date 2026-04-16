from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from switching.signal import Signal

# Repo-relative path. Resolved at call-time so tests can point elsewhere.
_DEFAULT_ROOT = Path(__file__).resolve().parents[3] / "data" / "historical_events"


def _parse_dt(value: str) -> datetime:
    # Accept both date-only and full ISO timestamps; normalize to UTC.
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        dt = datetime.strptime(value, "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load(detector: str, root: Path | None = None) -> list[Signal]:
    """Load curated historical events for a given detector.

    The CSV schema mirrors Signal fields so the backtester can reuse the same
    machinery as live scans. Extra columns beyond the schema are ignored.
    """
    root = root or _DEFAULT_ROOT
    path = root / f"{detector}.csv"
    if not path.exists():
        return []
    out: list[Signal] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            out.append(
                Signal(
                    detector=detector,
                    ticker=row["ticker"].strip().upper(),
                    company=row["company"].strip(),
                    event_dt=_parse_dt(row["event_dt"].strip()),
                    headline=row["headline"].strip(),
                    url=row.get("url", "").strip(),
                    evidence=row.get("evidence", "").strip(),
                    severity=float(row.get("severity") or 0.5),
                )
            )
    return out


def iter_detectors(root: Path | None = None) -> Iterable[str]:
    root = root or _DEFAULT_ROOT
    if not root.exists():
        return
    for path in sorted(root.glob("*.csv")):
        yield path.stem
