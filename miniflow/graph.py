"""DAG construction and validation.

:class:`DAG` checks a set of tasks for duplicate names, unknown dependencies
and cycles, then exposes the graph algorithms the scheduler needs:

* :meth:`DAG.topological_order` - Kahn's algorithm [1].
* :meth:`DAG.find_cycle` - iterative depth-first search that returns one cycle
  path, used to build readable error messages [2].
* :meth:`DAG.critical_path_lengths` - longest path from each task to a sink,
  the HEFT-style rank used to prioritise the ready queue [6].
* :meth:`DAG.descendants` - breadth-first traversal used to mark SKIPPED tasks.
"""

from __future__ import annotations

import types
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from miniflow.task import Task, first_paragraph

__all__ = ["DAG", "CycleError", "DuplicateTaskError", "GraphError", "UnknownDependencyError"]


class GraphError(ValueError):
    """Base class for DAG validation errors."""


class DuplicateTaskError(GraphError):
    def __init__(self, name: str) -> None:
        super().__init__(f"two tasks are named {name!r}")
        self.name = name


class UnknownDependencyError(GraphError):
    def __init__(self, task: str, missing: str) -> None:
        super().__init__(f"task {task!r} depends on unknown task {missing!r}")
        self.task = task
        self.missing = missing


class CycleError(GraphError):
    def __init__(self, cycle: list[str]) -> None:
        super().__init__("dependency cycle: " + " -> ".join(cycle))
        self.cycle = cycle


def describe_module(module: types.ModuleType) -> dict[str, Any]:
    """Title and story of a flow file: its docstring's first line and first paragraph after it.

    Optional module-level settings: ``WATCH`` (a string: what to look for while the flow
    runs) and ``HIGHLIGHTS`` (task names whose results summarise the run best).
    """
    lines = (module.__doc__ or "").strip().splitlines()
    about: dict[str, Any] = {
        "title": lines[0].strip() if lines else module.__name__.rsplit(".", 1)[-1]
    }
    about["description"] = first_paragraph("\n".join(lines[1:]))
    watch = getattr(module, "WATCH", "")
    if isinstance(watch, str) and watch:
        about["watch"] = " ".join(watch.split())
    highlights = getattr(module, "HIGHLIGHTS", None)
    if isinstance(highlights, (list, tuple)):
        about["highlights"] = [str(h) for h in highlights]
    return about


@dataclass(frozen=True)
class DAG:
    """A validated, immutable task graph. Build it with :meth:`from_tasks`."""

    tasks: Mapping[str, Task]
    upstream: Mapping[str, tuple[str, ...]]
    downstream: Mapping[str, tuple[str, ...]]
    #: Human-readable description of the whole flow: ``title``, ``description`` and
    #: ``watch`` (what to look for while it runs), ``highlights``. Shown by the dashboard.
    about: Mapping[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ build
    @classmethod
    def from_tasks(cls, tasks: Iterable[Task], about: Mapping[str, Any] | None = None) -> DAG:
        """Build and validate a DAG. Raises a :class:`GraphError` subclass on failure."""
        by_name: dict[str, Task] = {}
        for t in tasks:
            if t.name in by_name:
                if by_name[t.name] is t:
                    continue
                raise DuplicateTaskError(t.name)
            by_name[t.name] = t

        downstream: dict[str, list[str]] = {name: [] for name in by_name}
        for t in by_name.values():
            for dep in t.after:
                if dep not in by_name:
                    raise UnknownDependencyError(t.name, dep)
                downstream[dep].append(t.name)

        dag = cls(
            tasks=types.MappingProxyType(by_name),
            upstream=types.MappingProxyType({n: t.after for n, t in by_name.items()}),
            downstream=types.MappingProxyType({n: tuple(d) for n, d in downstream.items()}),
            about=types.MappingProxyType(dict(about or {})),
        )
        if len(dag._kahn()) != len(by_name):
            cycle = dag.find_cycle()
            assert cycle is not None
            raise CycleError(cycle)
        return dag

    @classmethod
    def from_module(cls, module: types.ModuleType) -> DAG:
        """Collect every :class:`Task` defined at module level, in definition order."""
        return cls.from_tasks(
            (v for v in vars(module).values() if isinstance(v, Task)), about=describe_module(module)
        )

    # ------------------------------------------------------------- algorithms
    def __len__(self) -> int:
        return len(self.tasks)

    def __contains__(self, name: object) -> bool:
        return name in self.tasks

    def _kahn(self) -> list[str]:
        indegree = {n: len(ups) for n, ups in self.upstream.items()}
        queue = deque(n for n, d in indegree.items() if d == 0)
        order: list[str] = []
        while queue:
            n = queue.popleft()
            order.append(n)
            for child in self.downstream[n]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        return order

    def topological_order(self) -> list[str]:
        """Return task names so that every task follows all of its upstream tasks."""
        return self._kahn()

    def find_cycle(self) -> list[str] | None:
        """Return one cycle as ``[a, b, ..., a]``, or ``None`` if the graph is acyclic.

        Iterative so that very deep chains do not hit the recursion limit.
        """
        white, grey, black = 0, 1, 2
        colour = dict.fromkeys(self.tasks, white)
        parent: dict[str, str] = {}
        for root in self.tasks:
            if colour[root] != white:
                continue
            colour[root] = grey
            stack: list[tuple[str, int]] = [(root, 0)]
            while stack:
                node, i = stack[-1]
                children = self.downstream[node]
                if i == len(children):
                    colour[node] = black
                    stack.pop()
                    continue
                stack[-1] = (node, i + 1)
                child = children[i]
                if colour[child] == grey:
                    path = [child, node]
                    while path[-1] != child:
                        path.append(parent[path[-1]])
                    path.reverse()
                    return path
                if colour[child] == white:
                    colour[child] = grey
                    parent[child] = node
                    stack.append((child, 0))
        return None

    def critical_path_lengths(
        self, weight: Callable[[Task], float] | None = None
    ) -> dict[str, float]:
        """Longest path (sum of task weights) from each task to any sink, inclusive.

        With the default weight of 1 per task this is the number of tasks on the
        longest remaining chain, so tasks that gate more work rank higher.
        """
        w = weight or (lambda _t: 1.0)
        rank: dict[str, float] = {}
        for n in reversed(self._kahn()):
            rank[n] = w(self.tasks[n]) + max((rank[c] for c in self.downstream[n]), default=0.0)
        return rank

    def descendants(self, name: str) -> set[str]:
        """All tasks reachable downstream of ``name`` (excluding itself), by BFS."""
        seen: set[str] = set()
        queue = deque(self.downstream[name])
        while queue:
            n = queue.popleft()
            if n not in seen:
                seen.add(n)
                queue.extend(self.downstream[n])
        return seen

    def ancestors(self, name: str) -> set[str]:
        """All tasks upstream of ``name`` (excluding itself)."""
        seen: set[str] = set()
        queue = deque(self.upstream[name])
        while queue:
            n = queue.popleft()
            if n not in seen:
                seen.add(n)
                queue.extend(self.upstream[n])
        return seen

    def roots(self) -> list[str]:
        return [n for n, ups in self.upstream.items() if not ups]

    def sinks(self) -> list[str]:
        return [n for n, downs in self.downstream.items() if not downs]

    # ----------------------------------------------------------------- export
    def to_dot(self, states: Mapping[str, str] | None = None) -> str:
        """Render the graph in Graphviz DOT. ``states`` optionally colours nodes by state."""
        colours = {
            "PENDING": "white",
            "READY": "lightyellow",
            "RUNNING": "lightblue",
            "RETRYING": "orange",
            "SUCCESS": "palegreen",
            "FAILED": "salmon",
            "SKIPPED": "lightgrey",
        }
        shapes = {"io": "box", "cpu": "box3d", "async": "ellipse"}
        lines = [
            "digraph miniflow {",
            "  rankdir=LR;",
            '  node [style=filled, fontname="Helvetica"];',
        ]
        for n, t in self.tasks.items():
            state = (states or {}).get(n)
            fill = colours.get(state or "", "white")
            label = f"{n}\\n[{t.kind}]" + (f"\\n{state}" if state else "")
            lines.append(f'  "{n}" [label="{label}", shape={shapes[t.kind]}, fillcolor={fill}];')
        for n, ups in self.upstream.items():
            lines.extend(f'  "{u}" -> "{n}";' for u in ups)
        lines.append("}")
        return "\n".join(lines) + "\n"
