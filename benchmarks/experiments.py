"""The experiments of spec §3.8, plus experiment 6 (failure-aware priority and pruning).

Each returns a list of flat dict rows (one CSV each).

Every MiniFlow run is also checked for correctness (experiment 5): no attempt may
start before all of its upstream tasks finished, using the timestamps the workers
recorded in the state store.
"""

from __future__ import annotations

import multiprocessing
import os
import random
import statistics
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from benchmarks import workloads as W
from miniflow import DAG, ExecutorRouter, RetryPolicy, Scheduler, State, StateStore, Task
from miniflow.priority import doomed, learn_history

SPAWN = multiprocessing.get_context("spawn")


def physical_cores() -> int | None:
    """Physical core count when psutil is available (bench group), else None."""
    try:
        import psutil
    except ImportError:
        return None
    count: int | None = psutil.cpu_count(logical=False)
    return count


@dataclass
class Config:
    """Sizes for one benchmark session. ``quick()`` is for smoke tests and CI."""

    repeats: int = 3
    workers: int = field(default_factory=lambda: os.cpu_count() or 4)
    task_seconds: float = 0.05
    shapes: tuple[str, ...] = ("chain", "fan-out", "diamond", "layered")
    overhead_sizes: tuple[int, ...] = (10, 100, 500, 1000, 2000, 5000)
    fault_rates: tuple[float, ...] = (0.0, 0.05, 0.1, 0.2, 0.3, 0.5)
    fault_retries: tuple[int, ...] = (0, 1, 3)
    fault_seeds: int = 20
    worker_choice_io: int = 32
    worker_choice_cpu: int = 8
    use_dask: bool = True
    fa_seeds: int = 40
    fa_train: int = 8
    fa_workers: int = 4
    fa_task_seconds: float = 0.02

    @classmethod
    def quick(cls) -> Config:
        return cls(
            repeats=1,
            workers=2,
            task_seconds=0.01,
            shapes=("chain", "fan-out"),
            overhead_sizes=(10, 50),
            fault_rates=(0.0, 0.3),
            fault_retries=(0, 2),
            fault_seeds=2,
            worker_choice_io=4,
            worker_choice_cpu=2,
            fa_seeds=3,
            fa_train=2,
            fa_workers=2,
            fa_task_seconds=0.003,
        )


@dataclass
class Correctness:
    """Experiment 5: a tally of every MiniFlow run checked against the store."""

    runs: int = 0
    attempts: int = 0
    violations: list[str] = field(default_factory=list)

    def check(self, store: StateStore, run_id: str, dag: DAG) -> None:
        attempts = store.attempts(run_id)
        finished = {a.task: a.finished_at for a in attempts if a.state is State.SUCCESS}
        for a in attempts:
            for up in dag.upstream[a.task]:
                done = finished.get(up)
                if done is None or a.started_at is None or done > a.started_at:
                    self.violations.append(f"{run_id}: {a.task} started before {up} finished")
        states = store.get_states(run_id)
        errors = {r.task: r.error or "" for r in store.changes_since(run_id, 0.0)}
        for name, state in states.items():
            if state is not State.SKIPPED or any(
                states[anc] is State.FAILED for anc in dag.ancestors(name)
            ):
                continue
            # Pruning (experiment 6) may skip a task only when none of its results can be used.
            pruned = errors.get(name, "").startswith("pruned:")
            useless = bool(dag.downstream[name]) and all(
                states[c] in (State.SKIPPED, State.FAILED) for c in dag.downstream[name]
            )
            if not (pruned and useless):
                self.violations.append(f"{run_id}: {name} SKIPPED without a FAILED ancestor")
        self.runs += 1
        self.attempts += len(attempts)

    def rows(self) -> list[dict[str, Any]]:
        return [
            {
                "runs_checked": self.runs,
                "attempts_checked": self.attempts,
                "violations": len(self.violations),
                "first_violation": self.violations[0] if self.violations else "",
            }
        ]


def _median_time(fn: Callable[[], Any], repeats: int) -> float:
    times = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        times.append(time.perf_counter() - start)
    return statistics.median(times)


def _miniflow(
    tasks: list[Task],
    router: ExecutorRouter,
    checker: Correctness,
    store: StateStore | None = None,
    policy: RetryPolicy | None = None,
) -> Any:
    dag = DAG.from_tasks(tasks)
    own = store is None
    store = store or StateStore()
    try:
        result = Scheduler(dag, router=router, store=store, policy=policy).run()
        checker.check(store, result.run_id, dag)
        return result
    finally:
        if own:
            store.close()


def _dask() -> Any:
    try:
        import dask.multiprocessing
        import dask.threaded
    except ImportError:
        return None
    return dask


# --------------------------------------------------------------- 1. speedup
def speedup(cfg: Config, checker: Correctness) -> list[dict[str, Any]]:
    """Wall time vs sequential execution on synthetic DAGs, with the work/span bound."""
    rows = []
    iterations = W.calibrate_cpu(cfg.task_seconds)
    dask = _dask() if cfg.use_dask else None
    bodies = {
        "io": (W.sleep_io, {"seconds": cfg.task_seconds}),
        "cpu": (W.burn_cpu, {"iterations": iterations}),
    }
    with ExecutorRouter(
        io_workers=cfg.workers, cpu_workers=cfg.workers, mp_context=SPAWN
    ) as router:
        dask_pool = ProcessPoolExecutor(cfg.workers, mp_context=SPAWN) if dask else None
        try:
            for kind, (fn, kwargs) in bodies.items():
                for shape in cfg.shapes:
                    edges = W.SHAPES[shape]()
                    tasks = W.build(edges, fn, kind=kind, **kwargs)
                    _miniflow(tasks, router, checker)  # warm up the pools
                    sequential = _median_time(
                        lambda e=edges, f=fn, k=kwargs: W.run_sequentially(e, f, **k), cfg.repeats
                    )
                    mini = _median_time(lambda t=tasks: _miniflow(t, router, checker), cfg.repeats)
                    n, span = len(edges), W.depth(edges)
                    # Pure-Python cpu work can't use hyperthreads, so its ceiling is the
                    # physical core count; io waits can use every worker.
                    usable = cfg.workers
                    if kind == "cpu":
                        usable = min(cfg.workers, physical_cores() or cfg.workers)
                    row: dict[str, Any] = {
                        "kind": kind,
                        "shape": shape,
                        "tasks": n,
                        "depth": span,
                        "workers": cfg.workers,
                        "usable_workers": usable,
                        "sequential_s": sequential,
                        "miniflow_s": mini,
                        "miniflow_speedup": sequential / mini,
                        # Work/span bound (Brent): with P workers and equal task costs the
                        # best possible speedup is min(P, work / span) = min(P, n / depth).
                        "ideal_speedup": min(cfg.workers, n / span),
                        "level_speedup": W.level_bound(edges, usable),
                        # Amdahl view of the same graph: serial fraction = depth / n.
                        "amdahl_speedup": 1 / ((span / n) + (1 - span / n) / cfg.workers),
                    }
                    if dask is not None:
                        graph = W.dask_graph(edges, fn, **kwargs)
                        keys = W.sinks(edges)
                        if kind == "io":
                            get = lambda g=graph, k=keys: dask.threaded.get(  # noqa: E731
                                g, k, num_workers=cfg.workers
                            )
                        else:
                            get = lambda g=graph, k=keys: dask.multiprocessing.get(  # noqa: E731
                                g, k, pool=dask_pool
                            )
                        get()  # warm up
                        dask_time = _median_time(get, cfg.repeats)
                        row["dask_s"] = dask_time
                        row["dask_speedup"] = sequential / dask_time
                    rows.append(row)
        finally:
            if dask_pool is not None:
                dask_pool.shutdown()
    return rows


# --------------------------------------------------------- 2. worker choice
def worker_choice(cfg: Config, checker: Correctness) -> list[dict[str, Any]]:
    """The same mixed io/cpu workload with all-threads, all-processes and per-task routing."""
    iterations = W.calibrate_cpu(cfg.task_seconds * 4)
    io_names = [f"io{i}" for i in range(cfg.worker_choice_io)]
    cpu_names = [f"cpu{i}" for i in range(cfg.worker_choice_cpu)]
    edges: W.Edges = {"root": []}
    edges.update({n: ["root"] for n in io_names + cpu_names})
    edges["join"] = io_names + cpu_names

    def tasks_for(io_kind: str, cpu_kind: str) -> list[Task]:
        out = [W.build({"root": []}, W.noop)[0]]
        out += [
            W.build({n: ["root"]}, W.sleep_io, kind=io_kind, seconds=cfg.task_seconds * 4)[0]
            for n in io_names
        ]
        out += [
            W.build({n: ["root"]}, W.burn_cpu, kind=cpu_kind, iterations=iterations)[0]
            for n in cpu_names
        ]
        out += W.build({"join": edges["join"]}, W.noop)
        return out

    configs = {
        "all threads": ("io", "io"),
        "all processes": ("cpu", "cpu"),
        "MiniFlow routing": ("io", "cpu"),
    }
    sequential = W.run_sequentially(
        {n: [] for n in io_names}, W.sleep_io, seconds=cfg.task_seconds * 4
    ) + W.run_sequentially({n: [] for n in cpu_names}, W.burn_cpu, iterations=iterations)
    rows = []
    for label, (io_kind, cpu_kind) in configs.items():
        tasks = tasks_for(io_kind, cpu_kind)
        # Threads are cheap, so io gets one per task; processes are capped at the core count.
        with ExecutorRouter(
            io_workers=cfg.worker_choice_io + cfg.worker_choice_cpu,
            cpu_workers=cfg.workers,
            mp_context=SPAWN,
        ) as router:
            _miniflow(tasks, router, checker)  # warm up
            wall = _median_time(lambda t=tasks: _miniflow(t, router, checker), cfg.repeats)
        rows.append(
            {
                "config": label,
                "io_tasks": cfg.worker_choice_io,
                "cpu_tasks": cfg.worker_choice_cpu,
                "task_seconds": cfg.task_seconds * 4,
                "sequential_s": sequential,
                "wall_s": wall,
                "speedup": sequential / wall,
            }
        )
    return rows


# ------------------------------------------------------- 3. scheduler overhead
def overhead(cfg: Config, checker: Correctness) -> list[dict[str, Any]]:
    """Time per empty task as the graph grows, for MiniFlow (memory and file store) and Dask."""
    rows = []
    dask = _dask() if cfg.use_dask else None
    with ExecutorRouter(io_workers=cfg.workers) as router:
        for n in cfg.overhead_sizes:
            width = max(1, min(50, n // 5))
            edges = W.layered(max(1, n // width), width, seed=n)
            tasks = W.build(edges, W.noop)
            count = len(edges)
            direct = W.run_sequentially(edges, W.noop)
            mem = _median_time(lambda t=tasks: _miniflow(t, router, checker), cfg.repeats)
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "bench.db"

                def file_run(t: list[Task] = tasks, path: Path = db) -> None:
                    with StateStore(path) as store:
                        _miniflow(t, router, checker, store=store)

                disk = _median_time(file_run, cfg.repeats)
            row: dict[str, Any] = {
                "tasks": count,
                "edges": sum(len(u) for u in edges.values()),
                "direct_us_per_task": 1e6 * direct / count,
                "miniflow_memory_us_per_task": 1e6 * mem / count,
                "miniflow_file_us_per_task": 1e6 * disk / count,
            }
            if dask is not None:
                graph = W.dask_graph(edges, W.noop)
                keys = W.sinks(edges)
                dask_time = _median_time(
                    lambda g=graph, k=keys: dask.threaded.get(g, k, num_workers=cfg.workers),
                    cfg.repeats,
                )
                row["dask_us_per_task"] = 1e6 * dask_time / count
            rows.append(row)
    return rows


# ----------------------------------------------------------- 4. fault tolerance
def fault_tolerance(cfg: Config, checker: Correctness) -> list[dict[str, Any]]:
    """Success rate and time under injected failures, with and without retries."""
    edges = W.layered(5, 6, seed=7)  # 30 tasks
    n = len(edges)
    policy = RetryPolicy(base=0.002, cap=0.02, jitter=0.1)
    rows = []
    with ExecutorRouter(io_workers=cfg.workers) as router:
        for retries in cfg.fault_retries:
            for p in cfg.fault_rates:
                ok_runs, success_frac, walls, attempts = 0, [], [], []
                for seed in range(cfg.fault_seeds):
                    W.reset_fault_counters()
                    tasks = [
                        W.build(
                            {name: ups},
                            W.flaky_io,
                            retries=retries,
                            p=p,
                            seed=seed,
                            name=name,
                            seconds=0.002,
                        )[0]
                        for name, ups in edges.items()
                    ]
                    start = time.perf_counter()
                    result = _miniflow(tasks, router, checker, policy=policy)
                    walls.append(time.perf_counter() - start)
                    ok_runs += result.ok
                    states = list(result.states.values())
                    success_frac.append(states.count(State.SUCCESS) / n)
                    executed = [a for a in result.attempts.values() if a > 0]
                    attempts.append(sum(executed) / len(executed))
                    for name, state in result.states.items():
                        if state is State.FAILED and result.attempts[name] != retries + 1:
                            checker.violations.append(
                                f"{name} FAILED after {result.attempts[name]} attempts"
                            )
                task_ok = 1 - p ** (retries + 1)
                rows.append(
                    {
                        "retries": retries,
                        "failure_rate": p,
                        "runs": cfg.fault_seeds,
                        "run_success_rate": ok_runs / cfg.fault_seeds,
                        # Every task must succeed: P(run ok) = (1 - p^(r+1))^n.
                        "expected_run_success": task_ok**n,
                        "tasks_succeeded": statistics.mean(success_frac),
                        "attempts_per_executed_task": statistics.mean(attempts),
                        "mean_wall_s": statistics.mean(walls),
                    }
                )
    return rows


# ------------------------------------------------- 6. failure-aware priority
FA_POLICIES: dict[str, dict[str, Any]] = {
    "critical path (default)": {},
    "prune only": {"prune": True},
    "failure-aware only": {"priority": "failure_aware"},
    "failure-aware + prune": {"priority": "failure_aware", "prune": True},
}


def etl(sources: int, transforms: int, reports: int, seed: int = 0) -> W.Edges:
    """Sources -> transforms (1-3 sources each) -> validate -> reports (2-3 transforms each)."""
    rng = random.Random(seed)
    edges: W.Edges = {f"src{i}": [] for i in range(sources)}
    for i in range(transforms):
        k = rng.randint(1, min(3, sources))
        edges[f"tf{i}"] = sorted(rng.sample([f"src{j}" for j in range(sources)], k))
        edges[f"check{i}"] = [f"tf{i}"]  # e.g. a schema check against an external service
    for i in range(reports):
        k = rng.randint(2, min(3, transforms))
        edges[f"rep{i}"] = sorted(rng.sample([f"check{j}" for j in range(transforms)], k))
    return edges


def gated(groups: int, heavy: int) -> W.Edges:
    """Per group: a cheap precondition (e.g. an API or credentials check) and ``heavy``
    independent expensive steps, all joined by the group's report."""
    edges: W.Edges = {}
    for g in range(groups):
        parts = [f"gate{g}"] + [f"heavy{g}_{i}" for i in range(heavy)]
        edges.update({p: [] for p in parts})
        edges[f"report{g}"] = parts
    return edges


def _fa_tasks(edges: W.Edges, risky: set[str], seed: int, cfg: Config) -> list[Task]:
    """Risky tasks are cheap and flaky (p = 0.35 per attempt); the rest are reliable."""
    costs = random.Random(f"costs:{sorted(edges)}")
    tasks = []
    for name, ups in edges.items():
        seconds = cfg.fa_task_seconds * (0.3 if name in risky else costs.uniform(1.0, 3.0))
        p = 0.35 if name in risky else 0.0
        tasks.append(
            W.build({name: ups}, W.flaky_io, retries=1, p=p, seed=seed, name=name, seconds=seconds)[
                0
            ]
        )
    return tasks


def _fa_workloads() -> dict[str, tuple[W.Edges, set[str]]]:
    lay = W.layered(6, 8, seed=11)
    pick = random.Random(5)
    lay_risky = set(pick.sample(sorted(lay), 6))
    pipe = etl(6, 10, 6, seed=3)
    pipe_risky = {n for n in pipe if n.startswith("check")} - {"check0", "check1"}
    gate = gated(5, 6)
    gate_risky = {n for n in gate if n.startswith("gate")}
    return {
        "gated joins (40 tasks)": (gate, gate_risky),
        "etl (32 tasks)": (pipe, pipe_risky),
        "layered (48 tasks)": (lay, lay_risky),
    }


def failure_aware(cfg: Config, checker: Correctness) -> list[dict[str, Any]]:
    """Worker time and time-to-failure with failure-aware priority and pruning.

    History comes from ``fa_train`` earlier runs (different fault seeds). Faults are
    deterministic per (seed, task, attempt), so every policy faces the same failures and
    the comparison is paired: the same seeds succeed or fail under every policy.
    """
    policy = RetryPolicy(base=0.002, cap=0.01, jitter=0.0)
    rows: list[dict[str, Any]] = []
    with ExecutorRouter(io_workers=cfg.fa_workers) as router:
        for shape, (edges, risky) in _fa_workloads().items():
            dag_flow = f"fa:{shape}"
            with StateStore() as train:
                for t in range(cfg.fa_train):
                    W.reset_fault_counters()
                    tasks = _fa_tasks(edges, risky, 10_000 + t, cfg)
                    Scheduler(
                        DAG.from_tasks(tasks),
                        router=router,
                        store=train,
                        policy=policy,
                        flow=dag_flow,
                    ).run()
                history = learn_history(train, dag_flow)
            for label, options in FA_POLICIES.items():
                failed_work, failed_wall, first_fail, useless, ok_wall = [], [], [], [], []
                ok = 0
                for seed in range(cfg.fa_seeds):
                    W.reset_fault_counters()
                    dag = DAG.from_tasks(_fa_tasks(edges, risky, seed, cfg))
                    with StateStore() as store:
                        sched = Scheduler(
                            dag,
                            router=router,
                            store=store,
                            policy=policy,
                            flow=dag_flow,
                            history=history,
                            **options,
                        )
                        result = sched.run()
                        checker.check(store, result.run_id, dag)
                        attempts = store.attempts(result.run_id)
                    spent = {a.task: 0.0 for a in attempts}
                    for a in attempts:
                        if a.started_at is not None and a.finished_at is not None:
                            spent[a.task] += a.finished_at - a.started_at
                    if result.ok:
                        ok += 1
                        ok_wall.append(result.duration)
                        continue
                    dead = {
                        n for n, s in result.states.items() if s in (State.SKIPPED, State.FAILED)
                    }
                    failed_work.append(sum(spent.values()))
                    failed_wall.append(result.duration)
                    useless.append(sum(spent.get(n, 0.0) for n in doomed(dag, dead)))
                    first_fail.append(
                        min(
                            a.finished_at
                            for a in attempts
                            if a.state is State.FAILED
                            and a.finished_at is not None
                            and result.states[a.task] is State.FAILED
                            and a.attempt == dag.tasks[a.task].max_attempts
                        )
                        - result.started_at
                    )
                mean = statistics.mean
                rows.append(
                    {
                        "workload": shape,
                        "policy": label,
                        "runs": cfg.fa_seeds,
                        "run_success_rate": ok / cfg.fa_seeds,
                        "failed_runs": len(failed_work),
                        "failed_worker_s": mean(failed_work) if failed_work else 0.0,
                        "failed_wall_s": mean(failed_wall) if failed_wall else 0.0,
                        "time_to_first_failure_s": mean(first_fail) if first_fail else 0.0,
                        "useless_worker_s": mean(useless) if useless else 0.0,
                        "ok_wall_s": mean(ok_wall) if ok_wall else 0.0,
                    }
                )
    base = {r["workload"]: r for r in rows if r["policy"] == "critical path (default)"}
    for r in rows:
        b = base[r["workload"]]
        for key in ("failed_worker_s", "failed_wall_s", "ok_wall_s"):
            r[f"{key}_vs_default"] = r[key] / b[key] if b[key] else 1.0
    return rows
