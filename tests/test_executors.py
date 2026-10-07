import multiprocessing
import os
import pickle
import threading
import time
from concurrent.futures import CancelledError, wait
from concurrent.futures.process import BrokenProcessPool

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from miniflow.executors import ExecutorRouter, Outcome, TaskError
from miniflow.task import Task
from tests import _flows

SPAWN = multiprocessing.get_context("spawn")


@pytest.fixture(scope="module")
def router():
    with ExecutorRouter(io_workers=4, cpu_workers=2, mp_context=SPAWN) as r:
        yield r


def _io_task(fn, name: str = "t") -> Task:
    return Task(fn=fn, name=name, kind="io")


# -- results from all three kinds ---------------------------------------------


def test_io_result(router):
    outcome = router.submit(_flows.io_double, (21,)).result(timeout=10)
    assert isinstance(outcome, Outcome)
    assert outcome.value == 42
    assert outcome.worker.startswith("thread-miniflow-io")


def test_io_worker_names_the_thread(router):
    outcome = router.submit(_flows.io_thread_name).result(timeout=10)
    assert outcome.worker == f"thread-{outcome.value}"


def test_cpu_result_with_kwargs(router):
    outcome = router.submit(_flows.add, (2, 3), {"scale": 10}).result(timeout=30)
    assert outcome.value == 50
    assert outcome.worker.startswith("pid-")


def test_async_result(router):
    outcome = router.submit(_flows.async_echo, ("hi",)).result(timeout=10)
    assert outcome == Outcome("hi", "loop", outcome.started_at, outcome.finished_at)


@pytest.mark.parametrize(
    ("t", "args"),
    [(_flows.io_sleep, (0.05,)), (_flows.cpu_sleep, (0.05,)), (_flows.async_echo, (1, 0.05))],
)
def test_timing_brackets_the_call(router, t, args):
    before = time.time()
    outcome = router.submit(t, args).result(timeout=30)
    after = time.time()
    assert before <= outcome.started_at <= outcome.finished_at <= after
    assert outcome.duration >= 0.04


# -- cpu tasks run in other processes -----------------------------------------


def test_cpu_runs_in_another_pid(router):
    outcome = router.submit(_flows.getpid).result(timeout=30)
    assert outcome.value != os.getpid()
    assert outcome.worker == f"pid-{outcome.value}"


@pytest.mark.filterwarnings("ignore:.*fork\\(\\) may lead to deadlocks:DeprecationWarning")
def test_cpu_task_with_default_start_method():
    with ExecutorRouter(cpu_workers=1) as r:
        assert r.submit(_flows.square, (7,)).result(timeout=30).value == 49


# -- exceptions ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("t", "original_type", "worker_prefix"),
    [
        (_flows.io_fail, KeyError, "thread-"),
        (_flows.cpu_fail, ValueError, "pid-"),
        (_flows.async_fail, RuntimeError, "loop"),
    ],
)
def test_exception_propagates_as_task_error(router, t, original_type, worker_prefix):
    future = router.submit(t, ("bad",))
    with pytest.raises(TaskError) as info:
        future.result(timeout=30)
    err = info.value
    assert isinstance(err.original, original_type)
    assert "bad" in str(err.original)
    assert err.__cause__ is err.original
    assert err.worker.startswith(worker_prefix)
    assert err.started_at <= err.finished_at
    assert original_type.__name__ in str(err)


def test_task_error_pickles():
    err = TaskError(ValueError("x"), "pid-1", 1.0, 2.0)
    clone = pickle.loads(pickle.dumps(err))
    assert isinstance(clone.original, ValueError)
    assert clone.__cause__ is clone.original
    assert (clone.worker, clone.started_at, clone.finished_at) == ("pid-1", 1.0, 2.0)


def test_unpicklable_cpu_exception_still_reported(router):
    with pytest.raises(TaskError, match="Unpicklable") as info:
        router.submit(_flows.cpu_fail_unpicklable).result(timeout=30)
    assert isinstance(info.value.original, RuntimeError)


def test_crashed_worker_is_not_wrapped():
    with ExecutorRouter(cpu_workers=1, mp_context=SPAWN) as r, pytest.raises(BrokenProcessPool):
        r.submit(_flows.cpu_crash).result(timeout=30)


# -- concurrency --------------------------------------------------------------


def test_many_concurrent_io_tasks():
    with ExecutorRouter(io_workers=16) as r:
        start = time.monotonic()
        futures = [r.submit(_flows.io_sleep, (0.2, i)) for i in range(32)]
        values = [f.result(timeout=30).value for f in futures]
        elapsed = time.monotonic() - start
    assert values == list(range(32))
    assert elapsed < 0.2 * 32 / 2  # well below serial time
    assert len({f.result().worker for f in futures}) > 1


def test_many_concurrent_async_tasks(router):
    start = time.monotonic()
    futures = [router.submit(_flows.async_echo, (i, 0.2)) for i in range(200)]
    done, not_done = wait(futures, timeout=30)
    elapsed = time.monotonic() - start
    assert not not_done
    assert sorted(f.result().value for f in done) == list(range(200))
    assert elapsed < 5


def test_mixed_kinds_can_be_waited_together(router):
    futures = [
        router.submit(_flows.io_double, (1,)),
        router.submit(_flows.square, (3,)),
        router.submit(_flows.async_echo, ("a",)),
    ]
    _, not_done = wait(futures, timeout=30)
    assert not not_done
    assert [f.result().value for f in futures] == [2, 9, "a"]


@settings(max_examples=25, deadline=None)
@given(xs=st.lists(st.integers(), max_size=20))
def test_io_results_match_direct_calls(xs):
    with ExecutorRouter(io_workers=4) as r:
        outcomes = [r.submit(_flows.io_double, (x,)).result(timeout=10) for x in xs]
    assert [o.value for o in outcomes] == [_flows.io_double(x) for x in xs]
    assert all(o.started_at <= o.finished_at for o in outcomes)


# -- lifecycle ----------------------------------------------------------------


def test_pools_are_created_lazily():
    r = ExecutorRouter()
    assert (r._threads, r._processes, r._loop) == (None, None, None)
    r.submit(_flows.io_double, (1,)).result(timeout=10)
    assert r._threads is not None
    assert (r._processes, r._loop) == (None, None)
    r.shutdown()


def test_shutdown_twice_and_stops_loop_thread():
    r = ExecutorRouter(cpu_workers=1, mp_context=SPAWN)
    for t, args in [(_flows.io_double, (1,)), (_flows.square, (2,)), (_flows.async_echo, (3,))]:
        r.submit(t, args).result(timeout=30)
    loop_thread = r._loop_thread
    assert loop_thread is not None
    assert loop_thread.daemon
    r.shutdown()
    r.shutdown()
    assert not loop_thread.is_alive()
    assert r._loop is not None
    assert r._loop.is_closed()


def test_shutdown_without_pools():
    r = ExecutorRouter()
    r.shutdown()
    r.shutdown(wait=False)


def test_submit_after_shutdown_raises():
    r = ExecutorRouter()
    r.shutdown()
    for t in (_flows.io_double, _flows.square, _flows.async_echo):
        with pytest.raises(RuntimeError, match="shutdown"):
            r.submit(t, (1,))


def test_context_manager_shuts_down():
    with ExecutorRouter() as r:
        assert r.submit(_flows.async_echo, ("x",)).result(timeout=10).value == "x"
        loop_thread = r._loop_thread
    assert loop_thread is not None
    assert not loop_thread.is_alive()
    with pytest.raises(RuntimeError):
        r.submit(_flows.io_double, (1,))


def test_shutdown_waits_for_pending_coroutines():
    r = ExecutorRouter()
    future = r.submit(_flows.async_echo, ("late", 0.2))
    r.shutdown(wait=True)
    assert future.result(timeout=0).value == "late"


def test_shutdown_cancel_futures_cancels_coroutines():
    r = ExecutorRouter()
    future = r.submit(_flows.async_forever)
    r.shutdown(wait=True, cancel_futures=True)
    assert future.cancelled()


def test_shutdown_no_wait_cancels_coroutines():
    r = ExecutorRouter()
    future = r.submit(_flows.async_forever)
    loop_thread = r._loop_thread
    r.shutdown(wait=False)
    assert loop_thread is not None
    loop_thread.join(timeout=10)
    with pytest.raises(CancelledError):
        future.result(timeout=10)


def test_cancelling_queued_cpu_future():
    with ExecutorRouter(cpu_workers=1, mp_context=SPAWN) as r:
        blocker = r.submit(_flows.cpu_sleep, (0.5,))
        queued = [r.submit(_flows.square, (i,)) for i in range(5)]
        assert queued[-1].cancel()
        assert blocker.result(timeout=30).value == 0.5
        assert [f.result(timeout=30).value for f in queued[:-1]] == [0, 1, 4, 9]
        assert queued[-1].cancelled()


def test_shutdown_cancel_futures_cancels_queued_cpu_tasks():
    r = ExecutorRouter(cpu_workers=1, mp_context=SPAWN)
    r.submit(_flows.cpu_sleep, (0.3,))
    queued = [r.submit(_flows.square, (i,)) for i in range(20)]
    r.shutdown(wait=True, cancel_futures=True)
    assert any(f.cancelled() for f in queued)


# -- validation ---------------------------------------------------------------


@pytest.mark.parametrize("options", [{"io_workers": 0}, {"cpu_workers": 0}])
def test_invalid_pool_sizes(options):
    with pytest.raises(ValueError, match="workers"):
        ExecutorRouter(**options)


def test_unknown_kind_rejected(router):
    bogus = _io_task(lambda: None)
    object.__setattr__(bogus, "kind", "gpu")
    with pytest.raises(ValueError, match="no executor"):
        router.submit(bogus)


def test_io_task_may_be_a_local_function(router):
    lock = threading.Lock()  # closures are fine for threads, unlike processes

    def locked() -> bool:
        with lock:
            return True

    assert router.submit(_io_task(locked)).result(timeout=10).value is True
