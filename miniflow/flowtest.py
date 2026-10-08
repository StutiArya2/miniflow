"""Execute JSON-flow examples with sample step results and exact assertions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from miniflow.flowfile import FlowFileError, load_json_flow
from miniflow.graph import DAG
from miniflow.scheduler import Scheduler
from miniflow.store import StateStore
from miniflow.task import Task, task

__all__ = ["run_flow_tests"]


def _sample(value: Any) -> Any:
    return value


def _parameter_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _select_tasks(dag: DAG, targets: set[str], replacements: Mapping[str, Any]) -> DAG:
    needed: set[str] = set()

    def visit(name: str) -> None:
        if name in needed:
            return
        needed.add(name)
        if name not in replacements:
            for upstream in dag.upstream[name]:
                visit(upstream)

    for target in targets:
        visit(target)
    selected: list[Task] = []
    for name in dag.topological_order():
        if name not in needed:
            continue
        if name in replacements:
            selected.append(task(_sample, name=name, args=(replacements[name],)))
        else:
            selected.append(dag.tasks[name])
    return DAG.from_tasks(selected)


def run_flow_tests(path: str | Path, document: Any | None = None) -> list[dict[str, Any]]:
    """Run each declared case in memory; return name, pass/fail and concise problems.

    ``inputs`` replaces named step results, so their upstream branches do not run.
    Only ancestors of asserted steps execute. Assertions compare JSON values exactly.
    The normal run database is untouched.
    """
    source = Path(path).resolve()
    if document is None:
        document = json.loads(source.read_text(encoding="utf-8"))
    # Structural validation happens before any test starts.
    checked = load_json_flow(source, _source=document, _nested=True)
    if not checked.tests:
        raise FlowFileError(source, ["add at least one case under 'tests' before running tests"])
    reports: list[dict[str, Any]] = []
    for case in checked.tests:
        name = str(case["name"])
        try:
            params = {key: _parameter_text(value) for key, value in case.get("params", {}).items()}
            flow = load_json_flow(source, _source=document, params=params)
        except FlowFileError as exc:
            reports.append({"name": name, "passed": False, "problems": exc.problems})
            continue
        inputs = case.get("inputs", {})
        expected = case["expect"]
        dag = _select_tasks(flow.dag, set(expected), inputs)
        unused = sorted(set(inputs) - set(dag.tasks))
        if unused:
            reports.append(
                {
                    "name": name,
                    "passed": False,
                    "problems": [
                        f"sample input {step!r} is not used by an expected step" for step in unused
                    ],
                }
            )
            continue
        with StateStore() as store:
            result = Scheduler(dag, store=store, flow=f"{flow.name}:{name}").run()
        problems = []
        for step, wanted in expected.items():
            if step not in result.results:
                problems.append(
                    f"{step}: did not produce a result ({result.errors.get(step, result.status)})"
                )
            else:
                try:
                    actual_json = json.dumps(result.results[step], sort_keys=True)
                    wanted_json = json.dumps(wanted, sort_keys=True)
                except (TypeError, ValueError):
                    problems.append(f"{step}: result cannot be compared as JSON")
                else:
                    if actual_json != wanted_json:
                        problems.append(f"{step}: result did not match the expected value")
        reports.append({"name": name, "passed": not problems, "problems": problems})
    return reports
