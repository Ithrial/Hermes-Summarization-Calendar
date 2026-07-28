"""America/Chicago date boundary helpers.

Day windows use Chicago local midnight-to-midnight (not a flat +24h UTC
offset) so DST transitions produce correct 23-hour and 25-hour days.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")


def chicago_midnight_utc(date_str: str) -> datetime:
    """Return UTC datetime for the start of *date_str* in Chicago time.

    Parameters
    ----------
    date_str:
        Calendar date in ``YYYY-MM-DD`` format.

    Returns
    -------
    datetime in UTC (e.g. 2026-03-08T06:00:00+00:00 on spring-forward day).
    """
    naive = datetime.strptime(date_str, "%Y-%m-%d")
    local = naive.replace(tzinfo=CHICAGO)
    return local.astimezone(timezone.utc)


def chicago_next_midnight_utc(date_str: str) -> datetime:
    """Return UTC datetime for the *next* calendar midnight in Chicago.

    This is NOT ``chicago_midnight_utc + 24h`` — on spring-forward days the
    gap is only 23 hours, and on fall-back it is 25 hours.

    Parameters
    ----------
    date_str:
        Calendar date in ``YYYY-MM-DD`` format.
    """
    naive = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
    local = naive.replace(tzinfo=CHICAGO)
    return local.astimezone(timezone.utc)


def chicago_day_window_utc(date_str: str) -> tuple[datetime, datetime]:
    """Return ``(start_utc, end_utc)`` for a Chicago calendar day.

    The interval is ``[start_utc, end_utc)`` — inclusive start, exclusive end.
    """
    return (chicago_midnight_utc(date_str), chicago_next_midnight_utc(date_str))


def utc_to_iso_z(dt: datetime | None) -> str | None:
    """Format a UTC datetime as ``YYYY-MM-DDTHH:MM:SSZ`` or return *None*."""
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def unix_ts_to_utc_iso(ts: float | None) -> str | None:
    """Convert a Unix REAL timestamp to ISO-8601 UTC string."""
    if ts is None:
        return None
    return utc_to_iso_z(datetime.fromtimestamp(ts, tz=timezone.utc))


def days_in_month(year: int, month: int) -> int:
    """Return the number of calendar days in a given year/month."""
    _, num = monthrange(year, month)
    return num


def date_str_from_ymd(year: int, month: int, day: int) -> str:
    """Format a date as ``YYYY-MM-DD`` from integers."""
    return f"{year:04d}-{month:02d}-{day:02d}"
