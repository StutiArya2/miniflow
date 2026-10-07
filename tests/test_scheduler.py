from __future__ import annotations

import time
from collections import defaultdict

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from miniflow import (
    DAG,
    Event,
    ExecutorRouter,
    RetryPolicy,
    Scheduler,
    State,
    StateStore,
    run,
    task,
)
from miniflow.states import TRANSITIONS
from tests import _sched_flows as F
from tests.conftest import random_dags

FAST = RetryPolicy(base=0.01, cap=0.05, jitter=0.0)


_stores: list[StateStore] = []


def new_store(*args: object) -> StateStore:
    store = StateStore(*args)  # type: ignore[arg-type]
    _stores.append(store)
    return store


@pytest.fixture(autouse=True)
def _reset_counters():
    F.calls.clear()
    yield
    F.calls.clear()
    while _stores:
        _stores.pop().close()


@pytest.fixture
def router():
    r = ExecutorRouter(io_workers=8, cpu_workers=2)
    yield r
    r.shutdown(wait=False, cancel_futures=True)


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, e: Event) -> None:
        self.events.append(e)

    def states(self, name: str) -> list[State]:
        return [e.state for e in self.events if e.task == name]


def assert_legal_transitions(rec: Recorder) -> None:
    seen: dict[str, State] = defaultdict(lambda: State.PENDING)
    for e in rec.events:
        if not e.from_cache:
            assert e.state in TRANSITIONS[seen[e.task]], f"{e.task}: {seen[e.task]} -> {e.state}"
        seen[e.task] = e.state


def assert_dependency_order(store: StateStore, run_id: str, dag: DAG) -> None:
    """Spec §3.8 correctness check: no attempt starts before its upstream finished."""
    attempts = store.attempts(run_id)
    finished = {a.task: a.finished_at for a in attempts if a.state is State.SUCCESS}
    for a in attempts:
        for up in dag.upstream[a.task]:
            assert finished[up] <= a.started_at, f"{a.task} started before {up} finished"


# ------------------------------------------------------------------ basics
def test_results_flow_downstream(router):
    tasks = [
        task(F.const, name="a", args=(1,)),
        task(F.add, name="b", after=["a"], args=(10,)),
        task(F.add, name="c", after=["a"], args=(100,)),
        task(F.add, name="d", after=["b", "c"]),
    ]
    result = run(tasks, router=router)
    assert result.ok
    assert result["b"] == 11
    assert result["c"] == 101
    assert result["d"] == 11 + 101
    assert set(result.states.values()) == {State.SUCCESS}


def test_mixed_worker_kinds(router):
    store = new_store()
    tasks = [
        task(F.const, name="number", args=(7,)),
        F.cpu_square_of,  # cpu tasks must be module-level so workers can import them
        task(F.async_add, name="aio", kind="async", after=["cpu_square_of"], args=(1,)),
    ]
    result = Scheduler(DAG.from_tasks(tasks), router=router, store=store).run()
    assert result["aio"] == 50
    workers = {a.task: a.worker or "" for a in store.attempts(result.run_id)}
    assert workers["number"].startswith("thread-")
    assert workers["cpu_square_of"].startswith("pid-")
    assert workers["aio"] == "loop"


def test_cpu_task_runs_in_worker_process(router):
    import os

    result = run([F.cpu_pid], router=router)
    assert result.ok
    assert result["cpu_pid"] != os.getpid()


def test_empty_dag():
    result = run([])
    assert result.ok
    assert result.states == {}


def test_store_and_events_agree(router):
    rec = Recorder()
    store = new_store()
    tasks = [task(F.const, name="a", args=(1,)), task(F.add, name="b", after=["a"])]
    result = Scheduler(DAG.from_tasks(tasks), router=router, store=store, listeners=[rec]).run()
    assert rec.states("a") == [State.READY, State.RUNNING, State.SUCCESS]
    assert store.get_states(result.run_id) == {"a": State.SUCCESS, "b": State.SUCCESS}
    assert store.list_runs()[0].status == "SUCCESS"
    assert store.load_completed(result.run_id) == {"a": 1, "b": 1}
    workers = {a.task: a.worker for a in store.attempts(result.run_id)}
    assert workers["a"].startswith("thread-")
    assert_legal_transitions(rec)


def test_listener_errors_do_not_break_the_run(router, caplog):
    def broken(_e: Event) -> None:
        raise RuntimeError("listener bug")

    result = run([task(F.const, name="a", args=(1,))], router=router, listeners=[broken])
    assert result.ok
    assert "listener" in caplog.text


# ------------------------------------------------------------------ retries
def test_flaky_task_succeeds_after_retries(router):
    rec = Recorder()
    store = new_store()
    t = task(F.flaky, name="f", retries=2, args=("f", 2))
    result = run([t], router=router, store=store, policy=FAST, listeners=[rec])
    assert result.ok
    assert result.attempts["f"] == 3
    assert rec.states("f").count(State.RETRYING) == 2
    attempts = store.attempts(result.run_id, "f")
    assert [a.state for a in attempts] == [State.FAILED, State.FAILED, State.SUCCESS]
    assert "ConnectionError" in (attempts[0].error or "")
    assert_legal_transitions(rec)


def test_backoff_delay_is_waited(router):
    policy = RetryPolicy(base=0.1, cap=1.0, jitter=0.0)
    t = task(F.flaky, name="f", retries=2, args=("f", 2))
    start = time.monotonic()
    result = run([t], router=router, policy=policy)
    assert result.ok
    assert time.monotonic() - start >= 0.1 + 0.2 - 0.02


def test_exhausted_retries_fail_and_skip_descendants(router):
    rec = Recorder()
    tasks = [
        task(F.const, name="root", args=(1,)),
        task(F.always_fail, name="bad", after=["root"], retries=1, kwargs={"message": "nope"}),
        task(F.ignore, name="child", after=["bad"], kwargs={"value": 2}),
        task(F.ignore, name="grandchild", after=["child"], kwargs={"value": 3}),
        task(F.ignore, name="sibling", after=["root"], kwargs={"value": 4}),
    ]
    result = run(tasks, router=router, policy=FAST, listeners=[rec])
    assert result.status == "FAILED"
    assert not result.ok
    assert result.states == {
        "root": State.SUCCESS,
        "bad": State.FAILED,
        "child": State.SKIPPED,
        "grandchild": State.SKIPPED,
        "sibling": State.SUCCESS,
    }
    assert result.attempts["bad"] == 2
    assert result.errors["bad"] == "ValueError: nope"
    assert "bad" in result.errors["child"]
    assert result.attempts["child"] == 0
    assert_legal_transitions(rec)


def test_async_failure_and_retry(router):
    calls = []

    async def sometimes() -> str:
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("first try fails")
        return "done"

    result = run([task(sometimes, kind="async", retries=1)], router=router, policy=FAST)
    assert result.ok
    assert result["sometimes"] == "done"


# ------------------------------------------------------------------ timeouts
def test_io_timeout_fails_fast(router):
    store = new_store()
    tasks = [
        task(F.sleep_then, name="slow", timeout=0.2, args=(3.0,)),
        task(F.ignore, name="after", after=["slow"]),
    ]
    start = time.monotonic()
    result = Scheduler(DAG.from_tasks(tasks), router=router, store=store).run()
    assert time.monotonic() - start < 2.0
    assert result.states["slow"] is State.FAILED
    assert result.states["after"] is State.SKIPPED
    assert result.errors["slow"].startswith("TaskTimeoutError")
    assert "TaskTimeoutError" in (store.attempts(result.run_id, "slow")[0].error or "")


def test_async_timeout_with_retry(router):
    t = task(F.async_sleep_then, name="slow", kind="async", timeout=0.1, retries=1, args=(5.0,))
    start = time.monotonic()
    result = run([t], router=router, policy=FAST)
    assert time.monotonic() - start < 2.0
    assert result.states["slow"] is State.FAILED
    assert result.attempts["slow"] == 2


def test_fast_task_with_timeout_succeeds(router):
    result = run([task(F.sleep_then, name="ok", timeout=5, args=(0.01, "v"))], router=router)
    assert result["ok"] == "v"


# ------------------------------------------------------------- worker crash
def test_crashed_process_restarts_pool_and_retries(router, tmp_path):
    marker = tmp_path / "crashed"
    tasks = [task(F.const, name="marker", args=(str(marker),)), F.cpu_crash_once]
    result = run(tasks, router=router, policy=FAST)
    assert marker.exists()
    assert result.states["cpu_crash_once"] is State.SUCCESS
    assert result["cpu_crash_once"] == "survived"
    assert result.attempts["cpu_crash_once"] == 2
    # The restarted pool keeps serving cpu tasks.
    later = run([task(F.const, name="number", args=(6,)), F.cpu_square_of], router=router)
    assert later["cpu_square_of"] == 36


# ---------------------------------------------------------- priority & limits
def test_critical_path_runs_first():
    rec = Recorder()
    # "lonely" is defined first, but "head" gates a chain of three tasks.
    tasks = [
        task(F.const, name="lonely", args=(0,)),
        task(F.const, name="head", args=(0,)),
        task(F.ignore, name="mid", after=["head"]),
        task(F.ignore, name="tail", after=["mid"]),
    ]
    with ExecutorRouter(io_workers=1) as single:
        run(tasks, router=single, listeners=[rec])
    starts = [e.task for e in rec.events if e.state is State.RUNNING]
    assert starts[0] == "head"


def test_io_capacity_is_respected():
    store = new_store()
    tasks = [task(F.sleep_then, name=f"t{i}", args=(0.15,)) for i in range(6)]
    with ExecutorRouter(io_workers=2) as two:
        result = run(tasks, router=two, store=store)
    spans = [(a.started_at, a.finished_at) for a in store.attempts(result.run_id)]
    peak = max(sum(s <= t < f for s, f in spans) for t, _ in spans)
    assert peak <= 2


def test_parallel_io_beats_sequential(router):
    tasks = [task(F.sleep_then, name=f"t{i}", args=(0.3,)) for i in range(8)]
    start = time.monotonic()
    result = run(tasks, router=router)
    assert result.ok
    assert time.monotonic() - start < 8 * 0.3 / 2


# ------------------------------------------------------------------- resume
def test_resume_reuses_successful_results(router, tmp_path):
    flag = tmp_path / "fixed"
    store = new_store(tmp_path / "state.db")
    tasks = [
        task(F.counted, name="a", kwargs={"key": "a", "value": 1}),
        task(F.fail_unless_file, name="b", after=["a"], kwargs={"path": str(flag)}),
        task(F.counted, name="c", after=["b"], kwargs={"key": "c"}),
        task(F.counted, name="side", kwargs={"key": "side", "value": 5}),
    ]
    dag = DAG.from_tasks(tasks)
    first = Scheduler(dag, router=router, store=store, flow="f").run()
    assert first.status == "FAILED"
    assert F.calls == {"a": 1, "side": 1}

    flag.write_text("ok")
    rec = Recorder()
    second = Scheduler(dag, router=router, store=store, flow="f", listeners=[rec]).run(
        resume_from=first.run_id
    )
    assert second.ok
    assert second.run_id != first.run_id
    assert second.from_cache == {"a", "side"}
    assert F.calls == {"a": 1, "side": 1, "c": 1}  # a and side were not re-executed
    assert second["b"] == "fixed"
    states = {r.task: r for r in store.changes_since(second.run_id, 0)}
    assert states["a"].from_cache
    assert not states["b"].from_cache
    assert_legal_transitions(rec)


def test_resume_of_complete_run_executes_nothing(router):
    store = new_store()
    tasks = [
        task(F.counted, name="a", kwargs={"key": "a", "value": 1}),
        task(F.counted, name="b", after=["a"], kwargs={"key": "b"}),
    ]
    dag = DAG.from_tasks(tasks)
    first = Scheduler(dag, router=router, store=store).run()
    again = Scheduler(dag, router=router, store=store).run(resume_from=first.run_id)
    assert again.ok
    assert again.from_cache == {"a", "b"}
    assert F.calls == {"a": 1, "b": 1}


def test_unpicklable_result_still_flows_but_reruns_on_resume(router):
    store = new_store()
    tasks = [task(F.unpicklable, name="lock"), task(F.holds_lock, name="check", after=["lock"])]
    dag = DAG.from_tasks(tasks)
    first = Scheduler(dag, router=router, store=store).run()
    assert first.ok
    assert first["check"] is True
    again = Scheduler(dag, router=router, store=store).run(resume_from=first.run_id)
    assert again.from_cache == set()  # lock was not stored, so check can't be reused either


def test_interrupt_marks_run_and_can_resume(router):
    store = new_store()
    tasks = [
        task(F.counted, name="done", kwargs={"key": "done", "value": 1}),
        task(F.interrupt, name="ctrl_c", after=["done"]),
    ]
    dag = DAG.from_tasks(tasks)
    result = Scheduler(dag, router=router, store=store).run()
    assert result.status == "INTERRUPTED"
    assert store.list_runs()[0].status == "INTERRUPTED"
    assert result.states["done"] is State.SUCCESS
    assert store.load_completed(result.run_id) == {"done": 1}


# --------------------------------------------------------- property-based
@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(random_dags(max_nodes=12), st.data())
def test_random_dags_respect_dependencies(edges, data):
    kinds = data.draw(
        st.lists(st.sampled_from(["io", "async"]), min_size=len(edges), max_size=len(edges))
    )
    tasks = []
    for (name, ups), kind in zip(edges.items(), kinds, strict=True):
        fn = F.async_sleep_then if kind == "async" else F.sleep_then
        # Results are ignored; upstream values arrive as extra positional args.
        tasks.append(task(_ignore_args(fn, kind), name=name, kind=kind, after=ups))
    rec = Recorder()
    store = new_store()
    dag = DAG.from_tasks(tasks)
    with ExecutorRouter(io_workers=4) as r:
        result = Scheduler(dag, router=r, store=store, listeners=[rec]).run()
    assert result.ok
    assert_dependency_order(store, result.run_id, dag)
    assert_legal_transitions(rec)


def _ignore_args(fn, kind):
    if kind == "async":

        async def wrapper(*_args: object) -> None:
            await fn(0.002)

    else:

        def wrapper(*_args: object) -> None:
            fn(0.002)

    return wrapper
