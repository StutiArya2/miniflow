"""Command line interface (spec §3.4). ``run``/``resume``/``status`` arrive in Phase 3."""

from __future__ import annotations

import argparse
import importlib.util
import sys
import types
from collections.abc import Sequence
from pathlib import Path

from miniflow.graph import DAG, GraphError

__all__ = ["load_flow", "main"]


def load_flow(path: str | Path) -> tuple[types.ModuleType, DAG]:
    """Import a flow file and build its DAG from the module-level tasks."""
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(p)
    name = p.stem
    spec = importlib.util.spec_from_file_location(name, p)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import {p}")
    module = importlib.util.module_from_spec(spec)
    # Registered so process workers can re-import cpu tasks by module name.
    sys.modules[name] = module
    sys.path.insert(0, str(p.parent))
    spec.loader.exec_module(module)
    return module, DAG.from_module(module)


def _cmd_graph(args: argparse.Namespace) -> int:
    _, dag = load_flow(args.flow)
    dot = dag.to_dot()
    if args.output:
        Path(args.output).write_text(dot, encoding="utf-8")
    else:
        sys.stdout.write(dot)
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    _, dag = load_flow(args.flow)
    order = dag.topological_order()
    print(f"OK: {len(dag)} tasks, order: {' -> '.join(order)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="miniflow", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("graph", help="export the DAG in Graphviz DOT format")
    g.add_argument("flow", help="path to a Python file defining tasks")
    g.add_argument("-o", "--output", help="write DOT to this file instead of stdout")
    g.set_defaults(func=_cmd_graph)

    v = sub.add_parser("validate", help="check the DAG and print a topological order")
    v.add_argument("flow")
    v.set_defaults(func=_cmd_validate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except GraphError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
