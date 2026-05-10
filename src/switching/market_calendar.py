"""US equity market calendar utilities.

Provides trading-day counting and market-hours detection so that exit
logic (hold_expiry, first_green) only fires on real trading sessions.
Without this, a position entered Thursday with hold_days=3 would
"expire" on Sunday and exit at Friday's stale yfinance price.

Holiday list covers NYSE/NASDAQ observed holidays 2024–2027.
Update ``_HOLIDAYS`` each year — or set the ``SWITCHING_EXTRA_HOLIDAYS``
env var to a comma-separated list of ISO dates (e.g. ``2026-01-02``).
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# NYSE / NASDAQ observed holidays 2024–2027
# ---------------------------------------------------------------------------
# Rules:
#   - If the holiday falls on Saturday → observed the preceding Friday
#   - If the holiday falls on Sunday  → observed the following Monday
# ---------------------------------------------------------------------------

_HOLIDAYS: frozenset[date] = frozenset({
    # ── 2024 ──────────────────────────────────────────────────────────────
    date(2024,  1,  1),  # New Year's Day
    date(2024,  1, 15),  # Martin Luther King Jr. Day
    date(2024,  2, 19),  # Presidents' Day
    date(2024,  3, 29),  # Good Friday
    date(2024,  5, 27),  # Memorial Day
    date(2024,  6, 19),  # Juneteenth National Independence Day
    date(2024,  7,  4),  # Independence Day
    date(2024,  9,  2),  # Labor Day
    date(2024, 11, 28),  # Thanksgiving Day
    date(2024, 12, 25),  # Christmas Day
    # ── 2025 ──────────────────────────────────────────────────────────────
    date(2025,  1,  1),  # New Year's Day
    date(2025,  1, 20),  # Martin Luther King Jr. Day
    date(2025,  2, 17),  # Presidents' Day
    date(2025,  4, 18),  # Good Friday
    date(2025,  5, 26),  # Memorial Day
    date(2025,  6, 19),  # Juneteenth
    date(2025,  7,  4),  # Independence Day
    date(2025,  9,  1),  # Labor Day
    date(2025, 11, 27),  # Thanksgiving Day
    date(2025, 12, 25),  # Christmas Day
    # ── 2026 ──────────────────────────────────────────────────────────────
    date(2026,  1,  1),  # New Year's Day
    date(2026,  1, 19),  # Martin Luther King Jr. Day
    date(2026,  2, 16),  # Presidents' Day
    date(2026,  4,  3),  # Good Friday
    date(2026,  5, 25),  # Memorial Day
    date(2026,  6, 19),  # Juneteenth (falls on Friday)
    date(2026,  7,  3),  # Independence Day observed (July 4 = Saturday)
    date(2026,  9,  7),  # Labor Day
    date(2026, 11, 26),  # Thanksgiving Day
    date(2026, 12, 25),  # Christmas Day (falls on Friday)
    # ── 2027 ──────────────────────────────────────────────────────────────
    date(2027,  1,  1),  # New Year's Day
    date(2027,  1, 18),  # Martin Luther King Jr. Day
    date(2027,  2, 15),  # Presidents' Day
    date(2027,  3, 26),  # Good Friday
    date(2027,  5, 31),  # Memorial Day
    date(2027,  6, 18),  # Juneteenth observed (June 19 = Saturday)
    date(2027,  7,  5),  # Independence Day observed (July 4 = Sunday)
    date(2027,  9,  6),  # Labor Day
    date(2027, 11, 25),  # Thanksgiving Day
    date(2027, 12, 24),  # Christmas observed (Dec 25 = Saturday)
})


def _load_extra_holidays() -> frozenset[date]:
    """Load any extra holidays from SWITCHING_EXTRA_HOLIDAYS env var."""
    raw = os.environ.get("SWITCHING_EXTRA_HOLIDAYS", "")
    if not raw:
        return frozenset()
    extras: set[date] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            extras.add(date.fromisoformat(part))
        except ValueError:
            log.warning("SWITCHING_EXTRA_HOLIDAYS: cannot parse %r as ISO date", part)
    return frozenset(extras)


_ALL_HOLIDAYS: frozenset[date] = _HOLIDAYS | _load_extra_holidays()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_trading_day(d: date | None = None) -> bool:
    """Return True if *d* is a US equity market trading day.

    A trading day is any weekday (Mon–Fri) that is not in the holiday list.
    Defaults to today (UTC) when *d* is ``None``.
    """
    if d is None:
        d = datetime.now(tz=timezone.utc).date()
    if d.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    return d not in _ALL_HOLIDAYS


def is_market_hours(now: datetime | None = None) -> bool:
    """Return True if US equity markets are currently open.

    Regular session: 9:30 am – 4:00 pm US Eastern Time, on trading days.
    """
    now = now or datetime.now(tz=timezone.utc)
    try:
        import zoneinfo
        et = now.astimezone(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        # zoneinfo not available or timezone not found — approximate as UTC-4
        et = now.astimezone(timezone(timedelta(hours=-4)))

    if not is_trading_day(et.date()):
        return False

    h, m = et.hour, et.minute
    after_open  = (h, m) >= (9, 30)
    before_close = (h, m) < (16,  0)
    return after_open and before_close


def trading_days_between(start: date, end: date) -> int:
    """Count trading days strictly after *start* up to and including *end*.

    Example: Mon → Wed = 2 (Tuesday and Wednesday count).
    """
    if end <= start:
        return 0
    count = 0
    d = start
    while d < end:
        d += timedelta(days=1)
        if is_trading_day(d):
            count += 1
    return count


def trading_days_since(dt_str: str) -> int:
    """Trading days elapsed since the entry timestamp (ISO string) up to today.

    Parses the ISO datetime string from ``Position.entry_dt`` and counts
    trading days between that date and today (UTC).  Returns 0 on parse error.
    """
    try:
        entry = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if entry.tzinfo is None:
            entry = entry.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return 0
    today = datetime.now(tz=timezone.utc).date()
    return trading_days_between(entry.date(), today)
