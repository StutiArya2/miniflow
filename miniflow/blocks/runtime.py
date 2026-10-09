"""Running one step of a JSON flow (standard library only).

Every step of a JSON flow is a MiniFlow task whose function is :func:`run_step`
(or :func:`run_step_async` for async blocks), called with the scheduler's usual
convention ``fn(*upstream_results, **config)``. ``config`` holds the block name and
its import reference, the step's raw settings, the names of its upstream steps
(in the same order as ``upstream_results``), the flow's parameter values and the
flow file's folder. Everything in it is plain data, so cpu steps can be sent to
worker processes.

For each run the step: fills placeholders in from the upstream results,
``params`` and ``env``; applies defaults; checks every value against its
:class:`~miniflow.blocks.Setting`; resolves relative ``file`` paths against the flow
folder; calls the block; and replaces secret values with ``••••`` in any error.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from miniflow.blocks import BlockError, BlockSpec, Setting, get_block
from miniflow.blocks.template import TemplateError, render

__all__ = [
    "StepError",
    "bind_subflow",
    "call_block",
    "collect_subflow",
    "prepare",
    "run_step",
    "run_step_async",
]

MASK = "••••"
_PARAM_TYPES: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
}


class StepError(RuntimeError):
    """A step that couldn't be prepared or whose block failed (secrets already masked)."""


def _spec(block: str, ref: str) -> BlockSpec:
    try:
        return get_block(block)
    except BlockError:
        # A plugin block in a fresh worker process: importing its module registers it.
        importlib.import_module(ref.partition(":")[0])
        return get_block(block)


def prepare(
    spec: BlockSpec, settings: Mapping[str, Any], context: Mapping[str, Any], base_dir: str
) -> tuple[dict[str, Any], list[str]]:
    """The block's keyword arguments, and the secret values to hide in messages."""
    values: dict[str, Any] = {}
    for s in spec.settings:
        if s.name in settings:
            try:
                value = render(settings[s.name], context)
            except TemplateError as exc:
                raise StepError(f"setting {s.name!r}: {exc}") from None
        elif s.required:
            raise StepError(f"missing setting {s.name!r}")
        else:
            value = s.default
        problem = s.check(value)
        if problem:
            raise StepError(f"setting {s.name!r}: {problem}")
        if s.file and isinstance(value, str) and value and not Path(value).is_absolute():
            value = str(Path(base_dir) / value)
        values[s.name] = value
    for name, raw in settings.items():  # extra settings of blocks that take **kwargs
        if spec.setting(name) is None:
            values[name] = render(raw, context)
    secrets = [str(values[s.name]) for s in spec.settings if s.secret and values.get(s.name)]
    return values, secrets


def _mask(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, MASK)
    return text


def _context(
    upstream: Sequence[Any], inputs: Sequence[str], params: Mapping[str, Any]
) -> dict[str, Any]:
    values = dict(zip(inputs, upstream, strict=True))
    actual_params = values.pop("__params__", params)
    return {**values, "params": actual_params, "env": os.environ}


def bind_subflow(
    *upstream: Any,
    settings: Mapping[str, Any],
    inputs: Sequence[str],
    params: Mapping[str, Any],
    declaration: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve a sub-flow's parameter settings from its parent's results."""
    context = _context(upstream, inputs, params)
    bound: dict[str, Any] = {}
    for name, definition in declaration.items():
        if name in settings:
            bound[name] = render(settings[name], context)
        elif "default" in definition:
            bound[name] = definition["default"]
        else:
            raise StepError(f"missing required setting {name!r}")
        kind = _PARAM_TYPES.get(definition.get("type", "str"))
        if kind is not None:
            problem = Setting(name, kind).check(bound[name])
            if problem:
                raise StepError(f"setting {name!r}: {problem}")
    return bound


def collect_subflow(
    *upstream: Any,
    outputs: Mapping[str, Any],
    inputs: Sequence[str],
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """Expose only declared outputs, or every step when none were declared."""
    context = _context(upstream, inputs, params)
    return {key: render(value, context) for key, value in outputs.items()}


def call_block(spec: BlockSpec, values: Mapping[str, Any], secrets: Sequence[str]) -> Any:
    """Call a synchronous block, turning its errors into :class:`StepError`."""
    try:
        return spec.fn(**values)
    except Exception as exc:
        raise StepError(_mask(f"{type(exc).__name__}: {exc}", secrets)) from None


def run_step(
    *upstream: Any,
    block: str,
    ref: str,
    settings: Mapping[str, Any],
    inputs: Sequence[str],
    params: Mapping[str, Any],
    base_dir: str,
) -> Any:
    """Task function of a JSON-flow step (io and cpu blocks)."""
    spec = _spec(block, ref)
    values, secrets = prepare(spec, settings, _context(upstream, inputs, params), base_dir)
    return call_block(spec, values, secrets)


async def run_step_async(
    *upstream: Any,
    block: str,
    ref: str,
    settings: Mapping[str, Any],
    inputs: Sequence[str],
    params: Mapping[str, Any],
    base_dir: str,
) -> Any:
    """Task function of a JSON-flow step (async blocks)."""
    spec = _spec(block, ref)
    values, secrets = prepare(spec, settings, _context(upstream, inputs, params), base_dir)
    try:
        return await spec.fn(**values)
    except Exception as exc:
        raise StepError(_mask(f"{type(exc).__name__}: {exc}", secrets)) from None
