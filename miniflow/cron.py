"""Cron expressions for flow schedules (standard library only).

A five-field expression in the laptop's local time::

    minute  hour  day-of-month  month  day-of-week
    0-59    0-23  1-31          1-12   0-7 (0 and 7 are Sunday)

Each field takes ``*``, a number, a range (``9-17``), a list (``1,15``) and steps
(``*/15``, ``10-50/20``). Months and weekdays can be written as names
(``jan-mar``, ``mon-fri``). As in classic cron, when both day-of-month and
day-of-week are restricted a day matches if *either* does; a field that starts with
``*`` (including ``*/2``) is not "restricted" in that sense, so ``0 9 */2 * 1`` means
the odd-numbered days that are also Mondays. The shortcuts ``@hourly``,
``@daily``, ``@weekly``, ``@monthly`` and ``@yearly`` are accepted.

:func:`parse_cron` reports mistakes in plain words (:class:`CronError`);
:meth:`CronExpr.next_after` gives the next time strictly after a timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

__all__ = ["CronError", "CronExpr", "parse_cron"]

_SHORTCUTS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
_DAYS_IN_MONTH = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]  # February counted as 29
_SEARCH_DAYS = 366 * 8  # long enough for the next 29 February


class CronError(ValueError):
    """A cron expression that is malformed or can never match."""


@dataclass(frozen=True)
class CronExpr:
    text: str
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    any_day: bool  # day-of-month starts with * (``*`` or ``*/n``)
    any_weekday: bool  # day-of-week starts with * (``*`` or ``*/n``)

    def _day_matches(self, day: datetime) -> bool:
        if day.month not in self.months:
            return False
        by_date = day.day in self.days
        by_weekday = day.isoweekday() % 7 in self.weekdays
        # A field that starts with ``*`` may still narrow the days (``*/2``), so it is
        # checked, not skipped. As in classic cron, the two fields are joined by "or"
        # only when neither starts with ``*``.
        if self.any_day or self.any_weekday:
            return by_date and by_weekday
        return by_date or by_weekday

    def next_after(self, now: float) -> float:
        """The first matching minute strictly after ``now`` (seconds are ignored)."""
        start = datetime.fromtimestamp(now).replace(second=0, microsecond=0) + timedelta(minutes=1)
        for offset in range(_SEARCH_DAYS):
            day = datetime.combine(start.date() + timedelta(days=offset), time())
            if not self._day_matches(day):
                continue
            floor = (start.hour, start.minute) if offset == 0 else (0, 0)
            for hour in self.hours:
                for minute in self.minutes:
                    if (hour, minute) >= floor:
                        return day.replace(hour=hour, minute=minute).timestamp()
        raise CronError(f"{self.text!r} has no run in the next eight years")  # pragma: no cover


def _number(text: str, noun: str, names: list[str], offset: int) -> int:
    word = text.lower()
    if names and word in names:
        return names.index(word) + offset
    if names and word.isalpha():
        raise CronError(f"{text!r} isn't a {'day of the week' if noun == 'day of week' else noun}")
    try:
        return int(text)
    except ValueError:
        raise CronError(f"{text!r} isn't a number (in the {noun} field)") from None


def _field(text: str, noun: str, low: int, high: int, names: list[str], offset: int) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        base, slash, step_text = part.partition("/")
        step = 1
        if slash:
            step = _number(step_text, noun, [], 0)
            if step < 1:
                raise CronError(f"{noun} step must be 1 or more, not {step}")
        if base == "*":
            first, last = low, high
        elif "-" in base:
            a, _, b = base.partition("-")
            first, last = _number(a, noun, names, offset), _number(b, noun, names, offset)
            if first > last:
                raise CronError(f"{noun} range {base} goes backwards")
        else:
            first = _number(base, noun, names, offset)
            last = high if slash else first
        for n in (first, last):
            if not low <= n <= high:
                raise CronError(f"{noun} {n} is outside {low}-{high}")
        values.update(range(first, last + 1, step))
    return values


def parse_cron(text: str) -> CronExpr:
    """Parse a five-field cron expression (or an ``@hourly``-style shortcut)."""
    raw = text.strip()
    if raw.startswith("@"):
        if raw.lower() not in _SHORTCUTS:
            raise CronError(f"unknown shortcut {raw!r}; use {', '.join(sorted(_SHORTCUTS))}")
        raw = _SHORTCUTS[raw.lower()]
    parts = raw.split()
    if len(parts) != 5:
        raise CronError(
            f"a cron expression needs 5 fields (minute hour day month weekday), got {len(parts)}"
        )
    minute, hour, day, month, weekday = parts
    minutes = _field(minute, "minute", 0, 59, [], 0)
    hours = _field(hour, "hour", 0, 23, [], 0)
    days = _field(day, "day of month", 1, 31, [], 0)
    months = _field(month, "month", 1, 12, _MONTHS, 1)
    weekdays = {d % 7 for d in _field(weekday, "day of week", 0, 7, _DAYS, 0)}
    expr = CronExpr(
        text=text.strip(),
        minutes=tuple(sorted(minutes)),
        hours=tuple(sorted(hours)),
        days=frozenset(days),
        months=frozenset(months),
        weekdays=frozenset(weekdays),
        any_day=day.startswith("*"),
        any_weekday=weekday.startswith("*"),
    )
    if (
        not expr.any_day
        and expr.any_weekday
        and not any(d <= _DAYS_IN_MONTH[m - 1] for m in months for d in days)
    ):
        raise CronError(f"{text.strip()!r} never happens: those months don't have those days")
    return expr
