"""Blocks proposed by an agent, waiting for a person to approve them (standard library only).

An agent may only *draft* a block: :func:`write_draft` stores ``blocks/_draft_<name>.py`` in
the flows folder. :func:`miniflow.blocks.load_plugins` skips files whose name starts with
``_``, so a draft is never imported. :func:`check_source` reads the source with :mod:`ast`
only; it never imports or runs it. A person approves a draft (the Editor's Pending blocks
panel, or ``miniflow approve-block``), which renames it to ``blocks/<name>.py`` and loads it.

    check_source(folder, name, source) -> {"ok", "checks": [{"check", "ok", "detail"}], "imports"}
    write_draft(folder, name, source, *, replace=False) -> draft   only if every check passes
    list_drafts(folder) -> [draft]
        a draft is {"name", "source", "sha256", "drafted_at", "ok", "checks", "imports"}
    approve(folder, name, sha256) -> [block names added]
    discard(folder, name, sha256) -> None

``folder`` is the flows folder. ``sha256`` is the hex digest of the source the person was
shown; :func:`approve` and :func:`discard` refuse a draft that has changed since. A block
that fails to load when approved is renamed back to a draft. Every failure is a
:class:`DraftError`; its ``report`` holds the check results when they are the reason.

The checks make importing an approved file run nothing but its imports and the ``@block``
registration: top-level code is limited to imports (before the first function), functions,
literal constants and a docstring; the only decorator is ``@block(...)`` with literal
arguments and ``Setting(...)`` calls; defaults and annotations call nothing.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

from miniflow.blocks import BlockError, all_blocks, load_plugins

__all__ = ["DraftError", "approve", "check_source", "discard", "list_drafts", "write_draft"]

_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_PREFIX = "_draft_"
_MAX_SOURCE = 100_000
_KINDS = ("io", "cpu", "async")
_ALLOWED_NAMES = {"Setting", "block", "str", "int", "float", "bool", "list", "dict", "object"}
_LOCK = threading.Lock()


class DraftError(ValueError):
    def __init__(self, message: str, report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = report


def _blocks_dir(folder: Path) -> Path:
    blocks = Path(folder).resolve() / "blocks"
    if blocks.is_symlink():
        raise DraftError("the blocks folder can't be a symlink")
    return blocks


def _add(checks: list[dict[str, Any]], check: str, ok: bool, problem: str) -> None:
    checks.append({"check": check, "ok": ok, "detail": "" if ok else problem})


def _calls_nothing(node: ast.AST | None) -> bool:
    return node is None or not any(isinstance(n, ast.Call) for n in ast.walk(node))


def _literal(node: ast.AST) -> bool:
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False
    return True


def _block_call(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Name) and node.func.id == "block"


def _plain_block_args(call: ast.Call) -> bool:
    """``@block(...)`` arguments: literals, setting types and ``Setting(...)`` calls only."""
    for n in ast.walk(call):
        if isinstance(n, ast.Call):
            if n is not call and not (isinstance(n.func, ast.Name) and n.func.id == "Setting"):
                return False
        elif isinstance(n, ast.Name):
            if n.id not in _ALLOWED_NAMES:
                return False
        elif not isinstance(
            n,
            ast.Constant | ast.List | ast.Tuple | ast.Dict | ast.keyword | ast.expr_context
            | ast.UnaryOp | ast.USub,
        ):  # fmt: skip
            return False
    return True


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _setting_names(call: ast.Call) -> list[str]:
    value = _keyword(call, "settings")
    names: list[str] = []
    for item in value.elts if isinstance(value, ast.List | ast.Tuple) else []:
        if isinstance(item, ast.Call):
            first = item.args[0] if item.args else _keyword(item, "name")
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.append(first.value)
    return names


def _top_level(
    body: list[ast.stmt],
) -> tuple[list[str], list[str], list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, ast.Call]]]:
    problems: list[str] = []
    imports: list[str] = []
    blocks: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, ast.Call]] = []
    seen_function = False
    for node in body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            if seen_function:
                problems.append(f"line {node.lineno}: put imports before the first function")
            if isinstance(node, ast.Import):
                imports += [alias.name for alias in node.names]
            else:
                imports.append("." * node.level + (node.module or ""))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            seen_function = True
            for decorator in node.decorator_list:
                if not (isinstance(decorator, ast.Call) and _block_call(decorator)):
                    problems.append(
                        f"line {decorator.lineno}: only @block(...) may decorate a function"
                    )
                elif _plain_block_args(decorator):
                    blocks.append((node, decorator))
                else:
                    problems.append(
                        f"line {decorator.lineno}: @block(...) takes plain values "
                        "and Setting(...) only"
                    )
            a = node.args
            parts: list[ast.AST | None] = [*a.defaults, *a.kw_defaults, node.returns]
            parts += [
                x.annotation
                for x in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]
                if x is not None
            ]
            if not all(_calls_nothing(p) for p in parts):
                problems.append(
                    f"line {node.lineno}: {node.name}() calls something in a default "
                    "value or annotation"
                )
        elif (
            isinstance(node, ast.Assign)
            and all(isinstance(t, ast.Name) for t in node.targets)
            and _literal(node.value)
        ) or (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and _calls_nothing(node.annotation)
            and (node.value is None or _literal(node.value))
        ):
            pass
        else:
            problems.append(
                f"line {node.lineno}: only imports, functions and plain constants "
                "can be at the top level"
            )
    return problems, imports, blocks


def _shadows_a_module(name: str) -> bool:
    if name in sys.stdlib_module_names or name in sys.modules:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return True


def check_source(folder: Path, name: str, source: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    _add(
        checks,
        "name",
        bool(_NAME.fullmatch(name)),
        "use lowercase letters, digits and _, starting with a letter, at most 40 characters",
    )
    _add(
        checks,
        "size",
        len(source) <= _MAX_SOURCE,
        f"a block's source must be at most {_MAX_SOURCE} characters",
    )
    if not all(c["ok"] for c in checks):
        return {"ok": False, "checks": checks, "imports": []}
    try:
        tree = ast.parse(source, filename=f"{_PREFIX}{name}.py")
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        where = f"line {exc.lineno}: " if isinstance(exc, SyntaxError) and exc.lineno else ""
        _add(checks, "parses", False, f"{where}{getattr(exc, 'msg', None) or exc}")
        return {"ok": False, "checks": checks, "imports": []}
    _add(checks, "parses", True, "")
    body = list(tree.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]  # the module docstring
    try:
        problems, imports, blocks = _top_level(body)
    except RecursionError:
        problems, imports, blocks = ["the source is nested too deeply"], [], []
    _add(checks, "top-level code", not problems, "; ".join(problems))
    _add(
        checks,
        "one block",
        len(blocks) == 1,
        "no function has @block(...)" if not blocks else "draft one block per file",
    )
    if len(blocks) == 1:
        fn, call = blocks[0]
        first = call.args[0] if call.args else None
        declared = first.value if isinstance(first, ast.Constant) else None
        _add(
            checks,
            "block name",
            declared == name,
            f"@block({declared!r}) should be @block({name!r})",
        )
        params = {a.arg for a in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]}
        missing = [s for s in _setting_names(call) if s not in params and fn.args.kwarg is None]
        _add(
            checks, "settings", not missing, f"not parameters of {fn.name}(): {', '.join(missing)}"
        )
        kind_node = _keyword(call, "kind")
        kind = (
            kind_node.value
            if isinstance(kind_node, ast.Constant)
            else "io"
            if kind_node is None
            else None
        )
        is_async = isinstance(fn, ast.AsyncFunctionDef)
        _add(
            checks,
            "kind",
            kind in _KINDS and (kind == "async") == is_async,
            "kind must be io, cpu or async, and async exactly for 'async def'",
        )
    blocks_dir = _blocks_dir(folder)
    try:
        if blocks_dir.is_dir():
            load_plugins(blocks_dir)
        taken = {b.name for b in all_blocks()}
        _add(checks, "name is free", name not in taken, f"there is already a block called {name!r}")
    except BlockError as exc:
        _add(checks, "name is free", False, f"couldn't load the existing blocks: {exc}")
    _add(
        checks,
        "doesn't shadow a module",
        not _shadows_a_module(name),
        f"a file called {name}.py would hide the module {name!r}; choose another name",
    )
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "imports": sorted(set(imports))}


def _draft_path(blocks: Path, name: str) -> Path:
    path = blocks / f"{_PREFIX}{name}.py"
    if not _NAME.fullmatch(name) or not path.is_file() or path.is_symlink():
        raise DraftError(f"there is no draft called {name!r}")
    return path


def _entry(folder: Path, path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    name = path.stem.removeprefix(_PREFIX)
    try:
        source = data.decode("utf-8")
        report = check_source(folder, name, source)
    except UnicodeDecodeError:
        source = data.decode("utf-8", errors="replace")
        report = {
            "ok": False,
            "checks": [{"check": "parses", "ok": False, "detail": "not UTF-8 text"}],
            "imports": [],
        }
    return {
        "name": name,
        "source": source,
        "sha256": hashlib.sha256(data).hexdigest(),
        "drafted_at": path.stat().st_mtime,
        **report,
    }


def write_draft(folder: Path, name: str, source: str, *, replace: bool = False) -> dict[str, Any]:
    report = check_source(folder, name, source)
    if not report["ok"]:
        raise DraftError("the draft has problems, so nothing was written", report)
    blocks = _blocks_dir(folder)
    with _LOCK:
        blocks.mkdir(exist_ok=True)
        path = blocks / f"{_PREFIX}{name}.py"
        if path.is_symlink():
            raise DraftError("a draft can't be a symlink")
        if path.exists() and not replace:
            raise DraftError(f"there is already a draft called {name!r}; pass replace to change it")
        temporary = blocks / f"{_PREFIX}{name}.tmp"
        temporary.write_bytes(source.encode("utf-8"))
        os.replace(temporary, path)
        return _entry(folder, path)


def list_drafts(folder: Path) -> list[dict[str, Any]]:
    blocks = _blocks_dir(folder)
    if not blocks.is_dir():
        return []
    paths = [
        p for p in sorted(blocks.glob(f"{_PREFIX}*.py"))
        if p.is_file() and not p.is_symlink() and _NAME.fullmatch(p.stem.removeprefix(_PREFIX))
    ]  # fmt: skip
    return [_entry(folder, p) for p in paths]


def _same(path: Path, sha256: str) -> bytes:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise DraftError("the draft changed since you looked at it; review it again")
    return data


def approve(folder: Path, name: str, sha256: str) -> list[str]:
    blocks = _blocks_dir(folder)
    with _LOCK:
        draft = _draft_path(blocks, name)
        data = _same(draft, sha256)
        try:
            report = check_source(folder, name, data.decode("utf-8"))
        except UnicodeDecodeError:
            raise DraftError("the draft isn't UTF-8 text") from None
        if not report["ok"]:
            raise DraftError("the draft doesn't pass its checks", report)
        target = blocks / f"{name}.py"
        if target.exists():
            raise DraftError(f"blocks/{name}.py already exists")
        draft.rename(target)
        try:
            return load_plugins(blocks)
        except BlockError as exc:
            target.rename(draft)
            raise DraftError(f"the block failed to load, so it is a draft again: {exc}") from None


def discard(folder: Path, name: str, sha256: str) -> None:
    blocks = _blocks_dir(folder)
    with _LOCK:
        draft = _draft_path(blocks, name)
        _same(draft, sha256)
        draft.unlink()
