"""Worker adapters behind one ``submit()`` interface (spec §3.4, §3.6 "Worker routing").

Contract (Phase 2, module A)
---------------------------
``ExecutorRouter(io_workers: int = 8, cpu_workers: int | None = None)``
    Owns a ``ThreadPoolExecutor`` (kind ``"io"``), a ``ProcessPoolExecutor``
    (kind ``"cpu"``, default size ``os.cpu_count()``) and an asyncio event loop
    running in a background daemon thread (kind ``"async"``). Pools are created
    lazily on first use. Usable as a context manager; ``__exit__`` calls
    :meth:`shutdown`.

``submit(task: Task, args: tuple, kwargs: dict) -> concurrent.futures.Future[Outcome]``
    Dispatches through a table keyed by ``task.kind``. For ``"cpu"`` the
    *Task object* is sent to the child (it pickles by reference, see
    ``Task.__reduce__``) and the child calls ``task.fn(*args, **kwargs)``.
    For ``"async"`` the coroutine is scheduled with
    ``asyncio.run_coroutine_threadsafe`` so every kind returns a
    ``concurrent.futures.Future`` and the scheduler can ``wait()`` on all of them.
    ``task.timeout`` is NOT enforced here; the scheduler handles it.

``@dataclass(frozen=True) Outcome(value: Any, worker: str, started_at: float, finished_at: float)``
    What every future resolves to. The wrapper runs *inside* the worker, so
    ``worker`` is ``"thread-<name>"``, ``"pid-<os.getpid()>"`` or ``"loop"``, and the
    timestamps (``time.time()``) bracket only the function call, not the queueing.
    On an exception, the future raises ``TaskError(original, worker, started_at,
    finished_at)``, which keeps the original as ``__cause__`` and stays picklable.
    The dashboard timeline (Phase 3.5) is built from these fields.

``shutdown(wait: bool = True, cancel_futures: bool = False) -> None``
    Stops all pools and the event loop thread. Idempotent.
"""

from __future__ import annotations

__all__ = ["ExecutorRouter"]


class ExecutorRouter:
    """Placeholder until Phase 2 (module A)."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("executors.py is implemented in Phase 2 (module A)")
