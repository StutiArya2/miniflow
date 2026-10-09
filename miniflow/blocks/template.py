"""``{{ placeholders }}`` in block settings (standard library only).

A placeholder names a value and an optional path into it::

    {{ get_prices }}                    the whole result of step get_prices
    {{ get_prices.json.items.0.name }}  dictionary keys and list positions
    {{ rows.count }}                    on a list: count, first and last
    {{ params.min_price }}              {{ env.SMTP_PASSWORD }}

:func:`render` fills placeholders in from a context (``{root: value}``). A string
that is exactly one placeholder becomes the value itself (a list stays a list);
placeholders inside longer text are inserted as text. Lists and dicts are
rendered item by item. :func:`references` lists what a value refers to, so a flow
can be checked, and its dependencies found, before it runs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

__all__ = ["TemplateError", "lookup", "references", "render"]

_ANY = re.compile(r"\{\{(.*?)\}\}")
_PATH = re.compile(r"\s*([A-Za-z_]\w*)((?:\.[\w-]+)*)\s*")
_LIST_NAMES = {"count", "first", "last"}


class TemplateError(ValueError):
    """A placeholder that is malformed or names something that isn't there."""


def _parse(inner: str) -> tuple[str, str]:
    m = _PATH.fullmatch(inner)
    if m is None:
        raise TemplateError(
            f"{{{{{inner}}}}} is not a valid placeholder; write it like {{{{ step.field }}}}"
        )
    return m.group(1), m.group(2).lstrip(".")


def references(value: Any) -> list[tuple[str, str]]:
    """``(root, path)`` for every placeholder in ``value``, in order of appearance."""
    out: list[tuple[str, str]] = []
    if isinstance(value, str):
        out += [_parse(m.group(1)) for m in _ANY.finditer(value)]
    elif isinstance(value, Mapping):
        for v in value.values():
            out += references(v)
    elif isinstance(value, list | tuple):
        for v in value:
            out += references(v)
    return out


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "true/false value"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "piece of text"
    if value is None:
        return "empty value"
    return type(value).__name__


def lookup(root: str, path: str, context: Mapping[str, Any]) -> Any:
    """The value at ``root.path`` in ``context``, or a :class:`TemplateError` saying why not."""
    if root not in context:
        raise TemplateError(f"{{{{ {root} }}}}: there is nothing called {root!r}")
    value, where = context[root], root
    if root == "env":  # never list the machine's environment variables in a message
        name = path.split(".")[0]
        if name not in value:
            raise TemplateError(f"environment variable {name} isn't set")
    for part in path.split(".") if path else []:
        if isinstance(value, Mapping):
            if part not in value:
                keys = ", ".join(map(str, value)) or "nothing"
                raise TemplateError(f"no {part!r} in {where} (it has: {keys})")
            value = value[part]
        elif isinstance(value, list | tuple):
            if part in _LIST_NAMES:
                if part == "count":
                    value = len(value)
                elif not value:
                    raise TemplateError(f"{where} is an empty list, so it has no {part} item")
                else:
                    value = value[0] if part == "first" else value[-1]
            elif part.isdigit():
                if int(part) >= len(value):
                    raise TemplateError(
                        f"{where} has {len(value)} items, so there is no item {part}"
                        " (counting starts at 0)"
                    )
                value = value[int(part)]
            else:
                raise TemplateError(
                    f"{where} is a list: use a position (0, 1, ...), count, first or last,"
                    f" not {part!r}"
                )
        else:
            raise TemplateError(f"{where} is a {_kind(value)}, so it has no {part!r}")
        where = f"{where}.{part}"
    return value


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict | list):
        return json.dumps(value)
    return str(value)


def render(value: Any, context: Mapping[str, Any]) -> Any:
    """``value`` with every placeholder filled in from ``context``."""
    if isinstance(value, str):
        whole = _ANY.fullmatch(value.strip())
        if whole is not None and value.strip().count("{{") == 1:
            return lookup(*_parse(whole.group(1)), context)
        return _ANY.sub(lambda m: _text(lookup(*_parse(m.group(1)), context)), value)
    if isinstance(value, Mapping):
        return {k: render(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, context) for v in value]
    return value
