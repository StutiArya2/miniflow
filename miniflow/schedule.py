"""Local interval and daily schedules declared inside JSON flows."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

_CLOCK = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")


@dataclass(frozen=True)
class Schedule:
    """A named local schedule; exactly one of interval_seconds and daily is set."""

    name: str
    interval_seconds: float | None = None
    daily: str | None = None

    def next_after(self, now: float) -> float:
        """Next due timestamp strictly after ``now`` in the laptop's local time zone."""
        if self.interval_seconds is not None:
            return now + self.interval_seconds
        assert self.daily is not None
        hour, minute = map(int, self.daily.split(":"))
        day = datetime.fromtimestamp(now).date()
        candidate = datetime.combine(day, time(hour, minute)).timestamp()
        if candidate <= now:
            candidate = datetime.combine(day + timedelta(days=1), time(hour, minute)).timestamp()
        return candidate


def parse_schedules(raw: Any) -> tuple[tuple[Schedule, ...], list[str]]:
    """Parse and report every malformed schedule before a flow can be saved or run."""
    if raw is None:
        return (), []
    if not isinstance(raw, list):
        return (), ["'schedules' should be a list"]
    schedules: list[Schedule] = []
    problems: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, 1):
        where = f"schedule {index}"
        if not isinstance(item, dict):
            problems.append(f"{where} should be an object")
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            problems.append(f"{where}: name should be non-empty text")
            continue
        if name in seen:
            problems.append(f"{where}: name {name!r} is repeated")
        seen.add(name)
        for key in item:
            if key not in ("name", "interval_seconds", "daily"):
                problems.append(f"{where}: unknown field {key!r}")
        interval = item.get("interval_seconds")
        daily = item.get("daily")
        if (interval is None) == (daily is None):
            problems.append(f"{where}: set either interval_seconds or daily (HH:MM)")
            continue
        if interval is not None:
            if (
                isinstance(interval, bool)
                or not isinstance(interval, int | float)
                or not math.isfinite(interval)
                or interval < 1
            ):
                problems.append(f"{where}: interval_seconds should be at least 1")
            else:
                schedules.append(Schedule(name=name, interval_seconds=float(interval)))
        elif not isinstance(daily, str) or not _CLOCK.fullmatch(daily):
            problems.append(f"{where}: daily should be local time as HH:MM")
        else:
            schedules.append(Schedule(name=name, daily=daily))
    return tuple(schedules), problems
