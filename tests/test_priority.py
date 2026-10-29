"""Failure-aware priority and doomed-task pruning (miniflow.priority, Scheduler options)."""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from miniflow import DAG, ExecutorRouter, RetryPolicy, Scheduler, State, StateStore, task
from miniflow.cli import main
from miniflow.priority import (
    FailureModel,
    TaskHistory,
    doomed,
    expected_attempts,
    learn_history,
    static_keys,
)
from miniflow.states import TRANSITIONS
from tests.conftest import random_dags

FAST = RetryPolicy(base=0.01, cap=0.05, jitter=0.0)

calls: list[str] = []


@pytest.fixture(autouse=True)
def _clear() -> Any:
    calls.clear()
    yield
    calls.clear()


def noop(*_up: Any) -> None:
    return None


def mark(*_up: Any, name: str, seconds: float = 0.0) -> str:
    calls.append(name)
    time.sleep(seconds)
    return name


def boom(*_up: Any, name: str, seconds: float = 0.0) -> None:
    calls.append(name)
    time.sleep(seconds)
    raise RuntimeError(f"{name} failed")


_flaky_seen: dict[str, int] = {}


def fail_first(*_up: Any, name: str) -> str:
    """Fails on its first attempt only."""
    calls.append(name)
    _flaky_seen[name] = _flaky_seen.get(name, 0) + 1
    if _flaky_seen[name] == 1:
        raise RuntimeError("first attempt fails")
    return name


def dag_of(edges: dict[str, list[str]]) -> DAG:
    return DAG.from_tasks(task(noop, name=n, kind="io", after=ups) for n, ups in edges.items())


# ---------------------------------------------------------------- doomed()
def brute_doomed(edges: dict[str, list[str]], dead: set[str]) -> set[str]:
    """Not dead, not a sink, and no sink reachable through non-dead tasks."""
    down: dict[str, list[str]] = {n: [] for n in edges}
    for n, ups in edges.items():
        for u in ups:
            down[u].append(n)
    out = set()
    for n in edges:
        if n in dead or not down[n]:
            continue
        seen, queue, alive = {n}, deque([n]), False
        while queue and not alive:
            for c in down[queue.popleft()]:
                if c in dead or c in seen:
                    continue
                if not down[c]:
                    alive = True
                    break
                seen.add(c)
                queue.append(c)
        if not alive:
            out.add(n)
    return out


def test_doomed_join():
    edges = {"a": [], "risky": ["a"], "big": ["a"], "report": ["risky", "big"], "log": ["a"]}
    dag = dag_of(edges)
    assert doomed(dag, {"risky", "report"}) == {"big"}  # 'a' still feeds the 'log' sink


def test_doomed_never_includes_sinks_or_dead():
    edges = {"a": [], "b": ["a"], "c": ["a"]}
    dag = dag_of(edges)
    assert doomed(dag, {"b"}) == set()
    assert doomed(dag, {"b", "c"}) == {"a"}


@settings(max_examples=150, deadline=None)
@given(data=st.data())
def test_doomed_matches_brute_force(data: st.DataObject):
    edges = data.draw(random_dags(max_nodes=14))
    if not edges:
        return
    names = sorted(edges)
    dead = set(data.draw(st.lists(st.sampled_from(names), max_size=len(names))))
    dag = dag_of(edges)
    assert doomed(dag, dead) == brute_doomed(edges, dead)


# --------------------------------------------------------------- the model
@pytest.mark.parametrize("p", [0.0, 0.2, 0.5, 0.9])
@pytest.mark.parametrize("left", [1, 2, 4])
def test_expected_attempts_closed_form(p: float, left: int):
    series = sum(p**k for k in range(left))  # attempt k+1 happens if the first k failed
    assert expected_attempts(p, left) == pytest.approx(series)


def test_expected_attempts_edges():
    assert expected_attempts(1.0, 3) == 3
    assert expected_attempts(0.3, 0) == 0


def test_failure_model_posterior_and_defaults():
    hist = {
        "a": TaskHistory(attempts=10, failures=9, successes=1, success_seconds=2.0),
        "b": TaskHistory(attempts=4, failures=0, successes=4, success_seconds=4.0),
    }
    model = FailureModel(hist)
    assert model.attempt_failure("a") == pytest.approx(9.02 / 12)
    assert model.attempt_failure("unknown") == pytest.approx(0.01)
    assert model.default_seconds == pytest.approx(1.5)  # median of 2.0 and 1.0
    assert model.seconds("unknown") == pytest.approx(1.5)
    t = task(noop, name="a", kind="io", retries=1)
    assert model.permanent_failure(t) == pytest.approx((9.02 / 12) ** 2)
    assert model.permanent_failure(t, attempts_used=1) == pytest.approx(9.02 / 12)
    assert model.permanent_failure(t, attempts_used=2) == 1.0


def test_failure_model_rejects_bad_prior():
    with pytest.raises(ValueError, match="prior"):
        FailureModel(prior_failures=2, prior_attempts=1)


def join_flow(*, risky_fails: bool = True, big_seconds: float = 0.0) -> list[Any]:
    a = task(mark, name="a", kind="io", kwargs={"name": "a"})
    risky = task(
        boom if risky_fails else mark, name="risky", kind="io", after=[a], kwargs={"name": "risky"}
    )
    big = task(
        mark, name="big", kind="io", after=[a], kwargs={"name": "big", "seconds": big_seconds}
    )
    report = task(mark, name="report", kind="io", after=[risky, big], kwargs={"name": "report"})
    return [a, risky, big, report]


RISKY_HISTORY = {
    "risky": TaskHistory(attempts=10, failures=8, successes=2, success_seconds=0.2),
    "big": TaskHistory(attempts=10, failures=0, successes=10, success_seconds=10.0),
    "a": TaskHistory(attempts=10, failures=0, successes=10, success_seconds=1.0),
    "report": TaskHistory(attempts=2, failures=0, successes=2, success_seconds=0.2),
}


def test_keys_put_risky_cheap_task_first():
    dag = DAG.from_tasks(join_flow())
    model = FailureModel({k: TaskHistory(**vars(v)) for k, v in RISKY_HISTORY.items()})
    keys = static_keys(dag, model)
    plain = dag.critical_path_lengths(model.expected_cost)
    assert plain["big"] > plain["risky"]  # critical path alone runs 'big' first
    assert keys["risky"][0] == 1.0  # promoted: its likely failure would save 'big'
    assert keys["risky"] > keys["big"]
    assert keys["big"] == (0.0, plain["big"])


def test_no_failure_history_means_critical_path_order():
    dag = DAG.from_tasks(join_flow())
    model = FailureModel({"big": TaskHistory(attempts=3, successes=3, success_seconds=3.0)})
    plain = dag.critical_path_lengths(model.expected_cost)
    assert static_keys(dag, model) == {n: (0.0, plain[n]) for n in dag.tasks}


def test_rarely_failing_expensive_task_is_not_promoted():
    # Fails 1 time in 50 and costs 10x what its failure would save: not worth the delay.
    dag = DAG.from_tasks(join_flow())
    model = FailureModel(
        {
            "risky": TaskHistory(attempts=50, failures=1, successes=49, success_seconds=490.0),
            "big": TaskHistory(attempts=50, successes=50, success_seconds=50.0),
        }
    )
    assert static_keys(dag, model)["risky"][0] == 0.0


def test_optional_tasks_get_no_failure_bonus():
    a = task(noop, name="a", kind="io")
    risky = task(noop, name="risky", kind="io", after=[a], optional=True)
    big = task(noop, name="big", kind="io", after=[a])
    end = task(noop, name="end", kind="io", after=[risky, big])
    dag = DAG.from_tasks([a, risky, big, end])
    model = FailureModel({"risky": TaskHistory(attempts=10, failures=10)})
    keys = static_keys(dag, model)
    assert keys["risky"] == (0.0, dag.critical_path_lengths(model.expected_cost)["risky"])


def test_learn_history_reads_attempts_of_the_flow():
    store = StateStore()
    try:
        tasks = [
            task(fail_first, name="f", kind="io", retries=1, kwargs={"name": "f"}),
            task(mark, name="m", kind="io", kwargs={"name": "m"}),
        ]
        _flaky_seen.clear()
        Scheduler(DAG.from_tasks(tasks), store=store, flow="mine", policy=FAST).run()
        Scheduler(DAG.from_tasks(tasks[1:]), store=store, flow="other", policy=FAST).run()
        hist = learn_history(store, "mine")
        assert (hist["f"].attempts, hist["f"].failures, hist["f"].successes) == (2, 1, 1)
        assert hist["m"].attempts == 1
        assert learn_history(store, "mine", last_runs=0) == {}
    finally:
        store.close()


# ---------------------------------------------------------- scheduler runs
def run_flow(tasks: list[Any], workers: int = 1, **options: Any) -> Any:
    with ExecutorRouter(io_workers=workers) as router, StateStore() as store:
        result = Scheduler(
            DAG.from_tasks(tasks), router=router, store=store, policy=FAST, **options
        ).run()
        attempts = store.attempts(result.run_id)
        return result, attempts


def test_default_runs_doomed_sibling():
    result, _ = run_flow(join_flow())
    assert result.states["big"] is State.SUCCESS  # wasted: 'report' is skipped
    assert result.states["report"] is State.SKIPPED


def test_failure_aware_and_prune_never_run_the_doomed_sibling():
    result, attempts = run_flow(
        join_flow(), priority="failure_aware", prune=True, history=RISKY_HISTORY
    )
    assert "big" not in calls
    assert result.states["big"] is State.SKIPPED
    assert result.errors["big"].startswith("pruned:")
    assert result.status == "FAILED"
    assert {a.task for a in attempts} == {"a", "risky"}


def test_failure_aware_without_prune_only_reorders():
    result, _ = run_flow(join_flow(), priority="failure_aware", history=RISKY_HISTORY)
    assert calls.index("risky") < calls.index("big")
    assert result.states["big"] is State.SUCCESS


def test_failure_aware_success_is_unchanged():
    result, _ = run_flow(
        join_flow(risky_fails=False), priority="failure_aware", prune=True, history=RISKY_HISTORY
    )
    assert result.ok
    assert set(calls) == {"a", "risky", "big", "report"}


def test_prune_keeps_independent_sinks():
    a = task(mark, name="a", kind="io", kwargs={"name": "a"})
    bad = task(boom, name="bad", kind="io", after=[a], kwargs={"name": "bad"})
    feeder = task(mark, name="feeder", kind="io", after=[a], kwargs={"name": "feeder"})
    end = task(mark, name="end", kind="io", after=[bad, feeder], kwargs={"name": "end"})
    other = task(mark, name="other", kind="io", after=[a], kwargs={"name": "other"})
    result, _ = run_flow([a, bad, feeder, end, other], prune=True)
    assert result.states["other"] is State.SUCCESS
    assert result.states["end"] is State.SKIPPED


def test_prune_skips_a_task_waiting_to_retry():
    _flaky_seen.clear()
    a = task(mark, name="a", kind="io", kwargs={"name": "a"})
    slow_bad = task(
        boom, name="slow_bad", kind="io", after=[a], kwargs={"name": "slow_bad", "seconds": 0.2}
    )
    flaky = task(
        fail_first, name="flaky", kind="io", after=[a], retries=1, kwargs={"name": "flaky"}
    )
    end = task(mark, name="end", kind="io", after=[slow_bad, flaky], kwargs={"name": "end"})
    with ExecutorRouter(io_workers=2) as router, StateStore() as store:
        result = Scheduler(
            DAG.from_tasks([a, slow_bad, flaky, end]),
            router=router,
            store=store,
            policy=RetryPolicy(base=2.0, cap=2.0, jitter=0.0),  # flaky waits 2 s to retry
            prune=True,
        ).run()
    assert result.states["flaky"] is State.SKIPPED
    assert result.attempts["flaky"] == 1
    assert result.duration < 1.5  # didn't wait for the backoff


def test_pruned_tasks_rerun_on_resume():
    with ExecutorRouter(io_workers=1) as router, StateStore() as store:
        first = Scheduler(
            DAG.from_tasks(join_flow()),
            router=router,
            store=store,
            policy=FAST,
            priority="failure_aware",
            prune=True,
            history=RISKY_HISTORY,
        ).run()
        assert first.states["big"] is State.SKIPPED
        calls.clear()
        second = Scheduler(
            DAG.from_tasks(join_flow(risky_fails=False)), router=router, store=store, policy=FAST
        ).run(resume_from=first.run_id)
    assert second.ok
    assert "a" not in calls
    assert {"risky", "big", "report"} <= set(calls)


def test_history_is_learned_from_earlier_runs():
    with ExecutorRouter(io_workers=1) as router, StateStore() as store:
        for _ in range(3):  # build up failures of 'risky'
            Scheduler(
                DAG.from_tasks(join_flow()), router=router, store=store, policy=FAST, flow="j"
            ).run()
        calls.clear()
        result = Scheduler(
            DAG.from_tasks(join_flow()),
            router=router,
            store=store,
            policy=FAST,
            flow="j",
            priority="failure_aware",
            prune=True,
        ).run()
    assert "big" not in calls
    assert result.states["big"] is State.SKIPPED


def test_every_transition_is_legal_under_pruning():
    events: list[Any] = []
    with ExecutorRouter(io_workers=2) as router, StateStore() as store:
        Scheduler(
            DAG.from_tasks(join_flow(big_seconds=0.05)),
            router=router,
            store=store,
            policy=FAST,
            priority="failure_aware",
            prune=True,
            history=RISKY_HISTORY,
            listeners=[events.append],
        ).run()
    seen: dict[str, State] = {}
    for e in events:
        if e.task in seen:
            assert e.state in TRANSITIONS[seen[e.task]]
        seen[e.task] = e.state


def test_bad_priority_is_rejected():
    with pytest.raises(ValueError, match="priority"):
        Scheduler(DAG.from_tasks(join_flow()), priority="fastest")  # type: ignore[arg-type]


def test_cli_flags(tmp_path: Path):
    flow = tmp_path / "join.py"
    flow.write_text(
        dedent(
            """
            from miniflow import task

            @task
            def a():
                return 1

            @task(after=[a])
            def risky(x):
                raise RuntimeError("no")

            @task(after=[a])
            def big(x):
                return x

            @task(after=[risky, big])
            def report(r, b):
                return r, b
            """
        ),
        encoding="utf-8",
    )
    db = tmp_path / "m.db"
    args = ["run", str(flow), "--db", str(db), "--quiet", "--io-workers", "1"]
    assert main([*args, "--priority", "failure-aware", "--prune"]) == 1
    with StateStore(db) as store:
        run_id = store.latest_run()
        assert run_id is not None
        states = store.get_states(run_id)
    assert states["report"] is State.SKIPPED
