"""No-code flows: JSON files of configured blocks (standard library only).

A flow file looks like::

    {
      "description": "Keep expensive sales and save them",
      "params": {"min": {"type": "float", "default": 100, "help": "Lowest price kept"}},
      "steps": {
        "sales": {"block": "read_csv", "settings": {"path": "sales.csv"}},
        "big": {
          "block": "filter",
          "settings": {"rows": "{{ sales }}", "where": "price > {{ params.min }}"}
        },
        "save": {
          "block": "write_csv",
          "settings": {"rows": "{{ big }}", "path": "out/big.csv"},
          "retries": 2
        }
      },
      "on_failure": {
        "block": "log",
        "settings": {"message": "{{ failure.step }} failed", "level": "error"}
      }
    }

:func:`load_json_flow` checks the whole file before anything runs and reports
every problem at once (:class:`FlowFileError`), in plain language with
suggestions for misspelt names. It returns a :class:`Flow` whose :attr:`Flow.dag`
is an ordinary MiniFlow DAG: each step is a task running
:func:`miniflow.blocks.runtime.run_step`, so retries, resume, the dashboard and the
CLI work exactly as for Python flows.

Step fields: ``block`` (required), ``settings``, ``after`` (extra dependencies),
``retries``, ``timeout``, ``kind`` (overrides the block's worker type), ``note`` and
``position`` (used by the editor). A step runs after every step its settings refer
to with ``{{ step }}`` placeholders, and after the steps in ``after``.

Parameters get their values from ``--param name=value``, then the environment file,
then their default. ``load_json_flow(..., environment="prod")`` reads
``environments/prod.json`` (parameter values) and, if present,
``environments/prod.env`` (``NAME=value`` lines added to the environment unless
already set) next to the flow file. Plugin blocks in a ``blocks/`` folder next to
the flow are loaded automatically.
"""

from __future__ import annotations

import asyncio
import difflib
import inspect
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from miniflow.blocks import BlockError, BlockSpec, Setting, get_block, load_plugins, type_name
from miniflow.blocks.runtime import call_block, prepare, run_step, run_step_async
from miniflow.blocks.template import TemplateError, references
from miniflow.graph import DAG, CycleError, GraphError
from miniflow.scheduler import RunResult
from miniflow.states import State
from miniflow.task import KINDS, Task, task

__all__ = ["Flow", "FlowFileError", "load_json_flow"]

_TOP = ("description", "params", "steps", "on_failure")
_STEP_FIELDS = ("block", "settings", "after", "retries", "timeout", "kind", "note", "position")
_PARAM_FIELDS = ("type", "default", "help")
_RESERVED = ("params", "env", "failure")
_FAILURE_FIELDS = ("step", "error", "failed", "skipped", "run_id")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENV_ONLY = re.compile(r"\s*\{\{\s*env\.[A-Za-z_]\w*\s*\}\}\s*")
_PARAM_TYPES: dict[str, type] = {
    "str": str, "int": int, "float": float, "bool": bool, "list": list, "dict": dict,
}  # fmt: skip
_TRUE, _FALSE = {"true", "yes", "1", "on"}, {"false", "no", "0", "off"}


class FlowFileError(ValueError):
    """A flow file with problems; :attr:`problems` lists them one per line."""

    def __init__(self, path: Path, problems: list[str]) -> None:
        self.path = path
        self.problems = problems
        noun = "problem" if len(problems) == 1 else "problems"
        lines = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"{path.name} has {len(problems)} {noun}:\n{lines}")


@dataclass(frozen=True)
class Flow:
    """A checked JSON flow, ready to run."""

    name: str
    path: Path
    dag: DAG
    description: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    on_failure: dict[str, Any] | None = None

    def run_on_failure(self, result: RunResult) -> Any:
        """Run the ``on_failure`` block for a failed run; ``None`` if there is none."""
        if self.on_failure is None:
            return None
        failed = [n for n, s in result.states.items() if s is State.FAILED]
        skipped = [n for n, s in result.states.items() if s is State.SKIPPED]
        first = failed[0] if failed else ""
        error = (result.errors.get(first) or "").strip().splitlines()
        failure = {
            "step": first,
            "error": error[-1] if error else "",
            "failed": failed,
            "skipped": skipped,
            "run_id": result.run_id,
        }
        spec = get_block(self.on_failure["block"])
        context = {"params": self.params, "env": os.environ, "failure": failure}
        values, secrets = prepare(
            spec, self.on_failure.get("settings", {}), context, str(self.path.parent)
        )
        if inspect.iscoroutinefunction(spec.fn):
            return asyncio.run(spec.fn(**values))
        return call_block(spec, values, secrets)


def _suggest(name: str, options: Iterable[str]) -> str:
    close = difflib.get_close_matches(name, list(options), n=1)
    return f" Did you mean {close[0]!r}?" if close else ""


def _read_env_file(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _parse_param(text: str, kind: type) -> Any:
    if kind is str:
        return text
    if kind is bool:
        if text.lower() in _TRUE:
            return True
        if text.lower() in _FALSE:
            return False
        raise ValueError
    if kind is float and re.fullmatch(r"[+-]?\d+", text.strip()):
        return int(text)  # 20000 stays 20000 in messages, not 20000.0
    if kind in (int, float):
        return kind(text)
    value = json.loads(text)
    if not isinstance(value, kind):
        raise ValueError
    return value


class _Checker:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.problems: list[str] = []

    def add(self, problem: str) -> None:
        self.problems.append(problem)

    # ------------------------------------------------------------ params
    def params(
        self, declared: Any, cli: Mapping[str, str], from_env: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(declared, dict):
            self.add("'params' should be an object of parameter definitions")
            return {}
        values: dict[str, Any] = {}
        for name, spec in declared.items():
            where = f"parameter {name!r}"
            if not isinstance(spec, dict):
                self.add(f'{where} should be an object like {{"type": "int", "default": 1}}')
                continue
            for key in spec:
                if key not in _PARAM_FIELDS:
                    self.add(f"{where}: unknown field {key!r}.{_suggest(key, _PARAM_FIELDS)}")
            kind_name = spec.get("type", "str")
            kind = _PARAM_TYPES.get(kind_name)
            if kind is None:
                self.add(
                    f"{where}: type should be one of {', '.join(_PARAM_TYPES)}, not {kind_name!r}"
                )
                continue
            setting = Setting(name, kind, default=spec.get("default", None))
            if name in cli:
                try:
                    values[name] = _parse_param(cli[name], kind)
                except ValueError:
                    self.add(f"{where}: {cli[name]!r} isn't {type_name(kind)}")
                    continue
            elif name in from_env:
                values[name] = from_env[name]
            elif "default" in spec:
                values[name] = spec["default"]
            else:
                self.add(f"{where} needs a value; give it with --param {name}=...")
                continue
            problem = setting.check(values[name])
            if problem and values[name] is not None:
                self.add(f"{where}: {problem}")
        for name in [*cli, *from_env]:
            if name not in declared:
                self.add(f"there is no parameter called {name!r}.{_suggest(name, declared)}")
        return values

    # ------------------------------------------------------- placeholders
    def refs(
        self,
        where: str,
        value: Any,
        steps: Iterable[str],
        params: Mapping[str, Any],
        *,
        me: str = "",
        in_failure: bool = False,
    ) -> list[str]:
        """Check the placeholders in ``value``; return the steps it refers to."""
        try:
            found = references(value)
        except TemplateError as exc:
            self.add(f"{where}: {exc}")
            return []
        used: list[str] = []
        steps = list(steps)
        for root, path in found:
            first = path.split(".")[0] if path else ""
            text = "{{ " + ".".join(filter(None, (root, path))) + " }}"
            if root == "params":
                if first and first not in params:
                    self.add(
                        f"{where}: {text}: the flow has no parameter {first!r}."
                        + _suggest(first, params)
                    )
            elif root == "env":
                if not first:
                    self.add(f"{where}: write {{{{ env.NAME }}}} to use one environment variable")
                elif first not in os.environ:
                    self.add(f"{where}: environment variable {first} isn't set")
            elif root == "failure":
                if not in_failure:
                    self.add(f"{where}: {text} can be used only in on_failure")
                elif first and first not in _FAILURE_FIELDS:
                    self.add(f"{where}: {text}: failure has {', '.join(_FAILURE_FIELDS)}")
            elif in_failure:
                self.add(f"{where}: on_failure can't use step results ({text})")
            elif root == me:
                self.add(f"{where}: a step can't use its own result ({text})")
            elif root not in steps:
                self.add(
                    f"{where}: {text} refers to a step called {root!r}, which doesn't exist."
                    + _suggest(root, steps)
                )
            elif root not in used:
                used.append(root)
        return used

    # ----------------------------------------------------------- settings
    def settings(
        self, where: str, spec: BlockSpec, settings: Any, steps: Iterable[str],
        params: Mapping[str, Any], *, me: str = "", in_failure: bool = False,
    ) -> list[str]:  # fmt: skip
        if not isinstance(settings, dict):
            self.add(f"{where}: 'settings' should be an object")
            return []
        takes_any = any(
            p.kind is p.VAR_KEYWORD for p in inspect.signature(spec.fn).parameters.values()
        )
        names = [s.name for s in spec.settings]
        used: list[str] = []
        for name, value in settings.items():
            setting = spec.setting(name)
            if setting is None:
                if not takes_any:
                    self.add(f"{where}: unknown setting {name!r}.{_suggest(name, names)}")
                continue
            here = f"{where}, setting {name!r}"
            found = self.refs(here, value, steps, params, me=me, in_failure=in_failure)
            used += [s for s in found if s not in used]
            if setting.secret and value not in ("", None):
                if not (isinstance(value, str) and _ENV_ONLY.fullmatch(value)):
                    self.add(
                        f"{here}: secrets can't be written in the flow; use {{{{ env.NAME }}}}"
                    )
            elif _is_literal(value):  # placeholders are checked once they are filled in
                problem = setting.check(value)
                if problem:
                    self.add(f"{here}: {problem}")
        for s in spec.settings:
            if s.required and s.name not in settings:
                self.add(f"{where}: missing required setting {s.name!r}")
        return used

    def block(self, where: str, raw: dict[str, Any]) -> BlockSpec | None:
        if "block" not in raw:
            self.add(f"{where}: missing 'block' (which block this step uses)")
            return None
        try:
            return get_block(str(raw["block"]))
        except BlockError as exc:
            self.add(f"{where}: {exc}")
            return None


def _is_literal(value: Any) -> bool:
    """True if ``value`` contains no placeholders (malformed ones count as placeholders)."""
    try:
        return not references(value)
    except TemplateError:
        return False


def load_json_flow(
    path: str | os.PathLike[str],
    *,
    params: Mapping[str, str] | None = None,
    environment: str | None = None,
    blocks: Iterable[str | os.PathLike[str]] = (),
) -> Flow:
    """Read, check and build the JSON flow at ``path``."""
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(p)
    try:
        spec = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FlowFileError(
            p, [f"isn't valid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}"]
        ) from None
    c = _Checker(p)
    if not isinstance(spec, dict):
        raise FlowFileError(p, ["a flow file should be a JSON object with a 'steps' field"])

    for folder in [p.parent / "blocks", *map(Path, blocks)]:
        if folder.is_dir():
            try:
                load_plugins(folder)
            except BlockError as exc:
                c.add(str(exc))

    for key in spec:
        if key not in _TOP:
            c.add(f"unknown top-level field {key!r}.{_suggest(key, _TOP)}")

    from_env: dict[str, Any] = {}
    if environment is not None:
        env_dir = p.parent / "environments"
        values_file = env_dir / f"{environment}.json"
        if not values_file.is_file():
            names = sorted(f.stem for f in env_dir.glob("*.json")) if env_dir.is_dir() else []
            c.add(
                f"there is no environment called {environment!r} ({values_file} is missing)."
                + _suggest(environment, names)
            )
        else:
            try:
                loaded = json.loads(values_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                loaded = None
                c.add(f"{values_file.name} isn't valid JSON: {exc.msg} at line {exc.lineno}")
            if isinstance(loaded, dict):
                from_env = loaded
            elif loaded is not None:
                c.add(f"{values_file.name} should be an object of parameter values")
            if (env_dir / f"{environment}.env").is_file():
                _read_env_file(env_dir / f"{environment}.env")

    values = c.params(spec.get("params", {}), params or {}, from_env)

    steps = spec.get("steps")
    if not isinstance(steps, dict) or not steps:
        c.add('the flow has no steps; add some under "steps"')
        raise FlowFileError(p, c.problems)

    names = list(steps)
    tasks: list[Task] = []
    for name, raw in steps.items():
        where = f"step {name!r}"
        if name in _RESERVED:
            c.add(f"{name!r} can't be used as a step name (it's reserved for {{{{ {name}.… }}}})")
            continue
        if not _NAME.fullmatch(name):
            c.add(
                f"{where}: step names may only use letters, digits and _,"
                " and can't start with a digit"
            )
            continue
        if not isinstance(raw, dict):
            c.add(f"{where} should be an object with a 'block' and its 'settings'")
            continue
        for key in raw:
            if key not in _STEP_FIELDS:
                c.add(f"{where}: unknown field {key!r}.{_suggest(key, _STEP_FIELDS)}")
        block = c.block(where, raw)
        if block is None:
            continue
        deps = c.settings(where, block, raw.get("settings", {}), names, values, me=name)
        after = raw.get("after", [])
        if not isinstance(after, list) or not all(isinstance(a, str) for a in after):
            c.add(f"{where}: 'after' should be a list of step names")
            after = []
        for a in after:
            if a not in names:
                c.add(f"{where}: 'after' names {a!r}, which isn't a step.{_suggest(a, names)}")
            elif a not in deps:
                deps.append(a)
        retries = raw.get("retries", 0)
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            c.add(f"{where}: retries should be a whole number, 0 or more")
            retries = 0
        timeout = raw.get("timeout")
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0
        ):
            c.add(f"{where}: timeout should be a number of seconds above 0")
            timeout = None
        kind = raw.get("kind", block.kind)
        if kind not in KINDS:
            c.add(f"{where}: kind should be one of {', '.join(KINDS)}, not {kind!r}")
            kind = block.kind
        elif (kind == "async") != (block.kind == "async"):
            c.add(f"{where}: block {block.name!r} can't run with kind {kind!r}")
            kind = block.kind
        if c.problems:
            continue  # keep checking, but don't build tasks for a broken flow
        tasks.append(
            task(
                run_step_async if kind == "async" else run_step,
                name=name,
                kind=kind,
                after=deps,
                retries=retries,
                timeout=timeout,
                kwargs={
                    "block": block.name,
                    "ref": block.ref,
                    "settings": raw.get("settings", {}),
                    "inputs": deps,
                    "params": values,
                    "base_dir": str(p.parent),
                },
            )
        )

    on_failure = spec.get("on_failure")
    if on_failure is not None:
        where = "on_failure"
        if not isinstance(on_failure, dict):
            c.add("on_failure should be an object with a 'block' and its 'settings'")
        else:
            block = c.block(where, on_failure)
            if block is not None:
                c.settings(
                    where, block, on_failure.get("settings", {}), names, values, in_failure=True
                )

    if not c.problems:
        try:
            dag = DAG.from_tasks(tasks)
        except CycleError as exc:
            c.add("these steps depend on each other: " + " -> ".join(exc.cycle))
        except GraphError as exc:  # pragma: no cover - names and dependencies are checked above
            c.add(str(exc))
    if c.problems:
        raise FlowFileError(p, c.problems)
    return Flow(
        name=p.stem,
        path=p,
        dag=dag,
        description=str(spec.get("description", "")),
        params=values,
        on_failure=on_failure,
    )
