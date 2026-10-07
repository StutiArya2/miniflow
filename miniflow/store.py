"""SQLite persistence of runs, task states, attempts and results (spec §3.4, §3.6 "Resume").

Contract (Phase 2, module C)
----------------------------
``StateStore(path: str | os.PathLike = ":memory:")``
    Opens (and creates if needed) a SQLite database with tables::

        runs(run_id TEXT PK, flow TEXT, graph TEXT, started_at REAL,
             finished_at REAL, status TEXT)
        task_states(run_id, task, state TEXT, attempts INT, result BLOB,
                    error TEXT, from_cache INT, started_at REAL, finished_at REAL,
                    updated_at REAL, PRIMARY KEY(run_id, task))
        attempts(run_id, task, attempt INT, state TEXT, error TEXT,
                 worker TEXT, started_at REAL, finished_at REAL)

    ``runs.graph`` is JSON ``{"tasks": {name: {"kind": ..., "after": [...]}}}``
    so the dashboard (Phase 3.5) can draw a run without the flow file.
    ``attempts.worker`` names what ran it (e.g. ``"thread-3"``, ``"pid-1234"``,
    ``"loop"``) for the dashboard timeline. ``updated_at`` lets readers fetch
    only rows changed since a timestamp.

    Thread-safe (the scheduler writes from one thread, but CLI ``status`` may
    read concurrently): use WAL mode and one connection guarded by a lock.
    Usable as a context manager.

Methods
    ``create_run(flow: str, dag: DAG) -> str`` - new run id, all tasks PENDING, graph JSON saved.
    ``set_state(run_id, task, state: State, *, error: str | None = None) -> None``
    ``record_attempt(run_id, task, attempt, state, error, started_at, finished_at,
    worker: str | None = None) -> None``
    ``save_result(run_id, task, value: Any, *, from_cache: bool = False) -> None`` -
    pickled; marks SUCCESS. ``from_cache=True`` when the value came from a resume.
    ``get_graph(run_id) -> dict[str, Any]`` - the JSON saved by ``create_run``.
    ``changes_since(run_id, ts: float) -> list[TaskRecord]`` - rows with ``updated_at > ts``.
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
