"""Synthetic graphs and task bodies for the evaluation.

Task bodies are plain module-level functions, so the same function can back many
``cpu`` tasks (they reach worker processes by reference). Every body accepts and
ignores upstream results, because MiniFlow passes them as positional arguments.
"""

from __future__ import annotations

import asyncio
import functools
import random
import threading
import time
from collections.abc import Callable
from typing import Any

from miniflow import DAG, Task, task

Edges = dict[str, list[str]]

# ---------------------------------------------------------------- task bodies


def noop(*_upstream: Any) -> None:
    return None


def sleep_io(*_upstream: Any, seconds: float) -> None:
    """Blocking wait, standing in for a network or disk call."""
    time.sleep(seconds)


async def sleep_async(*_upstream: Any, seconds: float) -> None:
    await asyncio.sleep(seconds)


def burn_cpu(*_upstream: Any, iterations: int) -> int:
    """Pure-Python arithmetic that holds the GIL for the whole call."""
    acc = 0
    for i in range(iterations):
        acc = (acc + i * i) % 1_000_003
    return acc


_attempts: dict[tuple[int, str], int] = {}
_attempts_lock = threading.Lock()


def flaky_io(*_upstream: Any, p: float, seed: int, name: str, seconds: float) -> None:
    """Fail each attempt with probability ``p``, deterministically per (seed, task, attempt)."""
    with _attempts_lock:
        attempt = _attempts.get((seed, name), 0) + 1
        _attempts[(seed, name)] = attempt
    time.sleep(seconds)
    if random.Random(f"{seed}:{name}:{attempt}").random() < p:
        raise ConnectionError(f"{name}: injected failure on attempt {attempt}")


def reset_fault_counters() -> None:
    with _attempts_lock:
        _attempts.clear()


def calibrate_cpu(target_seconds: float) -> int:
    """Iterations of :func:`burn_cpu` that take about ``target_seconds`` on this machine."""
    n = 20_000
    while True:
        start = time.perf_counter()
        burn_cpu(iterations=n)
        took = time.perf_counter() - start
        if took > 0.02:
            break
        n *= 2
    best = took
    for _ in range(3):  # take the fastest of a few runs to dodge noise
        start = time.perf_counter()
        burn_cpu(iterations=n)
        best = min(best, time.perf_counter() - start)
    return max(1, int(n * target_seconds / best))


# ------------------------------------------------------------------ graph shapes


def chain(n: int) -> Edges:
    """t0 -> t1 -> ... : no parallelism at all."""
    return {f"t{i}": ([f"t{i - 1}"] if i else []) for i in range(n)}


def fan_out(n: int) -> Edges:
    """One root, ``n`` independent tasks, one join: embarrassingly parallel."""
    edges: Edges = {"root": []}
    edges.update({f"t{i}": ["root"] for i in range(n)})
    edges["join"] = [f"t{i}" for i in range(n)]
    return edges


def diamond(stages: int, width: int) -> Edges:
    """``stages`` diamonds in a row: split into ``width`` tasks, then join, repeat."""
    edges: Edges = {"s0": []}
    for s in range(stages):
        mids = [f"s{s}_{i}" for i in range(width)]
        for m in mids:
            edges[m] = [f"s{s}"]
        edges[f"s{s + 1}"] = mids
    return edges


def layered(layers: int, width: int, seed: int = 0, max_parents: int = 3) -> Edges:
    """Random layered DAG: every task depends on 1..max_parents tasks of the previous layer."""
    rng = random.Random(seed)
    edges: Edges = {}
    for layer in range(layers):
        for i in range(width):
            name = f"l{layer}_{i}"
            if layer == 0:
                edges[name] = []
            else:
                k = rng.randint(1, min(max_parents, width))
                edges[name] = sorted(rng.sample([f"l{layer - 1}_{j}" for j in range(width)], k))
    return edges


SHAPES: dict[str, Callable[[], Edges]] = {
    "chain": lambda: chain(12),
    "fan-out": lambda: fan_out(32),
    "diamond": lambda: diamond(4, 8),
    "layered": lambda: layered(6, 8, seed=1),
}


def depth(edges: Edges) -> int:
    """Number of tasks on the longest path (the span when every task costs the same)."""
    level: dict[str, int] = {}
    for name in DAG.from_tasks(build(edges, noop)).topological_order():
        level[name] = 1 + max((level[u] for u in edges[name]), default=0)
    return max(level.values(), default=0)


def level_sizes(edges: Edges) -> list[int]:
    """Tasks per dependency level (level = longest path from a root)."""
    level: dict[str, int] = {}
    for name in DAG.from_tasks(build(edges, noop)).topological_order():
        level[name] = 1 + max((level[u] for u in edges[name]), default=0)
    sizes = [0] * max(level.values(), default=0)
    for lv in level.values():
        sizes[lv - 1] += 1
    return sizes


def level_bound(edges: Edges, workers: int) -> float:
    """Speedup of the best level-by-level schedule with equal task costs.

    Each level needs ceil(size / workers) waves, so the makespan is the sum of those.
    For chains, fan-outs and diamonds (where every level waits for the one before)
    this is the optimum; for random layered graphs it is a close reference.
    """
    waves = sum(-(-size // workers) for size in level_sizes(edges))
    return len(edges) / waves


# ----------------------------------------------------------------- task builders


def build(
    edges: Edges,
    fn: Callable[..., Any],
    kind: str = "io",
    kinds: dict[str, str] | None = None,
    retries: int = 0,
    **kwargs: Any,
) -> list[Task]:
    """MiniFlow tasks for ``edges``; ``kinds`` overrides the kind per task name."""
    return [
        task(
            fn,
            name=name,
            kind=(kinds or {}).get(name, kind),
            after=ups,
            retries=retries,
            kwargs=kwargs,
        )
        for name, ups in edges.items()
    ]


def dask_graph(edges: Edges, fn: Callable[..., Any], **kwargs: Any) -> dict[str, Any]:
    """The same graph for Dask: each key calls ``fn(*upstream_results, **kwargs)``."""
    call = functools.partial(fn, **kwargs)
    return {name: (call, *ups) for name, ups in edges.items()}


def sinks(edges: Edges) -> list[str]:
    used = {u for ups in edges.values() for u in ups}
    return [n for n in edges if n not in used]


def run_sequentially(edges: Edges, fn: Callable[..., Any], **kwargs: Any) -> float:
    """Wall time to call every task body once, in dependency order, in this process."""
    order = DAG.from_tasks(build(edges, noop)).topological_order()
    start = time.perf_counter()
    for _ in order:
        fn(**kwargs)
    return time.perf_counter() - start
