"""US equity market calendar utilities (with LSE support).

Provides trading-day counting and market-hours detection so that exit
logic (hold_expiry, first_green) only fires on real trading sessions.
Without this, a position entered Thursday with hold_days=3 would
"expire" on Sunday and exit at Friday's stale yfinance price.

Holiday list covers NYSE/NASDAQ observed holidays 2024–2027.
Update ``_HOLIDAYS`` each year — or set the ``SWITCHING_EXTRA_HOLIDAYS``
env var to a comma-separated list of ISO dates (e.g. ``2026-01-02``).

LSE support: ``is_lse_hours()``, ``is_lse_trading_day()``, and
``trading_days_since_lse()`` use the UK bank holiday calendar and
London time (Europe/London, handles BST/GMT automatically).
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
# NYSE / NASDAQ half-day closes (1:00 pm ET early close)
# ---------------------------------------------------------------------------
# Recurring half-days:
#   - Day before Independence Day (if July 4 falls on a weekday)
#   - Day after Thanksgiving (Black Friday)
#   - Christmas Eve (if a weekday)
# When Christmas/Independence Day fall on the weekend, the half-day pattern
# shifts and sometimes vanishes — date list is authoritative.
# Update each year from https://www.nyse.com/markets/hours-calendars.
# ---------------------------------------------------------------------------
_NYSE_HALF_DAYS: frozenset[date] = frozenset({
    # 2024
    date(2024,  7,  3),   # Wed before July 4 (Thu)
    date(2024, 11, 29),   # Black Friday
    date(2024, 12, 24),   # Christmas Eve (Tue)
    # 2025
    date(2025,  7,  3),   # Thu before July 4 (Fri)
    date(2025, 11, 28),   # Black Friday
    date(2025, 12, 24),   # Christmas Eve (Wed)
    # 2026
    # NOTE: July 4 2026 is Sat (observed Fri Jul 3, full holiday) — no half-day that week.
    date(2026, 11, 27),   # Black Friday
    date(2026, 12, 24),   # Christmas Eve (Thu)
    # 2027
    date(2027, 11, 26),   # Black Friday
    # July 4 2027 is Sun (observed Mon Jul 5) — Jul 2 Fri half-day per NYSE schedule
    date(2027,  7,  2),
    # Dec 25 2027 is Sat (observed Fri Dec 24, full holiday) — no half-day that week
})


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
    Half-day closes (1:00 pm ET) are honored — see ``_NYSE_HALF_DAYS``.
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
    after_open = (h, m) >= (9, 30)
    if et.date() in _NYSE_HALF_DAYS:
        before_close = (h, m) < (13, 0)
    else:
        before_close = (h, m) < (16, 0)
    return after_open and before_close


def minutes_since_us_open(now: datetime | None = None) -> float | None:
    """Minutes since today's NYSE/NASDAQ open, or None if the market is closed.

    Used by the trader loops to tighten the scan cadence in the first window
    after open so queued + just-arrived signals fire fast. Half-day-aware
    indirectly: returns None whenever ``is_market_hours`` is False.
    """
    now = now or datetime.now(tz=timezone.utc)
    if not is_market_hours(now):
        return None
    try:
        import zoneinfo
        et = now.astimezone(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        et = now.astimezone(timezone(timedelta(hours=-4)))
    open_dt = et.replace(hour=9, minute=30, second=0, microsecond=0)
    return (et - open_dt).total_seconds() / 60.0


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


# ---------------------------------------------------------------------------
# LSE / London Stock Exchange calendar (UK bank holidays 2024–2027)
# ---------------------------------------------------------------------------
# Rules (same as NYSE observed rules):
#   - If the holiday falls on Saturday → observed the preceding Friday
#   - If the holiday falls on Sunday  → observed the following Monday
# UK bank holidays that LSE observes:
#   New Year's Day, Good Friday, Easter Monday, Early May Bank Holiday,
#   Spring Bank Holiday, Summer Bank Holiday, Christmas Day, Boxing Day.
# ---------------------------------------------------------------------------

_LSE_HOLIDAYS: frozenset[date] = frozenset({
    # ── 2024 ──────────────────────────────────────────────────────────────
    date(2024,  1,  1),  # New Year's Day
    date(2024,  3, 29),  # Good Friday
    date(2024,  4,  1),  # Easter Monday
    date(2024,  5,  6),  # Early May Bank Holiday
    date(2024,  5, 27),  # Spring Bank Holiday
    date(2024,  8, 26),  # Summer Bank Holiday
    date(2024, 12, 25),  # Christmas Day
    date(2024, 12, 26),  # Boxing Day
    # ── 2025 ──────────────────────────────────────────────────────────────
    date(2025,  1,  1),  # New Year's Day
    date(2025,  4, 18),  # Good Friday
    date(2025,  4, 21),  # Easter Monday
    date(2025,  5,  5),  # Early May Bank Holiday
    date(2025,  5, 26),  # Spring Bank Holiday
    date(2025,  8, 25),  # Summer Bank Holiday
    date(2025, 12, 25),  # Christmas Day
    date(2025, 12, 26),  # Boxing Day
    # ── 2026 ──────────────────────────────────────────────────────────────
    date(2026,  1,  1),  # New Year's Day
    date(2026,  4,  3),  # Good Friday
    date(2026,  4,  6),  # Easter Monday
    date(2026,  5,  4),  # Early May Bank Holiday
    date(2026,  5, 25),  # Spring Bank Holiday
    date(2026,  8, 31),  # Summer Bank Holiday
    date(2026, 12, 25),  # Christmas Day
    date(2026, 12, 28),  # Boxing Day observed (Dec 26 is Saturday → Dec 28 Monday)
    # ── 2027 ──────────────────────────────────────────────────────────────
    date(2027,  1,  1),  # New Year's Day
    date(2027,  3, 26),  # Good Friday
    date(2027,  3, 29),  # Easter Monday
    date(2027,  5,  3),  # Early May Bank Holiday
    date(2027,  5, 31),  # Spring Bank Holiday
    date(2027,  8, 30),  # Summer Bank Holiday
    date(2027, 12, 27),  # Christmas Day observed (Dec 25 is Saturday → Dec 27 Monday)
    date(2027, 12, 28),  # Boxing Day observed (Dec 26 is Sunday → Dec 28 Tuesday)
})


# ---------------------------------------------------------------------------
# LSE half-day closes (12:30 London time early close)
# ---------------------------------------------------------------------------
# LSE closes early on Christmas Eve and New Year's Eve when those fall on a
# weekday. Update each year as needed.
# ---------------------------------------------------------------------------
_LSE_HALF_DAYS: frozenset[date] = frozenset({
    # 2024
    date(2024, 12, 24),   # Christmas Eve (Tue)
    date(2024, 12, 31),   # New Year's Eve (Tue)
    # 2025
    date(2025, 12, 24),   # Christmas Eve (Wed)
    date(2025, 12, 31),   # New Year's Eve (Wed)
    # 2026
    date(2026, 12, 24),   # Christmas Eve (Thu)
    date(2026, 12, 31),   # New Year's Eve (Thu)
    # 2027
    # NOTE: Dec 24 2027 is Fri but Dec 25 (Sat) is observed Mon Dec 27 — so
    # the FULL holiday is Dec 27 and Dec 24 trades a normal session (no half-day).
    date(2027, 12, 31),   # New Year's Eve (Fri)
})


def is_lse_trading_day(d: date | None = None) -> bool:
    """Return True if *d* is an LSE trading day.

    A trading day is any weekday (Mon–Fri) that is not a UK bank holiday.
    Defaults to today in London time when *d* is ``None``.
    """
    if d is None:
        try:
            import zoneinfo
            d = datetime.now(tz=zoneinfo.ZoneInfo("Europe/London")).date()
        except Exception:
            d = datetime.now(tz=timezone.utc).date()
    if d.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    return d not in _LSE_HOLIDAYS


def is_lse_hours(now: datetime | None = None) -> bool:
    """Return True if the LSE is currently open.

    Regular session: 08:00 – 16:30 London time (Europe/London), on LSE trading days.
    Half-day closes (12:30 London) on Christmas Eve / New Year's Eve are honored —
    see ``_LSE_HALF_DAYS``. zoneinfo handles BST/GMT transitions automatically.
    """
    now = now or datetime.now(tz=timezone.utc)
    try:
        import zoneinfo
        lt = now.astimezone(zoneinfo.ZoneInfo("Europe/London"))
    except Exception:
        # Fall back to UTC+1 (approximation — does not handle BST perfectly)
        lt = now.astimezone(timezone(timedelta(hours=1)))

    if not is_lse_trading_day(lt.date()):
        return False

    h, m = lt.hour, lt.minute
    after_open = (h, m) >= (8, 0)
    if lt.date() in _LSE_HALF_DAYS:
        before_close = (h, m) < (12, 30)
    else:
        before_close = (h, m) < (16, 30)
    return after_open and before_close


def minutes_since_lse_open(now: datetime | None = None) -> float | None:
    """Minutes since today's LSE open, or None if the LSE is closed."""
    now = now or datetime.now(tz=timezone.utc)
    if not is_lse_hours(now):
        return None
    try:
        import zoneinfo
        lt = now.astimezone(zoneinfo.ZoneInfo("Europe/London"))
    except Exception:
        lt = now.astimezone(timezone(timedelta(hours=1)))
    open_dt = lt.replace(hour=8, minute=0, second=0, microsecond=0)
    return (lt - open_dt).total_seconds() / 60.0


def _lse_trading_days_between(start: date, end: date) -> int:
    """Count LSE trading days strictly after *start* up to and including *end*."""
    if end <= start:
        return 0
    count = 0
    d = start
    while d < end:
        d += timedelta(days=1)
        if is_lse_trading_day(d):
            count += 1
    return count


def trading_days_since_lse(dt_str: str) -> int:
    """LSE trading days elapsed since the entry timestamp (ISO string) up to today.

    Analogous to ``trading_days_since`` but uses the LSE holiday calendar
    and London time for "today".  Returns 0 on parse error.
    """
    try:
        entry = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if entry.tzinfo is None:
            entry = entry.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return 0
    try:
        import zoneinfo
        today = datetime.now(tz=zoneinfo.ZoneInfo("Europe/London")).date()
    except Exception:
        today = datetime.now(tz=timezone.utc).date()
    return _lse_trading_days_between(entry.date(), today)
