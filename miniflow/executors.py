"""Worker adapters behind one ``submit()`` interface (spec §3.4, §3.6 "Worker routing").

Contract (Phase 2, module A)
---------------------------
``ExecutorRouter(io_workers: int = 8, cpu_workers: int | None = None, *, mp_context=None)``
    Owns a ``ThreadPoolExecutor`` (kind ``"io"``), a ``ProcessPoolExecutor``
    (kind ``"cpu"``, default size ``os.cpu_count()``) and an asyncio event loop
    running in a background daemon thread (kind ``"async"``). Pools are created
    lazily on first use. Usable as a context manager; ``__exit__`` calls
    :meth:`shutdown`. ``mp_context`` (e.g. ``multiprocessing.get_context("spawn")``)
    is passed to the process pool; ``None`` uses the platform default.

``submit(task: Task, args: tuple, kwargs: dict) -> concurrent.futures.Future[Outcome]``
    Dispatches through a table keyed by ``task.kind``. For ``"cpu"`` the
    *Task object* is sent to the child (it pickles by reference, see
    ``Task.__reduce__``) and the child calls ``task.fn(*args, **kwargs)``.
    For ``"async"`` the coroutine is scheduled with
    ``asyncio.run_coroutine_threadsafe`` so every kind returns a
    ``concurrent.futures.Future`` and the scheduler can ``wait()`` on all of them.
    ``task.timeout`` is NOT enforced here; the scheduler handles it.
    Raises ``RuntimeError`` after :meth:`shutdown`.

``@dataclass(frozen=True) Outcome(value: Any, worker: str, started_at: float, finished_at: float)``
    What every future resolves to. The wrapper runs *inside* the worker, so
    ``worker`` is ``"thread-<name>"``, ``"pid-<os.getpid()>"`` or ``"loop"``, and the
    timestamps (``time.time()``) bracket only the function call, not the queueing.
    On an exception, the future raises ``TaskError(original, worker, started_at,
    finished_at)``, which keeps the original as ``__cause__`` and stays picklable.
    The dashboard timeline (Phase 3.5) is built from these fields. Failures of the
    pool itself (e.g. ``BrokenProcessPool``) are raised as they are, not wrapped.

``shutdown(wait: bool = True, cancel_futures: bool = False) -> None``
    Stops all pools and the event loop thread. Idempotent. Coroutines still
    pending when the loop stops are cancelled; with ``wait=True`` they are first
    allowed to finish unless ``cancel_futures`` is set.
"""

from __future__ import annotations

import asyncio
import os
import pickle
import threading
import time
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import Future, InvalidStateError, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from multiprocessing.context import BaseContext
from types import TracebackType
from typing import Any, Self

from miniflow.task import Kind, Task

__all__ = ["ExecutorRouter", "Outcome", "TaskError"]


@dataclass(frozen=True)
class Outcome:
    """Result of one successful call, with where and when it ran."""

    value: Any
    worker: str
    started_at: float
    finished_at: float

    @property
    def duration(self) -> float:
        """Seconds spent inside the task function."""
        return self.finished_at - self.started_at


class TaskError(Exception):
    """A task function raised; wraps the original exception with worker and timing."""

    def __init__(
        self, original: BaseException, worker: str, started_at: float, finished_at: float
    ) -> None:
        # Everything lives in ``args`` so the default exception pickling round-trips it.
        super().__init__(original, worker, started_at, finished_at)
        self.original = original
        self.worker = worker
        self.started_at = started_at
        self.finished_at = finished_at
        self.__cause__ = original

    def __str__(self) -> str:
        return f"{type(self.original).__name__}: {self.original} (on {self.worker})"


def _call(
    fn: Callable[..., Any], args: tuple[Any, ...], kwargs: Mapping[str, Any], worker: str
) -> Outcome:
    started = time.time()
    try:
        value = fn(*args, **kwargs)
    except Exception as exc:
        raise TaskError(exc, worker, started, time.time()) from exc
    return Outcome(value, worker, started, time.time())


def _run_in_thread(
    fn: Callable[..., Any], args: tuple[Any, ...], kwargs: Mapping[str, Any]
) -> Outcome:
    return _call(fn, args, kwargs, f"thread-{threading.current_thread().name}")


def _run_in_process(
    task: Task, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Outcome | TaskError:
    """Runs in the child. Errors are *returned*, because ``ProcessPoolExecutor``
    would replace a raised exception's ``__cause__`` with the remote traceback."""
    try:
        return _call(task.fn, args, kwargs, f"pid-{os.getpid()}")
    except TaskError as err:
        try:
            pickle.dumps(err.original)
        except Exception:
            # Keep the type name and message even if the exception can't cross processes.
            original = RuntimeError(f"{type(err.original).__qualname__}: {err.original}")
            return TaskError(original, err.worker, err.started_at, err.finished_at)
        return err


async def _run_in_loop(
    fn: Callable[..., Coroutine[Any, Any, Any]], args: tuple[Any, ...], kwargs: Mapping[str, Any]
) -> Outcome:
    started = time.time()
    try:
        value = await fn(*args, **kwargs)
    except Exception as exc:
        raise TaskError(exc, "loop", started, time.time()) from exc
    return Outcome(value, "loop", started, time.time())


def _loop_main(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    finally:
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


async def _drain(cancel: bool) -> None:
    current = asyncio.current_task()
    others = [t for t in asyncio.all_tasks() if t is not current]
    if cancel:
        for t in others:
            t.cancel()
    await asyncio.gather(*others, return_exceptions=True)


def _settle(outer: Future[Outcome], inner: Future[Outcome | TaskError]) -> None:
    """Copy a finished process-pool future onto the future handed to the caller."""
    try:
        if inner.cancelled():
            outer.cancel()
            return
        exc = inner.exception()
        if exc is not None:
            outer.set_exception(exc)
            return
        result = inner.result()
        if isinstance(result, TaskError):
            outer.set_exception(result)
        else:
            outer.set_result(result)
    except InvalidStateError:  # the caller cancelled ``outer`` already
        pass


Submitter = Callable[[Task, tuple[Any, ...], dict[str, Any]], "Future[Outcome]"]


class ExecutorRouter:
    """Routes each task to the thread pool, process pool or event loop by ``task.kind``."""

    def __init__(
        self,
        io_workers: int = 8,
        cpu_workers: int | None = None,
        *,
        mp_context: BaseContext | None = None,
    ) -> None:
        if io_workers < 1:
            raise ValueError("io_workers must be >= 1")
        if cpu_workers is not None and cpu_workers < 1:
            raise ValueError("cpu_workers must be >= 1")
        self.io_workers = io_workers
        self.cpu_workers = cpu_workers
        self._mp_context = mp_context
        self._lock = threading.Lock()
        self._closed = False
        self._threads: ThreadPoolExecutor | None = None
        self._processes: ProcessPoolExecutor | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._dispatch: dict[Kind, Submitter] = {
            "io": self._submit_io,
            "cpu": self._submit_cpu,
            "async": self._submit_async,
        }

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.shutdown()

    def submit(
        self, task: Task, args: tuple[Any, ...] = (), kwargs: Mapping[str, Any] | None = None
    ) -> Future[Outcome]:
        """Start ``task.fn(*args, **kwargs)`` on the worker for ``task.kind``."""
        try:
            submitter = self._dispatch[task.kind]
        except KeyError:
            raise ValueError(f"task {task.name!r}: no executor for kind {task.kind!r}") from None
        return submitter(task, tuple(args), dict(kwargs or {}))

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        """Stop every pool and the event loop thread. Safe to call more than once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            threads, processes = self._threads, self._processes
            loop, loop_thread = self._loop, self._loop_thread
        if loop is not None and loop_thread is not None:
            if wait and threading.current_thread() is not loop_thread:
                asyncio.run_coroutine_threadsafe(_drain(cancel_futures), loop).result()
            loop.call_soon_threadsafe(loop.stop)
            if wait and threading.current_thread() is not loop_thread:
                loop_thread.join()
        if threads is not None:
            threads.shutdown(wait=wait, cancel_futures=cancel_futures)
        if processes is not None:
            processes.shutdown(wait=wait, cancel_futures=cancel_futures)

    # -- per-kind submitters ---------------------------------------------------

    def _submit_io(
        self, task: Task, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Future[Outcome]:
        with self._lock:
            self._check_open()
            if self._threads is None:
                self._threads = ThreadPoolExecutor(
                    self.io_workers, thread_name_prefix="miniflow-io"
                )
            return self._threads.submit(_run_in_thread, task.fn, args, kwargs)

    def _submit_cpu(
        self, task: Task, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Future[Outcome]:
        with self._lock:
            self._check_open()
            if self._processes is None:
                self._processes = ProcessPoolExecutor(self.cpu_workers, mp_context=self._mp_context)
            inner = self._processes.submit(_run_in_process, task, args, kwargs)
        outer: Future[Outcome] = Future()
        outer.add_done_callback(lambda f: inner.cancel() if f.cancelled() else None)
        inner.add_done_callback(lambda f: _settle(outer, f))
        return outer

    def _submit_async(
        self, task: Task, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Future[Outcome]:
        with self._lock:
            self._check_open()
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._loop_thread = threading.Thread(
                    target=_loop_main, args=(self._loop,), name="miniflow-loop", daemon=True
                )
                self._loop_thread.start()
            return asyncio.run_coroutine_threadsafe(_run_in_loop(task.fn, args, kwargs), self._loop)

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("cannot submit after ExecutorRouter.shutdown()")
