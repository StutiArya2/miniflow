"""Short human-readable text for a task's result, shown by the dashboard and ``status``.

A task can choose its own text by returning a mapping with a ``"summary"`` key
(``{"summary": "274 earthquakes in the last 24 hours", "quakes": [...]}``); the rest
of the mapping still flows to downstream tasks. Anything else gets a trimmed ``repr``.
"""

from __future__ import annotations

import reprlib
from collections.abc import Mapping
from typing import Any

__all__ = ["summarize"]

MAX_SUMMARY = 4000
MAX_REPR = 300

_repr = reprlib.Repr()
_repr.maxstring = 120
_repr.maxother = 120
_repr.maxlist = _repr.maxtuple = _repr.maxset = _repr.maxdict = 6
_repr.maxlevel = 3


def summarize(value: Any) -> str | None:
    """The preview text for ``value``, or ``None`` when there is nothing worth showing."""
    if value is None:
        return None
    if isinstance(value, Mapping) and "summary" in value:
        text = str(value["summary"]).strip()
        return text[:MAX_SUMMARY] or None
    if isinstance(value, str):
        text = value.strip()
        return text if len(text) <= MAX_REPR else text[: MAX_REPR - 1] + "…"
    try:
        text = _repr.repr(value)
    except Exception:  # a broken __repr__ must not fail the run
        return f"<{type(value).__name__}>"
    return text if len(text) <= MAX_REPR else text[: MAX_REPR - 1] + "…"
