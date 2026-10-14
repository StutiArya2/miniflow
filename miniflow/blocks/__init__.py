"""Blocks: ready-made steps for no-code (JSON) flows (standard library only).

A block is a plain function plus a description of its settings::

    from miniflow.blocks import Setting, block

    @block("weather", settings=[Setting("city", str), Setting("units", str, default="metric")])
    def weather(city, units):
        \"\"\"Current weather for a city.\"\"\"
        ...
        return {"temp": 21.5}

A JSON flow then uses it as ``{"block": "weather", "settings": {"city": "Vellore"}}``.
The function receives its settings as keyword arguments, after placeholders are
filled in, and returns a JSON-like value (dicts, lists, text, numbers) that later
steps can refer to.

The built-in blocks live in :mod:`miniflow.blocks.builtin`. :func:`load_plugins`
imports the ``*.py`` files of a folder (names starting with ``_`` are skipped),
which registers the blocks they define. Blocks run in worker processes too, so a
block function must be defined at module level.
"""

from __future__ import annotations

import difflib
import importlib
import inspect
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from miniflow.task import KINDS, Kind

__all__ = [
    "REQUIRED",
    "BlockError",
    "BlockSpec",
    "Setting",
    "all_blocks",
    "block",
    "get_block",
    "load_plugins",
    "type_name",
]


class _Required:
    def __repr__(self) -> str:
        return "REQUIRED"


REQUIRED: Any = _Required()
"""Default of a setting that must be given."""

_TYPE_NAMES = {
    str: "text",
    int: "a whole number",
    float: "a number",
    bool: "true or false",
    list: "a list",
    dict: "an object",
    object: "anything",
}


def type_name(t: type | tuple[type, ...]) -> str:
    """How a setting's type is described to people: ``text``, ``a list``, ..."""
    types = t if isinstance(t, tuple) else (t,)
    return " or ".join(_TYPE_NAMES.get(x, x.__name__) for x in types)


def _value_name(value: Any) -> str:
    if isinstance(value, bool):
        return "a true/false value"
    if isinstance(value, int | float):
        return "a number"
    if isinstance(value, str):
        return "a piece of text"
    if isinstance(value, list | tuple):
        return "a list"
    if isinstance(value, dict):
        return "an object"
    if value is None:
        return "an empty value"
    return f"a {type(value).__name__}"


def _matches(value: Any, t: type) -> bool:
    if t is object:
        return True
    if isinstance(value, bool):
        return t is bool
    if t is float:
        return isinstance(value, int | float)
    if t is list:
        return isinstance(value, list | tuple)
    return isinstance(value, t)


class BlockError(ValueError):
    """A block that is unknown, badly declared, or fails to load."""


@dataclass(frozen=True)
class Setting:
    """One setting of a block: its name, type, default, help text and limits.

    ``type`` is ``str``, ``int``, ``float``, ``bool``, ``list``, ``dict``,
    ``object`` (anything) or a tuple of these. ``secret`` settings (passwords,
    tokens) must come from ``{{ env.NAME }}`` and are masked in errors. A relative
    path in a ``file`` setting is taken relative to the flow file's folder.
    """

    name: str
    type: type | tuple[type, ...] = str
    default: Any = REQUIRED
    help: str = ""
    choices: tuple[Any, ...] = ()
    secret: bool = False
    file: bool = False

    @property
    def required(self) -> bool:
        return self.default is REQUIRED

    def check(self, value: Any) -> str | None:
        """``None`` if ``value`` is acceptable, otherwise what is wrong with it."""
        if value is None and self.default is None:
            return None
        types = self.type if isinstance(self.type, tuple) else (self.type,)
        if not any(_matches(value, t) for t in types):
            return f"should be {type_name(self.type)}, not {_value_name(value)}"
        if self.choices and value not in self.choices:
            options = ", ".join(map(str, self.choices))
            return f"should be one of {options}, not {value!r}"
        return None


@dataclass(frozen=True)
class BlockSpec:
    """A registered block."""

    name: str
    fn: Callable[..., Any]
    settings: tuple[Setting, ...]
    kind: Kind
    description: str
    ref: str  # "module:qualname", how worker processes import it

    def setting(self, name: str) -> Setting | None:
        return next((s for s in self.settings if s.name == name), None)


_REGISTRY: dict[str, BlockSpec] = {}
_BUILTINS_LOADED = False


def block(
    name: str, *, settings: Iterable[Setting] = (), kind: Kind = "io"
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register the decorated function as block ``name``; the function is returned unchanged."""
    settings = tuple(settings)

    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        if kind not in KINDS:
            raise BlockError(f"block {name!r}: kind must be one of {KINDS}, not {kind!r}")
        if inspect.iscoroutinefunction(fn) != (kind == "async"):
            raise BlockError(f"block {name!r}: use kind='async' exactly for 'async def' blocks")
        if name in _REGISTRY:
            raise BlockError(f"a block called {name!r} is already registered")
        params = inspect.signature(fn).parameters
        takes_any = any(p.kind is p.VAR_KEYWORD for p in params.values())
        for s in settings:
            if s.name not in params and not takes_any:
                raise BlockError(
                    f"block {name!r}: setting {s.name!r} isn't a parameter of {fn.__name__}()"
                )
        doc = inspect.getdoc(fn) or ""
        _REGISTRY[name] = BlockSpec(
            name=name,
            fn=fn,
            settings=settings,
            kind=kind,
            description=doc.split("\n\n")[0].replace("\n", " ").strip(),
            ref=f"{fn.__module__}:{fn.__qualname__}",
        )
        return fn

    return register


def _load_builtins() -> None:
    global _BUILTINS_LOADED
    if not _BUILTINS_LOADED:
        _BUILTINS_LOADED = True
        importlib.import_module("miniflow.blocks.builtin")


def all_blocks() -> list[BlockSpec]:
    """Every registered block, sorted by name."""
    _load_builtins()
    return sorted(_REGISTRY.values(), key=lambda b: b.name)


def get_block(name: str) -> BlockSpec:
    """The block called ``name``, or a :class:`BlockError` that suggests a close name."""
    _load_builtins()
    if name in _REGISTRY:
        return _REGISTRY[name]
    close = difflib.get_close_matches(name, _REGISTRY, n=1)
    hint = f" Did you mean {close[0]!r}?" if close else ""
    raise BlockError(f"there is no block called {name!r}.{hint}")


def load_plugins(folder: str | Path) -> list[str]:
    """Import the ``*.py`` files in ``folder``; return the names of the blocks they added."""
    path = Path(folder).resolve()
    if not path.is_dir():
        raise BlockError(f"blocks folder {path} doesn't exist")
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))  # worker processes inherit it, so cpu blocks resolve
    before = set(_REGISTRY)
    for file in sorted(path.glob("*.py")):
        if file.name.startswith("_") or file.stem in sys.modules:
            continue
        registered = set(_REGISTRY)
        try:
            importlib.import_module(file.stem)
        except BaseException as exc:
            for name, spec in list(_REGISTRY.items()):
                if name not in registered and spec.fn.__module__ == file.stem:
                    del _REGISTRY[name]
            sys.modules.pop(file.stem, None)
            if not isinstance(exc, Exception):
                raise
            raise BlockError(
                f"couldn't load blocks from {file}: {type(exc).__name__}: {exc}"
            ) from exc
    return sorted(set(_REGISTRY) - before)
