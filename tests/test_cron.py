"""Cron expressions for schedules (V3 milestone 2)."""

from __future__ import annotations

from datetime import datetime

import pytest

from miniflow.cron import CronError, parse_cron
from miniflow.schedule import parse_schedules


def at(*args: int) -> float:
    return datetime(*args).timestamp()


def when(ts: float) -> tuple[int, int, int, int, int]:
    d = datetime.fromtimestamp(ts)
    return d.year, d.month, d.day, d.hour, d.minute


@pytest.mark.parametrize(
    ("expr", "now", "expected"),
    [
        ("30 9 * * *", at(2026, 10, 9, 12, 0), (2026, 10, 10, 9, 30)),
        ("30 9 * * *", at(2026, 10, 9, 8, 0), (2026, 10, 9, 9, 30)),
        ("*/15 * * * *", at(2026, 10, 9, 12, 7), (2026, 10, 9, 12, 15)),
        ("*/15 * * * *", at(2026, 10, 9, 12, 15), (2026, 10, 9, 12, 30)),  # strictly after
        ("*/15 * * * *", at(2026, 10, 9, 12, 59), (2026, 10, 9, 13, 0)),
        ("0 9 * * mon-fri", at(2026, 10, 9, 10, 0), (2026, 10, 12, 9, 0)),  # Fri 10:00 -> Mon
        ("0 9 * * 1-5", at(2026, 10, 9, 8, 0), (2026, 10, 9, 9, 0)),
        ("0 0 1 * *", at(2026, 10, 9, 0, 0), (2026, 11, 1, 0, 0)),
        ("0 0 29 2 *", at(2026, 10, 9, 0, 0), (2028, 2, 29, 0, 0)),  # next leap year
        ("0,30 * * * *", at(2026, 10, 9, 12, 5), (2026, 10, 9, 12, 30)),
        ("10-50/20 * * * *", at(2026, 10, 9, 12, 11), (2026, 10, 9, 12, 30)),
        ("0 8 * JAN-MAR *", at(2026, 10, 9, 0, 0), (2027, 1, 1, 8, 0)),
        ("0 6 * * sun", at(2026, 10, 9, 0, 0), (2026, 10, 11, 6, 0)),
        ("0 6 * * 7", at(2026, 10, 9, 0, 0), (2026, 10, 11, 6, 0)),  # 7 is also Sunday
        ("0 6 * * 0", at(2026, 10, 9, 0, 0), (2026, 10, 11, 6, 0)),
        # day-of-month AND day-of-week both given: either may match (classic cron rule)
        ("0 0 13 * fri", at(2026, 10, 9, 12, 0), (2026, 10, 13, 0, 0)),
        ("@hourly", at(2026, 10, 9, 12, 30), (2026, 10, 9, 13, 0)),
        ("@daily", at(2026, 10, 9, 12, 30), (2026, 10, 10, 0, 0)),
        ("@weekly", at(2026, 10, 9, 12, 30), (2026, 10, 11, 0, 0)),
        ("@monthly", at(2026, 10, 9, 12, 30), (2026, 11, 1, 0, 0)),
        ("@yearly", at(2026, 10, 9, 12, 30), (2027, 1, 1, 0, 0)),
    ],
)
def test_next_run(expr, now, expected):
    assert when(parse_cron(expr).next_after(now)) == expected


def test_seconds_are_ignored_and_the_next_run_is_always_later():
    cron = parse_cron("* * * * *")
    now = at(2026, 10, 9, 12, 0, 30)
    assert when(cron.next_after(now)) == (2026, 10, 9, 12, 1)
    assert cron.next_after(now) > now


def test_successive_runs_walk_forward():
    cron = parse_cron("0 */6 * * *")
    t, seen = at(2026, 10, 9, 0, 0), []
    for _ in range(5):
        t = cron.next_after(t)
        seen.append(when(t)[2:])
    assert seen == [(9, 6, 0), (9, 12, 0), (9, 18, 0), (10, 0, 0), (10, 6, 0)]  # day, hour, minute


@pytest.mark.parametrize(
    ("expr", "message"),
    [
        ("", "needs 5 fields"),
        ("* * * *", "needs 5 fields"),
        ("* * * * * *", "needs 5 fields"),
        ("60 * * * *", "minute 60 is outside 0-59"),
        ("* 24 * * *", "hour 24 is outside 0-23"),
        ("* * 0 * *", "day of month 0 is outside 1-31"),
        ("* * * 13 *", "month 13 is outside 1-12"),
        ("* * * * 8", "day of week 8 is outside 0-7"),
        ("*/0 * * * *", "step must be 1 or more"),
        ("5-2 * * * *", "range 5-2 goes backwards"),
        ("* * * * foo", "'foo' isn't a day of the week"),
        ("* * * foo *", "'foo' isn't a month"),
        ("a * * * *", "'a' isn't a number"),
        ("@sometimes", "unknown shortcut '@sometimes'"),
        ("0 0 31 2 *", "never happens"),
        ("0 0 30 feb *", "never happens"),
    ],
)
def test_problems_are_explained(expr, message):
    with pytest.raises(CronError) as err:
        parse_cron(expr)
    assert message in str(err.value)


def test_schedule_accepts_a_cron_field():
    schedules, problems = parse_schedules([{"name": "weekdays", "cron": "0 9 * * mon-fri"}])
    assert problems == []
    (s,) = schedules
    assert s.cron == "0 9 * * mon-fri"
    assert when(s.next_after(at(2026, 10, 9, 10, 0)))[2:] == (12, 9, 0)


def test_schedule_with_a_bad_cron_says_what_is_wrong():
    _, problems = parse_schedules([{"name": "x", "cron": "61 * * * *"}])
    assert problems == ["schedule 1: cron: minute 61 is outside 0-59"]


def test_a_schedule_needs_exactly_one_kind_of_timing():
    _, problems = parse_schedules(
        [
            {"name": "none"},
            {"name": "two", "cron": "* * * * *", "daily": "09:00"},
            {"name": "three", "cron": "* * * * *", "interval_seconds": 60, "daily": "09:00"},
        ]
    )
    assert len(problems) == 3
    assert all("exactly one" in p for p in problems)


@pytest.mark.parametrize(
    ("expr", "now", "expected"),
    [
        # A step on a day field narrows the days; it used to be read as plain "*".
        ("0 9 */2 * *", at(2026, 11, 1, 10, 0), (2026, 11, 3, 9, 0)),
        ("0 9 */2 * *", at(2026, 11, 3, 10, 0), (2026, 11, 5, 9, 0)),
        ("0 9 */2 * *", at(2026, 11, 29, 10, 0), (2026, 12, 1, 9, 0)),  # restarts each month
        ("0 9 * * */2", at(2026, 11, 1, 10, 0), (2026, 11, 3, 9, 0)),  # Sun -> Tue
        ("0 9 * * */2", at(2026, 11, 7, 10, 0), (2026, 11, 8, 9, 0)),  # Sat -> Sun
        # With "*" in front, both fields must match: the 1st, 11th, 21st or 31st, on a Monday.
        ("0 9 */10 * 1", at(2026, 11, 1, 10, 0), (2026, 12, 21, 9, 0)),
        # "*/1" is still every day.
        ("0 9 */1 * */1", at(2026, 11, 1, 10, 0), (2026, 11, 2, 9, 0)),
    ],
)
def test_steps_on_day_fields_are_honoured(expr, now, expected):
    assert when(parse_cron(expr).next_after(now)) == expected
