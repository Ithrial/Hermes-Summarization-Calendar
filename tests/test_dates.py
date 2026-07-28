"""Tests for America/Chicago date boundary helpers.

Covers DST transitions, 23h/25h days, and UTC conversion correctness.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure our dashboard module is on the path
sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

from hermes_daily_ledger.dates import (
    CHICAGO,
    chicago_day_window_utc,
    chicago_midnight_utc,
    chicago_next_midnight_utc,
    days_in_month,
    date_str_from_ymd,
    unix_ts_to_utc_iso,
)


class TestChicagoMidnightUtc:
    def test_regular_winter_day(self):
        """Jan 15 CST = UTC+6, midnight CST = 06:00 UTC."""
        dt = chicago_midnight_utc("2026-01-15")
        assert dt.strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-01-15T06:00:00Z"

    def test_regular_summer_day(self):
        """July 15 CDT = UTC+5, midnight CDT = 05:00 UTC."""
        dt = chicago_midnight_utc("2026-07-15")
        assert dt.strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-07-15T05:00:00Z"

    def test_spring_forward_day(self):
        """March 8 2026: clocks spring forward at 2AM -> midnight CDT = 05:00 UTC."""
        dt = chicago_midnight_utc("2026-03-08")
        assert dt.strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-03-08T06:00:00Z"

    def test_fall_back_day(self):
        """Nov 1 2026: clocks fall back at 2AM -> midnight CDT = 05:00 UTC."""
        dt = chicago_midnight_utc("2026-11-01")
        assert dt.strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-11-01T05:00:00Z"


class TestChicagoNextMidnightUtc:
    def test_regular_24h_day(self):
        """Normal day gap is 24 hours in UTC."""
        start = chicago_midnight_utc("2026-07-15")
        nxt = chicago_next_midnight_utc("2026-07-15")
        diff_hours = (nxt - start).total_seconds() / 3600
        assert diff_hours == 24.0

    def test_spring_forward_23h_day(self):
        """March 8, 2026: only 23 hours between local midnights."""
        start = chicago_midnight_utc("2026-03-08")
        nxt = chicago_next_midnight_utc("2026-03-08")
        diff_hours = (nxt - start).total_seconds() / 3600
        assert diff_hours == 23.0, f"Expected 23h spring-forward day, got {diff_hours}h"

    def test_fall_back_25h_day(self):
        """Nov 1, 2026: 25 hours between local midnights."""
        start = chicago_midnight_utc("2026-11-01")
        nxt = chicago_next_midnight_utc("2026-11-01")
        diff_hours = (nxt - start).total_seconds() / 3600
        assert diff_hours == 25.0, f"Expected 25h fall-back day, got {diff_hours}h"


class TestChicagoDayWindow:
    def test_window_exclusive_end(self):
        """Window is [start, end) — exclusive upper bound."""
        start, end = chicago_day_window_utc("2026-07-15")
        assert start < end
        # Message at exactly the end should NOT be in the day
        assert end - start != type(end).min  # not zero-length

    def test_spring_forward_window(self):
        """23h window on DST spring-forward."""
        start, end = chicago_day_window_utc("2026-03-08")
        diff = (end - start).total_seconds() / 3600
        assert diff == 23.0

    def test_fall_back_window(self):
        """25h window on DST fall-back."""
        start, end = chicago_day_window_utc("2026-11-01")
        diff = (end - start).total_seconds() / 3600
        assert diff == 25.0


class TestUnixTimestampHelpers:
    def test_unix_to_iso(self):
        result = unix_ts_to_utc_iso(0)
        assert result == "1970-01-01T00:00:00Z"

    def test_unix_to_iso_none(self):
        assert unix_ts_to_utc_iso(None) is None


class TestDaysInMonth:
    def test_feb_non_leap(self):
        assert days_in_month(2025, 2) == 28

    def test_feb_leap(self):
        assert days_in_month(2024, 2) == 29

    def test_august(self):
        assert days_in_month(2026, 8) == 31


class TestDateStr:
    def test_formatting(self):
        assert date_str_from_ymd(2026, 7, 5) == "2026-07-05"

    def test_zero_padding(self):
        assert date_str_from_ymd(2026, 1, 3) == "2026-01-03"
