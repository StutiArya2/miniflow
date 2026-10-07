"""SQLite persistence of runs, task states, attempts and results (spec §3.4, §3.6 "Resume").

Contract (Phase 2, module C)
----------------------------
``StateStore(path: str | os.PathLike = ":memory:")``
    Opens (and creates if needed) a SQLite database with tables::

        runs(run_id TEXT PK, flow TEXT, started_at REAL, finished_at REAL, status TEXT)
        task_states(run_id, task, state TEXT, attempts INT, result BLOB,
                    error TEXT, started_at REAL, finished_at REAL,
                    PRIMARY KEY(run_id, task))
        attempts(run_id, task, attempt INT, state TEXT, error TEXT,
                 started_at REAL, finished_at REAL)

    Thread-safe (the scheduler writes from one thread, but CLI ``status`` may
    read concurrently): use WAL mode and one connection guarded by a lock.
    Usable as a context manager.

Methods
    ``create_run(flow: str, tasks: Iterable[str]) -> str`` - new run id, all tasks PENDING.
    ``set_state(run_id, task, state: State, *, error: str | None = None) -> None``
    ``record_attempt(run_id, task, attempt, state, error, started_at, finished_at) -> None``
    ``save_result(run_id, task, value: Any) -> None`` - pickled; marks SUCCESS.
    ``load_completed(run_id) -> dict[str, Any]`` - task -> unpickled result for SUCCESS tasks.
    ``get_states(run_id) -> dict[str, State]``
    ``attempts(run_id, task: str | None = None) -> list[AttemptRecord]``
    ``finish_run(run_id, status: str) -> None``
    ``latest_run(flow: str | None = None) -> str | None``
    ``list_runs() -> list[RunRecord]``
"""

from __future__ import annotations

__all__ = ["StateStore"]


class StateStore:
    """Placeholder until Phase 2 (module C)."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("store.py is implemented in Phase 2 (module C)")
