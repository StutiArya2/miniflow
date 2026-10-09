"""Transform rows (lists of dicts): filter, select, aggregate, sort.

They run as ``cpu`` tasks by default, so big tables are processed in a worker process.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping
from typing import Any

from miniflow.blocks import Setting, block
from miniflow.blocks.expr import ConditionError, compile_condition

Row = Mapping[str, Any]


def _field(row: Row, name: str, where: str) -> Any:
    if name in row:
        return row[name]
    close = difflib.get_close_matches(name, [str(k) for k in row], n=1)
    hint = f" Did you mean {close[0]!r}?" if close else ""
    raise ValueError(f"{where}: no field {name!r} (fields: {', '.join(map(str, row))}).{hint}")


@block(
    "filter",
    settings=[
        Setting("rows", list, help="Rows to filter, e.g. {{ read_sales }}"),
        Setting("where", str, help="Condition, e.g. price > 100 and region == 'North'"),
    ],
    kind="cpu",
)
def filter_rows(rows: list[Row], where: str) -> list[Row]:
    """Keep the rows that match a condition."""
    condition = compile_condition(where)
    kept = []
    for i, row in enumerate(rows, start=1):
        try:
            if condition.evaluate(row):
                kept.append(row)
        except ConditionError as exc:
            raise ValueError(f"row {i}: {exc}") from None
    return kept


@block(
    "select",
    settings=[
        Setting("rows", list),
        Setting("fields", (list, dict), help="Fields to keep, or {new_name: old_name}"),
    ],
    kind="cpu",
)
def select(rows: list[Row], fields: list[str] | dict[str, str]) -> list[dict[str, Any]]:
    """Keep only some fields of each row, optionally renaming them."""
    pairs = list(fields.items()) if isinstance(fields, dict) else [(f, f) for f in fields]
    return [
        {new: _field(row, old, f"row {i}") for new, old in pairs}
        for i, row in enumerate(rows, start=1)
    ]


_METRIC = re.compile(r"\s*(count|sum|avg|min|max)\s*(?:\(\s*([^)]*?)\s*\))?\s*")


def _metric(spec: str) -> tuple[str, str | None]:
    m = _METRIC.fullmatch(spec)
    if m is None or (m.group(1) != "count" and not m.group(2)):
        raise ValueError(
            f"{spec!r} isn't a metric; use count, sum(field), avg(field), min(field) or max(field)"
        )
    return m.group(1), m.group(2) or None


@block(
    "aggregate",
    settings=[
        Setting("rows", list),
        Setting("metrics", dict, help='e.g. {"total": "sum(price)", "items": "count"}'),
        Setting("group_by", (list, str), default=None, help="Field(s) to group by"),
        Setting("decimals", int, default=2, help="Round results to this many decimals"),
    ],
    kind="cpu",
)
def aggregate(
    rows: list[Row],
    metrics: dict[str, str],
    group_by: list[str] | str | None = None,
    decimals: int | None = 2,
) -> list[dict[str, Any]]:
    """Count, sum, average, min or max, overall or per group."""
    keys = [group_by] if isinstance(group_by, str) else list(group_by or [])
    parsed = {name: _metric(spec) for name, spec in metrics.items()}
    groups: dict[tuple[Any, ...], list[Row]] = {}
    for i, row in enumerate(rows, start=1):
        groups.setdefault(tuple(_field(row, k, f"row {i}") for k in keys), []).append(row)
    out = []
    for key, members in groups.items():
        result: dict[str, Any] = dict(zip(keys, key, strict=True))
        for name, (fn, field) in parsed.items():
            if fn == "count":
                result[name] = len(members)
                continue
            assert field is not None
            values = [_field(r, field, name) for r in members]
            values = [v for v in values if v is not None]
            if fn == "sum":
                result[name] = sum(values)
            elif fn == "avg":
                result[name] = sum(values) / len(values) if values else None
            else:
                result[name] = (min if fn == "min" else max)(values, default=None)
            if decimals is not None and isinstance(result[name], float):
                result[name] = round(result[name], decimals)
        out.append(result)
    return out


@block(
    "sort",
    settings=[
        Setting("rows", list),
        Setting("by", (str, list), help="Field(s) to sort by"),
        Setting("descending", bool, default=False),
    ],
    kind="cpu",
)
def sort(rows: list[Row], by: str | list[str], descending: bool = False) -> list[Row]:
    """Sort rows by one or more fields; empty values go last."""
    fields = [by] if isinstance(by, str) else by
    for f in fields:
        for i, row in enumerate(rows, start=1):
            _field(row, f, f"row {i}")
    result = list(rows)
    for f in reversed(fields):  # stable sorts, last key first
        present = [r for r in result if r[f] is not None]
        missing = [r for r in result if r[f] is None]
        result = sorted(present, key=lambda r: r[f], reverse=descending) + missing
    return result
