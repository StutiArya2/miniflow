"""Dates and text: date_format, date_add, date_diff and text_template."""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from miniflow.blocks import Setting, block

_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _parse(value: Any, input_format: str = "") -> tuple[datetime, bool]:
    """``(moment, date_only)``: ISO text, text in ``input_format``, or epoch seconds (UTC)."""
    if isinstance(value, bool):
        raise ValueError(f"{value!r} isn't a date")
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, UTC), False
    if not isinstance(value, str):
        raise ValueError(f"{value!r} isn't a date")
    text = value.strip()
    try:
        if input_format:
            return datetime.strptime(text, input_format), False
        if _DATE_ONLY.fullmatch(text):
            return datetime.fromisoformat(text), True
        return datetime.fromisoformat(text.replace("Z", "+00:00")), False
    except ValueError:
        how = (
            f" in the form {input_format}"
            if input_format
            else " (use 2026-10-09 or 2026-10-09T14:30:00)"
        )
        raise ValueError(f"{value!r} isn't a date{how}") from None


def _on_rows(
    value: Any, rows: list[dict[str, Any]] | None, field: str, into: str, fn: Callable[[Any], Any]
) -> Any:
    """Apply ``fn`` to ``value``, or to ``field`` of every row (stored in ``into``)."""
    if rows is None:
        if value is None:
            raise ValueError("give either value, or rows and field")
        return fn(value)
    if not field:
        raise ValueError("give either value, or rows and field")
    out = []
    for i, row in enumerate(rows, start=1):
        if field not in row:
            raise ValueError(f"row {i}: no field {field!r} (fields: {', '.join(map(str, row))})")
        out.append({**row, (into or field): fn(row[field])})
    return out


_ROWS = [
    Setting("rows", list, default=None, help="Rows to change instead of a single value"),
    Setting("field", str, default="", help="With rows: the field holding the date"),
    Setting(
        "into",
        str,
        default="",
        help="With rows: field to store the result in (default: the same field)",
    ),
]


@block(
    "date_format",
    settings=[
        Setting(
            "value",
            object,
            default=None,
            help="A date: 2026-10-09, 2026-10-09T14:30:00 or epoch seconds",
        ),
        Setting(
            "format", str, default="%Y-%m-%d", help="Output form, e.g. %d/%m/%Y or %b %d, %H:%M"
        ),
        Setting("input_format", str, default="", help="How the input is written, if not ISO"),
        *_ROWS,
    ],
)
def date_format(
    value: Any = None,
    format: str = "%Y-%m-%d",
    input_format: str = "",
    rows: list[dict[str, Any]] | None = None,
    field: str = "",
    into: str = "",
) -> Any:
    """Write a date in a different form (a single value, or a field of every row)."""
    return _on_rows(value, rows, field, into, lambda v: _parse(v, input_format)[0].strftime(format))


@block(
    "date_add",
    settings=[
        Setting("value", object, default=None, help="A date: 2026-10-09 or 2026-10-09T14:30:00"),
        Setting("weeks", float, default=0),
        Setting("days", float, default=0),
        Setting("hours", float, default=0),
        Setting("minutes", float, default=0),
        Setting("seconds", float, default=0),
        *_ROWS,
    ],
)
def date_add(
    value: Any = None,
    weeks: float = 0,
    days: float = 0,
    hours: float = 0,
    minutes: float = 0,
    seconds: float = 0,
    rows: list[dict[str, Any]] | None = None,
    field: str = "",
    into: str = "",
) -> Any:
    """Move a date forward (or back, with negative numbers); a date stays a date."""
    delta = timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    whole_days = not (hours or minutes or seconds)

    def shift(v: Any) -> str:
        moment, date_only = _parse(v)
        moved = moment + delta
        return moved.date().isoformat() if date_only and whole_days else moved.isoformat()

    return _on_rows(value, rows, field, into, shift)


@block(
    "date_diff",
    settings=[
        Setting("start", object, help="The earlier date"),
        Setting("end", object, help="The later date"),
        Setting("unit", str, default="days", choices=("days", "hours", "minutes", "seconds")),
    ],
)
def date_diff(start: Any, end: Any, unit: str = "days") -> float:
    """Time from start to end in days, hours, minutes or seconds (negative if end is earlier)."""
    a, b = _parse(start)[0], _parse(end)[0]
    try:
        seconds = (b - a).total_seconds()
    except TypeError:
        raise ValueError("one date has a time zone and the other doesn't") from None
    result = seconds / {"days": 86400, "hours": 3600, "minutes": 60, "seconds": 1}[unit]
    return int(result) if result == int(result) else round(result, 6)


@block(
    "text_template",
    settings=[
        Setting("rows", list, help="Rows to list, e.g. {{ ranked }}"),
        Setting(
            "row", str, help="One line per row, with {field} for each value, e.g. - {city}: {aqi}"
        ),
        Setting("header", str, default="", help="Text before the rows ({count} = number of rows)"),
        Setting("footer", str, default="", help="Text after the rows ({count} = number of rows)"),
        Setting("separator", str, default="\n", help="Between rows"),
    ],
)
def text_template(
    rows: list[Mapping[str, Any]],
    row: str,
    header: str = "",
    footer: str = "",
    separator: str = "\n",
) -> dict[str, Any]:
    """Turn rows into text, such as an email body or a chat message."""
    lines = []
    for i, r in enumerate(rows, start=1):
        try:
            lines.append(row.format(**r))
        except KeyError as exc:
            name = str(exc.args[0])
            close = difflib.get_close_matches(name, [str(k) for k in r], n=1)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            raise ValueError(
                f"row {i}: no field {name!r} (fields: {', '.join(map(str, r))}).{hint}"
            ) from None
        except (IndexError, ValueError) as exc:
            raise ValueError(f"row {i}: the row text {row!r} isn't valid: {exc}") from None
    count = str(len(rows))
    pieces = [
        header.replace("{count}", count),
        separator.join(lines),
        footer.replace("{count}", count),
    ]
    return {"text": "\n".join(p for p in pieces if p), "count": len(rows)}
