"""Tests für Wochentagsanzeige und Wochenrechnung."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.formatting import (
    format_date,
    format_datetime,
    format_period,
    shift_period,
    week_of,
    weekday_short,
)


@pytest.mark.parametrize(
    "day, expected",
    [
        (date(2000, 1, 31), "Mo"),
        (date(2000, 2, 1), "Di"),
        (date(2000, 2, 2), "Mi"),
        (date(2000, 2, 3), "Do"),
        (date(2000, 2, 4), "Fr"),
        (date(2000, 2, 5), "Sa"),
        (date(2000, 2, 6), "So"),
    ],
)
def test_weekday_short_covers_whole_week(day, expected):
    assert weekday_short(day) == expected


def test_weekday_short_accepts_datetime_and_none():
    assert weekday_short(datetime(2000, 1, 31, 23, 59)) == "Mo"
    assert weekday_short(None) == ""


def test_format_date_puts_weekday_next_to_date():
    assert format_date(date(2000, 1, 31)) == "Mo 31.01.2000"
    assert format_date(None) == "—"


def test_format_datetime_keeps_time():
    assert format_datetime(datetime(2000, 1, 31, 19, 45)) == "Mo 31.01.2000 19:45"
    assert format_datetime(None) == "—"


def test_format_period_shows_both_weekdays():
    assert (
        format_period(date(2000, 1, 31), date(2000, 2, 7))
        == "Mo 31.01.2000 – Mo 07.02.2000"
    )


def test_shift_period_moves_exactly_one_week():
    """Das Beispiel aus dem Alltag: Mo–Mo wird zum darauffolgenden Mo–Mo."""
    start, end = shift_period(date(2000, 1, 31), date(2000, 2, 7))

    assert (start, end) == (date(2000, 2, 7), date(2000, 2, 14))
    assert weekday_short(start) == weekday_short(end) == "Mo"


def test_shift_period_crosses_month_and_leap_day():
    start, end = shift_period(date(2000, 2, 24), date(2000, 3, 2))

    assert (start, end) == (date(2000, 3, 2), date(2000, 3, 9))  # 29.02.2000 mitgezählt


def test_shift_period_crosses_year_boundary():
    assert shift_period(date(1999, 12, 27), date(2000, 1, 3)) == (
        date(2000, 1, 3),
        date(2000, 1, 10),
    )


def test_shift_period_backwards():
    assert shift_period(date(2000, 2, 7), date(2000, 2, 14), weeks=-1) == (
        date(2000, 1, 31),
        date(2000, 2, 7),
    )


def test_shift_period_keeps_length_of_uncommon_ranges():
    start, end = shift_period(date(2000, 1, 1), date(2000, 1, 3))

    assert (end - start).days == 2  # Länge bleibt unverändert


@pytest.mark.parametrize("anchor", [date(2000, 1, 31), date(2000, 2, 2), date(2000, 2, 6)])
def test_week_of_snaps_to_monday_monday(anchor):
    """Jeder Tag der Woche ergibt denselben Montag-bis-Montag-Zuschnitt."""
    assert week_of(anchor) == (date(2000, 1, 31), date(2000, 2, 7))
