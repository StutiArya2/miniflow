from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from miniflow import DAG, State, task
from miniflow.store import AttemptRecord, RunRecord, StateStore, TaskRecord
from tests.conftest import make_tasks, noop


async def anoop() -> None:
    return None


@pytest.fixture
def store() -> Iterator[StateStore]:
    with StateStore() as s:
        yield s


@pytest.fixture
def dag() -> DAG:
    return DAG.from_tasks(make_tasks({"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]}))


def test_create_run_marks_all_tasks_pending(store: StateStore, dag: DAG) -> None:
    run_id = store.create_run("flow.py", dag)
    assert isinstance(run_id, str)
    assert run_id
    assert store.get_states(run_id) == dict.fromkeys(["a", "b", "c", "d"], State.PENDING)
    [run] = store.list_runs()
    assert run == RunRecord(run_id, "flow.py", run.started_at, None, "RUNNING")
    assert run.started_at == pytest.approx(time.time(), abs=60)


def test_run_ids_are_unique(store: StateStore, dag: DAG) -> None:
    ids = {store.create_run("f", dag) for _ in range(50)}
    assert len(ids) == 50


def test_graph_json_from_dag(store: StateStore) -> None:
    tasks = [
        task(noop, name="load"),
        task(noop, name="crunch", kind="cpu", after=["load"]),
        task(anoop, name="publish", kind="async", after=["crunch", "load"]),
    ]
    run_id = store.create_run("f", DAG.from_tasks(tasks))
    graph = store.get_graph(run_id)
    assert graph == {
        "tasks": {
            "load": {"kind": "io", "after": [], "description": "", "optional": False},
            "crunch": {"kind": "cpu", "after": ["load"], "description": "", "optional": False},
            "publish": {
                "kind": "async",
                "after": ["crunch", "load"],
                "description": "",
                "optional": False,
            },
        },
        "about": {},
    }
    # Stored as plain JSON so other readers (the dashboard) can parse it directly.
    raw = store._conn.execute("SELECT graph FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0]
    assert json.loads(raw) == graph


def test_get_graph_unknown_run(store: StateStore) -> None:
    with pytest.raises(KeyError):
        store.get_graph("nope")


def test_set_state_transitions_and_timestamps(store: StateStore, dag: DAG) -> None:
    run_id = store.create_run("f", dag)
    store.set_state(run_id, "a", State.READY)
    store.set_state(run_id, "a", State.RUNNING)
    [rec] = [r for r in store.changes_since(run_id, 0) if r.task == "a"]
    assert rec.state is State.RUNNING
    assert rec.started_at is not None
    assert rec.finished_at is None
    first_start = rec.started_at

    store.set_state(run_id, "a", State.RETRYING, error="boom")
    store.set_state(run_id, "a", State.READY)
    store.set_state(run_id, "a", State.RUNNING)
    store.set_state(run_id, "a", State.FAILED)
    [rec] = [r for r in store.changes_since(run_id, 0) if r.task == "a"]
    assert rec.state is State.FAILED
    assert rec.started_at == first_start  # first RUNNING wins
    assert rec.finished_at is not None
    assert rec.error == "boom"  # kept when no new error is given
    assert store.get_states(run_id)["a"] is State.FAILED

    store.set_state(run_id, "b", State.SKIPPED, error="upstream a failed")
    assert store.get_states(run_id)["b"] is State.SKIPPED


def test_set_state_unknown_task(store: StateStore, dag: DAG) -> None:
    run_id = store.create_run("f", dag)
    with pytest.raises(KeyError):
        store.set_state(run_id, "zzz", State.READY)
    with pytest.raises(KeyError):
        store.set_state("no-run", "a", State.READY)


def test_attempts_log_with_workers(store: StateStore, dag: DAG) -> None:
    run_id = store.create_run("f", dag)
    store.record_attempt(run_id, "a", 1, State.FAILED, "err1", 10.0, 11.0, worker="thread-1")
    store.record_attempt(run_id, "a", 2, State.SUCCESS, None, 12.0, 13.0, worker="thread-2")
    store.record_attempt(run_id, "b", 1, State.SUCCESS, None, 11.5, 14.0, worker="pid-1234")
    store.record_attempt(run_id, "c", 1, State.SUCCESS, None, 11.6, 12.0)

    log = store.attempts(run_id)
    assert [(a.task, a.attempt) for a in log] == [("a", 1), ("b", 1), ("c", 1), ("a", 2)]
    assert log[0] == AttemptRecord(run_id, "a", 1, State.FAILED, "err1", "thread-1", 10.0, 11.0)
    assert log[1].worker == "pid-1234"
    assert log[2].worker is None

    a_log = store.attempts(run_id, "a")
    assert [a.worker for a in a_log] == ["thread-1", "thread-2"]
    assert all(isinstance(a.state, State) for a in a_log)
    [rec] = [r for r in store.changes_since(run_id, 0) if r.task == "a"]
    assert rec.attempts == 2

    # Re-recording an attempt replaces it rather than duplicating it.
    store.record_attempt(run_id, "c", 1, State.FAILED, "late", 11.6, 12.5, worker="loop")
    [c1] = store.attempts(run_id, "c")
    assert (c1.state, c1.error, c1.worker) == (State.FAILED, "late", "loop")

    with pytest.raises(KeyError):
        store.record_attempt(run_id, "zzz", 1, State.SUCCESS, None, 0.0, 1.0)


def test_save_and_load_completed_round_trip(store: StateStore, dag: DAG) -> None:
    run_id = store.create_run("f", dag)
    value = {"rows": [1, 2, 3], "nested": (1.5, None, b"\x00"), "s": {"x"}}
    store.save_result(run_id, "a", value)
    store.save_result(run_id, "b", None)
    store.set_state(run_id, "c", State.FAILED, error="nope")

    loaded = store.load_completed(run_id)
    assert loaded == {"a": value, "b": None}
    states = store.get_states(run_id)
    assert states["a"] is State.SUCCESS
    assert states["c"] is State.FAILED

    recs = {r.task: r for r in store.changes_since(run_id, 0)}
    assert recs["a"].has_result
    assert not recs["a"].from_cache
    assert recs["a"].finished_at is not None
    assert not recs["c"].has_result

    store.save_result(run_id, "c", 42, from_cache=True)
    recs = {r.task: r for r in store.changes_since(run_id, 0)}
    assert recs["c"].from_cache
    assert recs["c"].error is None
    assert store.load_completed(run_id)["c"] == 42


def test_save_result_unpicklable_leaves_state(store: StateStore, dag: DAG) -> None:
    run_id = store.create_run("f", dag)
    with pytest.raises(TypeError):
        store.save_result(run_id, "a", threading.Lock())
    assert store.get_states(run_id)["a"] is State.PENDING


@settings(max_examples=50, deadline=None)
@given(
    value=st.recursive(
        st.none() | st.booleans() | st.integers() | st.text() | st.binary(),
        lambda kids: st.lists(kids) | st.dictionaries(st.text(), kids),
        max_leaves=20,
    )
)
def test_round_trip_property(value: object) -> None:
    with StateStore() as s:
        run_id = s.create_run("f", DAG.from_tasks(make_tasks({"t": []})))
        s.save_result(run_id, "t", value)
        assert s.load_completed(run_id) == {"t": value}


def test_changes_since(store: StateStore, dag: DAG) -> None:
    run_id = store.create_run("f", dag)
    other = store.create_run("g", dag)
    initial = store.changes_since(run_id, 0)
    assert {r.task for r in initial} == {"a", "b", "c", "d"}
    assert all(isinstance(r, TaskRecord) for r in initial)
    cursor = max(r.updated_at for r in initial)
    assert store.changes_since(run_id, cursor) == []

    store.set_state(run_id, "a", State.READY)
    store.set_state(run_id, "a", State.RUNNING)
    store.set_state(other, "a", State.READY)  # other runs don't leak in
    store.save_result(run_id, "c", 1)
    changed = store.changes_since(run_id, cursor)
    assert [r.task for r in changed] == ["a", "c"]
    assert changed[0].state is State.RUNNING

    cursor = max(r.updated_at for r in changed)
    store.record_attempt(run_id, "b", 1, State.SUCCESS, None, 1.0, 2.0, worker="thread-0")
    [rec] = store.changes_since(run_id, cursor)
    assert rec.task == "b"
    assert rec.attempts == 1


def test_updated_at_strictly_increases(store: StateStore, dag: DAG) -> None:
    run_id = store.create_run("f", dag)
    stamps = []
    for _ in range(200):
        store.set_state(run_id, "a", State.READY)
        [rec] = [r for r in store.changes_since(run_id, 0) if r.task == "a"]
        stamps.append(rec.updated_at)
    assert all(b > a for a, b in pairwise(stamps))


def test_latest_run_and_list_runs(store: StateStore, dag: DAG) -> None:
    assert store.latest_run() is None
    assert store.list_runs() == []
    r1 = store.create_run("flow_a.py", dag)
    r2 = store.create_run("flow_b.py", dag)
    r3 = store.create_run("flow_a.py", dag)
    assert store.latest_run() == r3
    assert store.latest_run("flow_a.py") == r3
    assert store.latest_run("flow_b.py") == r2
    assert store.latest_run("missing.py") is None
    assert [r.run_id for r in store.list_runs()] == [r3, r2, r1]

    store.finish_run(r1, "SUCCESS")
    run = {r.run_id: r for r in store.list_runs()}[r1]
    assert run.status == "SUCCESS"
    assert run.finished_at is not None
    assert run.finished_at >= run.started_at
    with pytest.raises(KeyError):
        store.finish_run("nope", "FAILED")


def test_reopen_file_database_for_resume(tmp_path: Path, dag: DAG) -> None:
    db = tmp_path / "runs.db"
    with StateStore(db) as s:
        run_id = s.create_run("flow.py", dag)
        s.save_result(run_id, "a", {"answer": 42})
        s.set_state(run_id, "b", State.READY)
        s.set_state(run_id, "b", State.RUNNING)
        s.record_attempt(run_id, "a", 1, State.SUCCESS, None, 1.0, 2.0, worker="pid-7")
        # "crash": no finish_run

    with StateStore(str(db)) as s:
        assert s.latest_run("flow.py") == run_id
        assert s.load_completed(run_id) == {"a": {"answer": 42}}
        assert s.get_states(run_id)["b"] is State.RUNNING
        assert s.get_graph(run_id)["tasks"]["d"]["after"] == ["b", "c"]
        assert [a.worker for a in s.attempts(run_id)] == ["pid-7"]
        [run] = s.list_runs()
        assert run.finished_at is None
        # Resume: a fresh run that reuses the cached result.
        resumed = s.create_run("flow.py", dag)
        s.save_result(resumed, "a", s.load_completed(run_id)["a"], from_cache=True)
        assert s.latest_run() == resumed
        assert {r.task: r.from_cache for r in s.changes_since(resumed, 0)}["a"]


def test_file_database_uses_wal(tmp_path: Path) -> None:
    db = tmp_path / "wal.db"
    with StateStore(db) as s:
        mode = s._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


def test_second_connection_can_read_while_open(tmp_path: Path, dag: DAG) -> None:
    db = tmp_path / "shared.db"
    with StateStore(db) as writer:
        run_id = writer.create_run("f", dag)
        writer.set_state(run_id, "a", State.READY)
        with StateStore(db) as reader:
            assert reader.get_states(run_id)["a"] is State.READY
            writer.save_result(run_id, "a", "x")
            assert reader.load_completed(run_id) == {"a": "x"}


def test_concurrent_writes_from_8_threads(tmp_path: Path) -> None:
    n_threads, per_thread = 8, 25
    edges: dict[str, list[str]] = {
        f"t{i}_{j}": [] for i in range(n_threads) for j in range(per_thread)
    }
    dag = DAG.from_tasks(make_tasks(edges))
    with StateStore(tmp_path / "concurrent.db") as s:
        run_id = s.create_run("f", dag)
        barrier = threading.Barrier(n_threads)
        errors: list[BaseException] = []

        def worker(i: int) -> None:
            try:
                barrier.wait()
                for j in range(per_thread):
                    name = f"t{i}_{j}"
                    s.set_state(run_id, name, State.READY)
                    s.set_state(run_id, name, State.RUNNING)
                    s.record_attempt(
                        run_id, name, 1, State.SUCCESS, None, 0.0, 1.0, worker=f"thread-{i}"
                    )
                    s.save_result(run_id, name, (i, j))
                    s.get_states(run_id)  # interleave reads with writes
            except BaseException as e:  # pragma: no cover - reported below
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        results = s.load_completed(run_id)
        assert results == {f"t{i}_{j}": (i, j) for i in range(n_threads) for j in range(per_thread)}
        assert len(s.attempts(run_id)) == n_threads * per_thread
        stamps = [r.updated_at for r in s.changes_since(run_id, 0)]
        assert len(set(stamps)) == len(stamps)


def test_memory_databases_are_independent(dag: DAG) -> None:
    with StateStore(":memory:") as a, StateStore() as b:
        run_id = a.create_run("f", dag)
        assert a.latest_run() == run_id
        assert b.latest_run() is None
        assert a.path == ":memory:"


def test_close_via_context_manager(dag: DAG) -> None:
    with StateStore() as s:
        s.create_run("f", dag)
    with pytest.raises(sqlite3.ProgrammingError):
        s.list_runs()
