"""Tests for the US market calendar utilities."""

from datetime import date, datetime, timezone

import pytest

from switching.market_calendar import (
    is_market_hours,
    is_trading_day,
    trading_days_between,
    trading_days_since,
)


# ---------------------------------------------------------------------------
# is_trading_day
# ---------------------------------------------------------------------------

def test_weekday_is_trading_day():
    assert is_trading_day(date(2025, 5, 5)) is True   # Monday

def test_saturday_not_trading():
    assert is_trading_day(date(2025, 5, 3)) is False  # Saturday

def test_sunday_not_trading():
    assert is_trading_day(date(2025, 5, 4)) is False  # Sunday

def test_known_holiday_not_trading():
    assert is_trading_day(date(2025, 12, 25)) is False  # Christmas

def test_good_friday_2025_not_trading():
    assert is_trading_day(date(2025, 4, 18)) is False

def test_thanksgiving_2025_not_trading():
    assert is_trading_day(date(2025, 11, 27)) is False

def test_day_before_holiday_is_trading():
    # Christmas Eve 2025 is Wednesday — market is open (no early close in list)
    assert is_trading_day(date(2025, 12, 24)) is True

def test_juneteenth_2025_not_trading():
    assert is_trading_day(date(2025, 6, 19)) is False

def test_independence_day_2026_observed_friday():
    # July 4, 2026 falls on Saturday → observed July 3 (Friday) is the holiday
    assert is_trading_day(date(2026, 7, 3)) is False   # observed holiday
    assert is_trading_day(date(2026, 7, 4)) is False   # Saturday anyway

def test_christmas_2027_observed_friday():
    # Dec 25, 2027 falls on Saturday → observed Dec 24 (Friday)
    assert is_trading_day(date(2027, 12, 24)) is False  # observed holiday
    assert is_trading_day(date(2027, 12, 23)) is True   # Wednesday before


# ---------------------------------------------------------------------------
# is_market_hours
# ---------------------------------------------------------------------------

def _utc(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)

def test_market_open_midday():
    # 14:00 UTC = 10:00 ET (EDT, UTC-4) on a Tuesday — market is open
    assert is_market_hours(_utc(2025, 5, 6, 14, 0)) is True

def test_market_closed_early_morning():
    # 08:00 UTC = 04:00 ET — market is closed
    assert is_market_hours(_utc(2025, 5, 6, 8, 0)) is False

def test_market_closed_after_hours():
    # 21:00 UTC = 17:00 ET — market is closed
    assert is_market_hours(_utc(2025, 5, 6, 21, 0)) is False

def test_market_closed_on_saturday():
    assert is_market_hours(_utc(2025, 5, 3, 14, 0)) is False

def test_market_closed_on_holiday():
    # Christmas 2025 at noon ET
    assert is_market_hours(_utc(2025, 12, 25, 16, 0)) is False


# ---------------------------------------------------------------------------
# trading_days_between
# ---------------------------------------------------------------------------

def test_mon_to_fri_is_four_days():
    # Mon → Fri: Tue Wed Thu Fri = 4 trading days
    assert trading_days_between(date(2025, 5, 5), date(2025, 5, 9)) == 4

def test_fri_to_mon_skips_weekend():
    # Fri → Mon: only Monday counts = 1 trading day
    assert trading_days_between(date(2025, 5, 2), date(2025, 5, 5)) == 1

def test_same_day_is_zero():
    assert trading_days_between(date(2025, 5, 5), date(2025, 5, 5)) == 0

def test_end_before_start_is_zero():
    assert trading_days_between(date(2025, 5, 9), date(2025, 5, 5)) == 0

def test_span_including_holiday():
    # Wednesday April 16 → Wednesday April 23, 2025
    # Good Friday April 18 is a holiday → only 4 trading days (Mon 21, Tue 22, Wed 23 + Tue 15... wait)
    # Apr 16 (Wed) → Apr 23 (Wed): Thu 17, Fri 18 (holiday), Mon 21, Tue 22, Wed 23 = 4 trading days
    assert trading_days_between(date(2025, 4, 16), date(2025, 4, 23)) == 4

def test_hold_3_days_from_thursday_ends_tuesday():
    # Thu May 1 + 3 trading days: Fri=1, Mon=2, Tue=3 → ends Tuesday May 6
    start = date(2025, 5, 1)
    count = 0
    target = start
    for i in range(1, 30):
        from datetime import timedelta
        candidate = start + timedelta(days=i)
        from switching.market_calendar import is_trading_day as itd
        if itd(candidate):
            count += 1
        if count == 3:
            target = candidate
            break
    assert target == date(2025, 5, 6)  # Tuesday


# ---------------------------------------------------------------------------
# trading_days_since (integration with real timestamps)
# ---------------------------------------------------------------------------

def test_trading_days_since_parses_iso_with_z():
    """Should not raise on 'Z' suffix timestamps."""
    # Just check it runs without error; can't assert exact value (depends on today)
    result = trading_days_since("2024-01-15T10:00:00Z")
    assert result >= 0

def test_trading_days_since_parses_iso_with_offset():
    result = trading_days_since("2024-01-15T10:00:00+00:00")
    assert result >= 0

def test_trading_days_since_bad_string_returns_zero():
    assert trading_days_since("not-a-date") == 0

def test_trading_days_since_future_returns_zero():
    # A date far in the future should return 0 (end <= start)
    assert trading_days_since("2099-01-01T00:00:00+00:00") == 0


# ---------------------------------------------------------------------------
# Half-day closes (1:00 pm ET early close)
# ---------------------------------------------------------------------------

def test_market_open_at_noon_on_half_day():
    # Black Friday 2025 — half-day. 12:00 ET should still be OPEN.
    # 12:00 ET (EST = UTC-5) = 17:00 UTC
    bf_noon = datetime(2025, 11, 28, 17, 0, tzinfo=timezone.utc)
    assert is_market_hours(bf_noon) is True

def test_market_closed_at_1pm_on_half_day():
    # Black Friday 2025 — half-day. 13:00 ET = 18:00 UTC, market CLOSED.
    bf_1pm = datetime(2025, 11, 28, 18, 0, tzinfo=timezone.utc)
    assert is_market_hours(bf_1pm) is False

def test_market_still_open_at_2pm_on_normal_day():
    # Regular Wednesday in Nov 2025 (no half-day) — 14:00 ET = 19:00 UTC, OPEN.
    normal = datetime(2025, 11, 26, 19, 0, tzinfo=timezone.utc)
    assert is_market_hours(normal) is True

def test_market_closed_at_2pm_on_christmas_eve_half_day():
    # Christmas Eve 2025 (Wed) — half-day, 14:00 ET = 19:00 UTC.
    xe = datetime(2025, 12, 24, 19, 0, tzinfo=timezone.utc)
    assert is_market_hours(xe) is False


# ---------------------------------------------------------------------------
# minutes_since_us_open
# ---------------------------------------------------------------------------

def test_minutes_since_us_open_at_open():
    # 14:30 UTC in winter = 09:30 ET, market just opened
    open_t = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    from switching.market_calendar import minutes_since_us_open
    result = minutes_since_us_open(open_t)
    assert result is not None
    assert 0 <= result < 1.0

def test_minutes_since_us_open_30_min_later():
    # 30 minutes after open
    later = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
    from switching.market_calendar import minutes_since_us_open
    result = minutes_since_us_open(later)
    assert result is not None
    assert 29 <= result <= 31

def test_minutes_since_us_open_returns_none_when_closed():
    # Saturday — market closed
    sat = datetime(2026, 1, 3, 15, 0, tzinfo=timezone.utc)
    from switching.market_calendar import minutes_since_us_open
    assert minutes_since_us_open(sat) is None


# ---------------------------------------------------------------------------
# LSE half-day closes (12:30 London early close)
# ---------------------------------------------------------------------------

def test_lse_open_at_noon_on_half_day():
    from switching.market_calendar import is_lse_hours
    # Christmas Eve 2025 (Wed) — half-day. 12:00 London = 12:00 UTC (GMT in winter).
    # Market should still be OPEN at noon.
    xe_noon = datetime(2025, 12, 24, 12, 0, tzinfo=timezone.utc)
    assert is_lse_hours(xe_noon) is True

def test_lse_closed_at_1230_on_half_day():
    from switching.market_calendar import is_lse_hours
    # Christmas Eve 2025 — 12:30 London = 12:30 UTC, market CLOSED.
    xe_1230 = datetime(2025, 12, 24, 12, 30, tzinfo=timezone.utc)
    assert is_lse_hours(xe_1230) is False

def test_lse_open_at_3pm_on_normal_day():
    from switching.market_calendar import is_lse_hours
    # Regular Mon Nov 24 2025 — 15:00 London = 15:00 UTC, OPEN.
    normal = datetime(2025, 11, 24, 15, 0, tzinfo=timezone.utc)
    assert is_lse_hours(normal) is True

def test_lse_nye_half_day():
    from switching.market_calendar import is_lse_hours
    # New Year's Eve 2025 (Wed) — half-day, 13:00 London = 13:00 UTC, CLOSED.
    nye = datetime(2025, 12, 31, 13, 0, tzinfo=timezone.utc)
    assert is_lse_hours(nye) is False
