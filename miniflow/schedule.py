"""Schedules and triggers declared inside JSON flows.

Time-based: ``interval_seconds``, ``daily`` (``"HH:MM"``) or ``cron`` (see
:mod:`miniflow.cron`), optionally with ``"catch_up": true`` to run once after the dashboard
was down through launches. Event-based: ``watch`` (a file pattern; a run starts when new files
appear and have stopped growing, checked every ``poll_seconds``) or ``webhook`` (the name of
an environment variable holding the secret token that callers must present).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from miniflow.cron import CronError, parse_cron

_CLOCK = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_FIELDS = (
    "name", "interval_seconds", "daily", "cron", "watch", "poll_seconds", "webhook", "catch_up",
)  # fmt: skip
_TIMINGS = ("interval_seconds", "daily", "cron", "watch", "webhook")
_TIMED = ("interval_seconds", "daily", "cron")


@dataclass(frozen=True)
class Schedule:
    """A named local schedule or trigger; exactly one of the timing fields is set."""

    name: str
    interval_seconds: float | None = None
    daily: str | None = None
    cron: str | None = None
    watch: str | None = None
    poll_seconds: float = 5.0
    webhook: str | None = None
    catch_up: bool = False

    @property
    def kind(self) -> str:
        for kind in _TIMINGS:
            if getattr(self, kind) is not None:
                return kind.removesuffix("_seconds")
        raise ValueError("schedule has no timing")  # pragma: no cover

    @property
    def timed(self) -> bool:
        """True if it fires at clock times (so it has a next due time)."""
        return self.kind in ("interval", "daily", "cron")

    def next_after(self, now: float) -> float:
        """Next due timestamp strictly after ``now`` in the laptop's local time zone."""
        if self.interval_seconds is not None:
            return now + self.interval_seconds
        if self.cron is not None:
            return parse_cron(self.cron).next_after(now)
        if self.daily is None:
            raise ValueError(f"a {self.kind} trigger has no next time")
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
            if key not in _FIELDS:
                problems.append(f"{where}: unknown field {key!r}")
        given = [k for k in _TIMINGS if item.get(k) is not None]
        if len(given) != 1:
            problems.append(
                f"{where}: set exactly one of interval_seconds, daily (HH:MM), cron,"
                " watch or webhook"
            )
            continue
        schedule, found = _one(where, name, given[0], item)
        problems += found
        if schedule is not None:
            schedules.append(schedule)
    return tuple(schedules), problems


def _one(
    where: str, name: str, kind: str, item: dict[str, Any]
) -> tuple[Schedule | None, list[str]]:
    """Check one schedule that has exactly one timing field."""
    problems: list[str] = []
    value = item[kind]
    extra: dict[str, Any] = {}
    if kind == "interval_seconds":
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value < 1
        ):
            problems.append(f"{where}: interval_seconds should be at least 1")
        else:
            extra["interval_seconds"] = float(value)
    elif kind == "daily":
        if not isinstance(value, str) or not _CLOCK.fullmatch(value):
            problems.append(f"{where}: daily should be local time as HH:MM")
        else:
            extra["daily"] = value
    elif kind == "cron":
        try:
            if not isinstance(value, str):
                raise CronError("should be text like '0 9 * * mon-fri'")
            parse_cron(value)
        except CronError as exc:
            problems.append(f"{where}: cron: {exc}")
        else:
            extra["cron"] = value
    elif kind == "watch":
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{where}: watch should be a file pattern such as inbox/*.csv")
        else:
            extra["watch"] = value.strip()
        poll = item.get("poll_seconds", 5)
        if (
            isinstance(poll, bool)
            or not isinstance(poll, int | float)
            or not math.isfinite(poll)
            or poll < 1
        ):
            problems.append(f"{where}: poll_seconds should be at least 1")
        else:
            extra["poll_seconds"] = float(poll)
    else:  # webhook
        if not isinstance(value, str) or not _ENV_NAME.fullmatch(value):
            problems.append(
                f"{where}: webhook should be the name of an environment variable holding the"
                " secret token, such as ORDERS_HOOK_TOKEN"
            )
        else:
            extra["webhook"] = value
    if "poll_seconds" in item and kind != "watch":
        problems.append(f"{where}: poll_seconds only applies to watch schedules")
    if "catch_up" in item:
        if not isinstance(item["catch_up"], bool):
            problems.append(f"{where}: catch_up should be true or false")
        elif kind not in _TIMED:
            problems.append(f"{where}: catch_up only applies to interval, daily and cron schedules")
        else:
            extra["catch_up"] = item["catch_up"]
    if problems:
        return None, problems
    return Schedule(name=name, **extra), []
