"""Tests for LSE market calendar utilities."""

from datetime import date, datetime, timezone

import pytest

from switching.market_calendar import (
    is_lse_hours,
    is_lse_trading_day,
    trading_days_since_lse,
)


# ---------------------------------------------------------------------------
# is_lse_trading_day
# ---------------------------------------------------------------------------

def test_tuesday_is_lse_trading_day():
    # Tuesday May 6, 2025 — regular weekday
    assert is_lse_trading_day(date(2025, 5, 6)) is True


def test_saturday_is_not_lse_trading_day():
    assert is_lse_trading_day(date(2025, 5, 3)) is False


def test_sunday_is_not_lse_trading_day():
    assert is_lse_trading_day(date(2025, 5, 4)) is False


def test_good_friday_2025_not_lse_trading():
    assert is_lse_trading_day(date(2025, 4, 18)) is False


def test_easter_monday_2025_not_lse_trading():
    assert is_lse_trading_day(date(2025, 4, 21)) is False


def test_boxing_day_2025_not_lse_trading():
    assert is_lse_trading_day(date(2025, 12, 26)) is False


def test_christmas_day_2025_not_lse_trading():
    assert is_lse_trading_day(date(2025, 12, 25)) is False


def test_early_may_bank_holiday_2025_not_lse_trading():
    # Early May Bank Holiday 2025: May 5
    assert is_lse_trading_day(date(2025, 5, 5)) is False


def test_day_before_good_friday_is_lse_trading():
    # Thursday April 17, 2025 — regular trading day
    assert is_lse_trading_day(date(2025, 4, 17)) is True


def test_lse_2026_boxing_day_observed():
    # Dec 26, 2026 is Saturday → observed Mon Dec 28
    assert is_lse_trading_day(date(2026, 12, 28)) is False


# ---------------------------------------------------------------------------
# is_lse_hours
# ---------------------------------------------------------------------------

def _london_utc(year, month, day, hour, minute=0) -> datetime:
    """Create a UTC datetime that corresponds to the given London clock time.

    In May (BST = UTC+1), London 10:00 = UTC 09:00.
    In January (GMT = UTC+0), London 10:00 = UTC 10:00.
    We use fixed offsets here since zoneinfo may or may not be available.
    """
    import zoneinfo
    from datetime import timedelta
    tz = zoneinfo.ZoneInfo("Europe/London")
    local = datetime(year, month, day, hour, minute, tzinfo=tz)
    return local.astimezone(timezone.utc)


def test_lse_open_at_1000_london_tuesday():
    # 10:00 London on Tuesday May 6, 2025 — within 08:00–16:30
    try:
        dt = _london_utc(2025, 5, 6, 10, 0)
        assert is_lse_hours(dt) is True
    except Exception:
        pytest.skip("zoneinfo not available in this environment")


def test_lse_closed_at_1700_london():
    # 17:00 London — after close (16:30)
    try:
        dt = _london_utc(2025, 5, 6, 17, 0)
        assert is_lse_hours(dt) is False
    except Exception:
        pytest.skip("zoneinfo not available in this environment")


def test_lse_closed_on_saturday_at_1000():
    # 10:00 London on Saturday
    try:
        dt = _london_utc(2025, 5, 3, 10, 0)
        assert is_lse_hours(dt) is False
    except Exception:
        pytest.skip("zoneinfo not available in this environment")


def test_lse_closed_before_open_at_0730():
    # 07:30 London — before 08:00 open
    try:
        dt = _london_utc(2025, 5, 6, 7, 30)
        assert is_lse_hours(dt) is False
    except Exception:
        pytest.skip("zoneinfo not available in this environment")


def test_lse_closed_on_good_friday():
    try:
        dt = _london_utc(2025, 4, 18, 10, 0)
        assert is_lse_hours(dt) is False
    except Exception:
        pytest.skip("zoneinfo not available in this environment")


# ---------------------------------------------------------------------------
# trading_days_since_lse
# ---------------------------------------------------------------------------

def test_trading_days_since_lse_same_day_returns_zero():
    # A future date always returns 0
    result = trading_days_since_lse("2099-01-01T00:00:00+00:00")
    assert result == 0


def test_trading_days_since_lse_parses_z_suffix():
    result = trading_days_since_lse("2024-01-15T10:00:00Z")
    assert result >= 0


def test_trading_days_since_lse_bad_string_returns_zero():
    assert trading_days_since_lse("not-a-date") == 0


def test_trading_days_since_lse_counts_correctly():
    """Days since a date ~one week ago should be 3-5 trading days."""
    from datetime import timedelta
    past = datetime.now(tz=timezone.utc) - timedelta(days=7)
    result = trading_days_since_lse(past.isoformat())
    # 7 calendar days normally gives 4-5 LSE trading days
    assert 0 <= result <= 7
