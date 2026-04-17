from __future__ import annotations

import csv
import importlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, TYPE_CHECKING

from switching.signal import Signal

if TYPE_CHECKING:  # avoid import cycle at runtime
    from switching.sources.sec_edgar import EdgarClient

log = logging.getLogger(__name__)

def _find_data_root() -> Path:
    pkg_data = Path(__file__).resolve().parents[1] / "data" / "historical_events"
    if pkg_data.is_dir():
        return pkg_data
    repo_data = Path(__file__).resolve().parents[3] / "data" / "historical_events"
    if repo_data.is_dir():
        return repo_data
    return pkg_data


_DEFAULT_ROOT = _find_data_root()


def _parse_dt(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        dt = datetime.strptime(value, "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load(
    detector: str,
    root: Path | None = None,
    *,
    live: "EdgarClient | None" = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[Signal]:
    """Load curated historical events for a detector.

    Always reads the hand-curated seed from ``data/historical_events/<name>.csv``.
    If ``live`` is provided, also calls the detector module's optional
    ``pull_live(client, since, until)`` hook and merges the results (deduped
    via ``Signal.dedup_key``).
    """
    seeds = _load_seed(detector, root=root)
    if live is None:
        return seeds
    merged = seeds + _pull_live(detector, live, since=since, until=until)
    return _dedup(merged)


def _load_seed(detector: str, *, root: Path | None = None) -> list[Signal]:
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


def _pull_live(
    detector: str,
    client: "EdgarClient",
    *,
    since: datetime | None,
    until: datetime | None,
) -> list[Signal]:
    try:
        mod = importlib.import_module(f"switching.detectors.{detector}")
    except ImportError as exc:
        log.warning("cannot import detector %s for live seed: %s", detector, exc)
        return []
    hook = getattr(mod, "pull_live", None)
    if not callable(hook):
        return []
    try:
        return list(hook(client, since=since, until=until))
    except Exception as exc:
        log.warning("live-seed pull failed for %s: %s", detector, exc)
        return []


def _dedup(signals: Iterable[Signal]) -> list[Signal]:
    seen: set[tuple[str, str, str]] = set()
    out: list[Signal] = []
    for s in signals:
        key = s.dedup_key()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def iter_detectors(root: Path | None = None) -> Iterable[str]:
    root = root or _DEFAULT_ROOT
    if not root.exists():
        return
    for path in sorted(root.glob("*.csv")):
        yield path.stem
