"""Safe row conditions such as ``price > 100 and region == 'North'``.

Used by the ``filter`` block. A condition is parsed with :mod:`ast` and then
evaluated by walking the tree; nothing goes through ``eval``. Only these are
allowed: field names (``price``, or ``row['unit price']`` for names with spaces),
numbers, text, ``True``/``False``/``None``, lists, comparisons (``== != < <= > >=``,
``in``, ``not in``), ``and``/``or``/``not``, ``+ - * / %`` and the functions
``lower upper len abs round str int float``.
"""

from __future__ import annotations

import ast
import difflib
import operator
from collections.abc import Callable, Mapping
from typing import Any

__all__ = ["Condition", "ConditionError", "compile_condition"]


class ConditionError(ValueError):
    """A condition that is unsafe, malformed or doesn't fit the data."""


_COMPARE: dict[type[ast.cmpop], Callable[[Any, Any], Any]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}
_ARITHMETIC: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}
_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "lower": lambda s: str(s).lower(),
    "upper": lambda s: str(s).upper(),
    "len": len,
    "abs": abs,
    "round": round,
    "str": str,
    "int": int,
    "float": float,
}
_CONSTANTS = {"True": True, "False": False, "None": None}


def _check(node: ast.AST) -> None:
    """Refuse anything outside the small allowed language."""
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            name = node.func.id if isinstance(node.func, ast.Name) else ast.unparse(node.func)
            raise ConditionError(
                f"can't call {name}() in a condition; allowed: {', '.join(_FUNCTIONS)}"
            )
        if node.keywords:
            raise ConditionError("functions in a condition take plain arguments only")
        for arg in node.args:
            _check(arg)
        return
    if isinstance(node, ast.Subscript):
        if not (
            isinstance(node.value, ast.Name)
            and node.value.id == "row"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            raise ConditionError("use row['field name'] to refer to a field with spaces")
        return
    allowed = (
        ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
        ast.Compare, ast.BinOp, ast.Constant, ast.Name, ast.List, ast.Tuple, ast.Load,
        *_COMPARE, *_ARITHMETIC,
    )  # fmt: skip
    if not isinstance(node, allowed):
        raise ConditionError(f"can't use {ast.unparse(node)!r} in a condition")
    for child in ast.iter_child_nodes(node):
        _check(child)


class Condition:
    """A checked condition; :meth:`evaluate` it against one row (a dict)."""

    def __init__(self, text: str, tree: ast.Expression) -> None:
        self.text = text
        self._tree = tree

    def evaluate(self, row: Mapping[str, Any]) -> bool:
        return bool(self._eval(self._tree.body, row))

    def _field(self, name: str, row: Mapping[str, Any]) -> Any:
        if name in row:
            return row[name]
        close = difflib.get_close_matches(name, [str(k) for k in row], n=1)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        fields = ", ".join(map(str, row)) or "none"
        raise ConditionError(f"the row has no field {name!r} (fields: {fields}).{hint}")

    def _eval(self, node: ast.AST, row: Mapping[str, Any]) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in _CONSTANTS:
                return _CONSTANTS[node.id]
            return self._field(node.id, row)
        if isinstance(node, ast.Subscript):
            assert isinstance(node.slice, ast.Constant)
            assert isinstance(node.slice.value, str)
            return self._field(node.slice.value, row)
        if isinstance(node, ast.List | ast.Tuple):
            return [self._eval(e, row) for e in node.elts]
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return all(self._eval(v, row) for v in node.values)
            return any(self._eval(v, row) for v in node.values)
        if isinstance(node, ast.UnaryOp):
            value = self._eval(node.operand, row)
            if isinstance(node.op, ast.Not):
                return not value
            return -value if isinstance(node.op, ast.USub) else +value
        try:
            if isinstance(node, ast.Compare):
                left = self._eval(node.left, row)
                for op, right_node in zip(node.ops, node.comparators, strict=True):
                    right = self._eval(right_node, row)
                    if not _COMPARE[type(op)](left, right):
                        return False
                    left = right
                return True
            if isinstance(node, ast.BinOp):
                return _ARITHMETIC[type(node.op)](
                    self._eval(node.left, row), self._eval(node.right, row)
                )
            if isinstance(node, ast.Call):
                assert isinstance(node.func, ast.Name)
                return _FUNCTIONS[node.func.id](*(self._eval(a, row) for a in node.args))
        except TypeError as exc:
            raise ConditionError(
                f"can't compare or combine these values in {self.text!r}: {exc}"
            ) from None
        except (ValueError, ZeroDivisionError) as exc:
            raise ConditionError(f"{self.text!r}: {exc}") from None
        raise ConditionError(f"can't use {ast.unparse(node)!r} in a condition")  # pragma: no cover


def compile_condition(text: str) -> Condition:
    """Parse and check ``text``; raise :class:`ConditionError` if it isn't allowed."""
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError:
        raise ConditionError(f"{text!r} isn't a valid condition") from None
    _check(tree)
    return Condition(text, tree)
