"""Layered DAG layout for the dashboard, computed server-side so the page only draws.

Works on the stored graph JSON (``{"tasks": {name: {"kind": ..., "after": [...]}}}``),
so a run can be drawn without its flow file.

* **Columns**: a task's column is the length of the longest path from any root
  to it, so every edge points to a later column.
* **Rows**: tasks in a column are reordered by the barycenter heuristic (average
  row of their neighbours in the adjacent column), sweeping right and then
  left a few times, which removes most edge crossings in typical pipelines.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from typing import Any

__all__ = ["layered_layout"]

_SWEEPS = 4


def layered_layout(graph: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """Return ``{task: {"col": c, "row": r}}`` for the stored graph JSON."""
    tasks: Mapping[str, Mapping[str, Any]] = graph["tasks"]
    upstream = {n: [u for u in info.get("after", []) if u in tasks] for n, info in tasks.items()}
    downstream: dict[str, list[str]] = {n: [] for n in tasks}
    for n, ups in upstream.items():
        for u in ups:
            downstream[u].append(n)

    # Longest path from a root, in topological (Kahn) order.
    indegree = {n: len(ups) for n, ups in upstream.items()}
    queue = deque(n for n, d in indegree.items() if d == 0)
    col: dict[str, int] = {}
    while queue:
        n = queue.popleft()
        col[n] = max((col[u] + 1 for u in upstream[n]), default=0)
        for child in downstream[n]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    for n in tasks:  # defensive: a stored graph should be acyclic
        col.setdefault(n, 0)

    columns: list[list[str]] = [[] for _ in range(max(col.values(), default=-1) + 1)]
    for n in tasks:  # stored order is topological, a good starting point
        columns[col[n]].append(n)

    def reorder(
        column: list[str], neighbours: Mapping[str, list[str]], pos: dict[str, int]
    ) -> None:
        def key(n: str) -> tuple[float, int]:
            linked = [pos[m] for m in neighbours[n] if m in pos]
            centre = sum(linked) / len(linked) if linked else float(pos.get(n, 0))
            return (centre, pos.get(n, 0))

        column.sort(key=key)

    for sweep in range(_SWEEPS):
        pos = {n: i for c in columns for i, n in enumerate(c)}
        if sweep % 2 == 0:
            for c in columns[1:]:
                reorder(c, upstream, pos)
                pos.update({n: i for i, n in enumerate(c)})
        else:
            for c in reversed(columns[:-1]):
                reorder(c, downstream, pos)
                pos.update({n: i for i, n in enumerate(c)})

    return {n: {"col": ci, "row": ri} for ci, c in enumerate(columns) for ri, n in enumerate(c)}
