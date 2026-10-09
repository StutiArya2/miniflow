"""Short human-readable text for a task's result, shown by the dashboard and ``status``.

A task can choose its own text by returning a mapping with a ``"summary"`` key
(``{"summary": "274 earthquakes in the last 24 hours", "quakes": [...]}``); the rest
of the mapping still flows to downstream tasks. Anything else gets a trimmed ``repr``.
"""

from __future__ import annotations

import math
import os
import re
import reprlib
from collections.abc import Mapping
from typing import Any

__all__ = ["inspect_value", "summarize"]

MAX_SUMMARY = 4000
MAX_REPR = 300

_repr = reprlib.Repr()
_repr.maxstring = 120
_repr.maxother = 120
_repr.maxlist = _repr.maxtuple = _repr.maxset = _repr.maxdict = 6
_repr.maxlevel = 3
_SECRET_NAME = re.compile(r"password|secret|token|api.?key|auth|credential", re.I)


def _redact(text: str) -> str:
    for key, secret in os.environ.items():
        if _SECRET_NAME.search(key) and len(secret) >= 4:
            text = text.replace(secret, "••••")
    return text


def _sample(value: Any, depth: int = 0) -> Any:
    """A small JSON-compatible preview; secret-looking fields are never displayed."""
    if depth > 3:
        return "…"
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:12]:
            label = str(key)
            out[label] = "••••" if _SECRET_NAME.search(label) else _sample(item, depth + 1)
        if len(value) > 12:
            out["…"] = f"{len(value) - 12} more fields"
        return out
    if isinstance(value, list | tuple):
        rows = [_sample(item, depth + 1) for item in value[:5]]
        if len(value) > 5:
            rows.append(f"… {len(value) - 5} more items")
        return rows
    if isinstance(value, str):
        return _redact(value)[:240]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    return f"<{type(value).__name__}>"


def inspect_value(value: Any) -> tuple[Any, int | None]:
    """Structured sample and item count for the dashboard."""
    count = len(value) if isinstance(value, list | tuple | Mapping) else None
    return _sample(value), count


def summarize(value: Any) -> str | None:
    """The preview text for ``value``, or ``None`` when there is nothing worth showing."""
    if value is None:
        return None
    if isinstance(value, Mapping) and "summary" in value:
        text = _redact(str(value["summary"]).strip())
        return text[:MAX_SUMMARY] or None
    if isinstance(value, str):
        text = _redact(value.strip())
        return text if len(text) <= MAX_REPR else text[: MAX_REPR - 1] + "…"
    try:
        text = _repr.repr(_sample(value))
    except Exception:  # a broken __repr__ must not fail the run
        return f"<{type(value).__name__}>"
    return _redact(text if len(text) <= MAX_REPR else text[: MAX_REPR - 1] + "…")
