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

    ``runs.graph`` is JSON ``{"tasks": {name: {"kind", "after", "description"}}, "about": {...}}``
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
    ``set_preview(run_id, task, preview)`` - short human-readable text of a result.
    ``save_result(run_id, task, value: Any, *, from_cache=False, preview=None) -> None`` -
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

import json
import os
import pickle
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any

from miniflow.preview import inspect_value
from miniflow.states import TERMINAL, State

if TYPE_CHECKING:
    from miniflow.graph import DAG

__all__ = ["AttemptRecord", "RunRecord", "ScheduleEvent", "StateStore", "TaskRecord"]

_SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    flow        TEXT NOT NULL,
    graph       TEXT NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL,
    status      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_states (
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    task        TEXT NOT NULL,
    state       TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    result      BLOB,
    error       TEXT,
    from_cache  INTEGER NOT NULL DEFAULT 0,
    preview     TEXT,
    sample      TEXT,
    item_count  INTEGER,
    started_at  REAL,
    finished_at REAL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (run_id, task)
);
CREATE INDEX IF NOT EXISTS task_states_updated ON task_states(run_id, updated_at);
CREATE TABLE IF NOT EXISTS attempts (
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    task        TEXT NOT NULL,
    attempt     INTEGER NOT NULL,
    state       TEXT NOT NULL,
    error       TEXT,
    worker      TEXT,
    started_at  REAL,
    finished_at REAL,
    PRIMARY KEY (run_id, task, attempt)
);
CREATE TABLE IF NOT EXISTS schedule_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    flow        TEXT NOT NULL,
    schedule    TEXT NOT NULL,
    due_at      REAL NOT NULL,
    recorded_at REAL NOT NULL,
    status      TEXT NOT NULL,
    message     TEXT
);
"""


@dataclass(frozen=True)
class RunRecord:
    """One row of ``runs`` (without the graph; use :meth:`StateStore.get_graph`)."""

    run_id: str
    flow: str
    started_at: float
    finished_at: float | None
    status: str


@dataclass(frozen=True)
class TaskRecord:
    """One row of ``task_states``. The pickled result is not loaded; see ``has_result``."""

    run_id: str
    task: str
    state: State
    attempts: int
    error: str | None
    from_cache: bool
    started_at: float | None
    finished_at: float | None
    updated_at: float
    has_result: bool
    preview: str | None = None
    sample: Any = None
    item_count: int | None = None


@dataclass(frozen=True)
class AttemptRecord:
    """One row of ``attempts``: a single execution of a task by one worker."""

    run_id: str
    task: str
    attempt: int
    state: State
    error: str | None
    worker: str | None
    started_at: float | None
    finished_at: float | None


@dataclass(frozen=True)
class ScheduleEvent:
    """One scheduled launch or a missed/failed launch."""

    id: int
    flow: str
    schedule: str
    due_at: float
    recorded_at: float
    status: str
    message: str | None


class StateStore:
    """SQLite-backed store of runs, task states, attempts and pickled results."""

    def __init__(self, path: str | os.PathLike[str] = ":memory:") -> None:
        self.path = os.fspath(path)
        self._lock = threading.RLock()
        self._last_ts = 0.0
        # One connection shared by all threads; every use holds ``self._lock``.
        # ``timeout`` covers other processes (e.g. the dashboard) holding the file.
        self._conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(task_states)")}
            if "preview" not in columns:  # databases written before result previews existed
                self._conn.execute("ALTER TABLE task_states ADD COLUMN preview TEXT")
            if "sample" not in columns:
                self._conn.execute("ALTER TABLE task_states ADD COLUMN sample TEXT")
            if "item_count" not in columns:
                self._conn.execute("ALTER TABLE task_states ADD COLUMN item_count INTEGER")
            self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            self._conn.commit()

    # --------------------------------------------------------------- lifecycle
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _now(self) -> float:
        """Wall-clock time, strictly increasing per store so ``changes_since`` misses nothing."""
        ts = max(time.time(), self._last_ts + 1e-6)
        self._last_ts = ts
        return ts

    # ------------------------------------------------------------------ writes
    def create_run(
        self, flow: str, dag: DAG, *, signatures: dict[str, str | None] | None = None
    ) -> str:
        """Start a new run: every task PENDING and the graph saved as JSON."""
        run_id = uuid.uuid4().hex[:12]
        order = dag.topological_order()
        graph: dict[str, Any] = {
            "tasks": {
                name: {
                    "kind": dag.tasks[name].kind,
                    "after": list(dag.upstream[name]),
                    "description": dag.tasks[name].description,
                    "optional": dag.tasks[name].optional,
                    **({"signature": signatures.get(name)} if signatures is not None else {}),
                }
                for name in order
            },
            "about": dict(dag.about),
        }
        with self._lock, self._conn:
            now = self._now()
            self._conn.execute(
                "INSERT INTO runs (run_id, flow, graph, started_at, finished_at, status)"
                " VALUES (?, ?, ?, ?, NULL, ?)",
                (run_id, flow, json.dumps(graph), now, "RUNNING"),
            )
            self._conn.executemany(
                "INSERT INTO task_states (run_id, task, state, updated_at) VALUES (?, ?, ?, ?)",
                [(run_id, name, State.PENDING.value, now) for name in order],
            )
        return run_id

    def set_state(self, run_id: str, task: str, state: State, *, error: str | None = None) -> None:
        """Move ``task`` to ``state``. ``error`` replaces the stored error when given.

        RUNNING stamps ``started_at`` (first time only); terminal states stamp ``finished_at``.
        """
        state = State(state)
        with self._lock, self._conn:
            now = self._now()
            cur = self._conn.execute(
                "UPDATE task_states SET state = ?, error = COALESCE(?, error),"
                " started_at = CASE WHEN ? THEN COALESCE(started_at, ?) ELSE started_at END,"
                " finished_at = CASE WHEN ? THEN ? ELSE finished_at END,"
                " updated_at = ? WHERE run_id = ? AND task = ?",
                (
                    state.value,
                    error,
                    state is State.RUNNING,
                    now,
                    state in TERMINAL,
                    now,
                    now,
                    run_id,
                    task,
                ),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no task {task!r} in run {run_id!r}")

    def record_attempt(
        self,
        run_id: str,
        task: str,
        attempt: int,
        state: State,
        error: str | None,
        started_at: float | None,
        finished_at: float | None,
        worker: str | None = None,
    ) -> None:
        """Log one attempt (re-recording the same attempt number replaces it)."""
        state = State(state)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE task_states SET attempts = MAX(attempts, ?), updated_at = ?"
                " WHERE run_id = ? AND task = ?",
                (attempt, self._now(), run_id, task),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no task {task!r} in run {run_id!r}")
            self._conn.execute(
                "INSERT OR REPLACE INTO attempts"
                " (run_id, task, attempt, state, error, worker, started_at, finished_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, task, attempt, state.value, error, worker, started_at, finished_at),
            )

    def save_result(
        self,
        run_id: str,
        task: str,
        value: Any,
        *,
        from_cache: bool = False,
        preview: str | None = None,
    ) -> None:
        """Pickle ``value`` and mark the task SUCCESS."""
        blob = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        sample, item_count = inspect_value(value)
        with self._lock, self._conn:
            now = self._now()
            cur = self._conn.execute(
                "UPDATE task_states SET state = ?, result = ?, error = NULL, from_cache = ?,"
                " preview = ?, sample = ?, item_count = ?, finished_at = ?, updated_at = ?"
                " WHERE run_id = ? AND task = ?",
                (
                    State.SUCCESS.value,
                    blob,
                    int(from_cache),
                    preview,
                    json.dumps(sample),
                    item_count,
                    now,
                    now,
                    run_id,
                    task,
                ),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no task {task!r} in run {run_id!r}")

    def set_preview(self, run_id: str, task: str, preview: str | None) -> None:
        """Store a short human-readable description of a task's result."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE task_states SET preview = ?, updated_at = ? WHERE run_id = ? AND task = ?",
                (preview, self._now(), run_id, task),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no task {task!r} in run {run_id!r}")

    def finish_run(self, run_id: str, status: str) -> None:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
                (status, self._now(), run_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no run {run_id!r}")

    def record_schedule_event(
        self, flow: str, schedule: str, due_at: float, status: str, message: str | None = None
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO schedule_events (flow, schedule, due_at, recorded_at, status, message)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (flow, schedule, due_at, self._now(), status, message),
            )

    def list_schedule_events(self, limit: int = 50) -> list[ScheduleEvent]:
        rows = self._query(
            "SELECT id, flow, schedule, due_at, recorded_at, status, message"
            " FROM schedule_events ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [ScheduleEvent(*row) for row in rows]

    # ------------------------------------------------------------------- reads
    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def get_graph(self, run_id: str) -> dict[str, Any]:
        rows = self._query("SELECT graph FROM runs WHERE run_id = ?", (run_id,))
        if not rows:
            raise KeyError(f"no run {run_id!r}")
        graph: dict[str, Any] = json.loads(rows[0][0])
        return graph

    def changes_since(self, run_id: str, ts: float) -> list[TaskRecord]:
        """Task rows of ``run_id`` with ``updated_at > ts``, oldest change first."""
        rows = self._query(
            "SELECT run_id, task, state, attempts, error, from_cache, started_at, finished_at,"
            " updated_at, result IS NOT NULL, preview, sample, item_count FROM task_states"
            " WHERE run_id = ? AND updated_at > ? ORDER BY updated_at, task",
            (run_id, ts),
        )
        return [
            TaskRecord(
                run_id=r[0],
                task=r[1],
                state=State(r[2]),
                attempts=r[3],
                error=r[4],
                from_cache=bool(r[5]),
                started_at=r[6],
                finished_at=r[7],
                updated_at=r[8],
                has_result=bool(r[9]),
                preview=r[10],
                sample=json.loads(r[11]) if r[11] is not None else None,
                item_count=r[12],
            )
            for r in rows
        ]

    def load_completed(self, run_id: str) -> dict[str, Any]:
        """Unpickled results of every SUCCESS task in ``run_id``."""
        rows = self._query(
            "SELECT task, result FROM task_states"
            " WHERE run_id = ? AND state = ? AND result IS NOT NULL",
            (run_id, State.SUCCESS.value),
        )
        return {task: pickle.loads(blob) for task, blob in rows}

    def get_states(self, run_id: str) -> dict[str, State]:
        rows = self._query(
            "SELECT task, state FROM task_states WHERE run_id = ? ORDER BY rowid", (run_id,)
        )
        return {task: State(state) for task, state in rows}

    def attempts(self, run_id: str, task: str | None = None) -> list[AttemptRecord]:
        """Attempt log of a run (or one task), ordered by start time then attempt number."""
        sql = (
            "SELECT run_id, task, attempt, state, error, worker, started_at, finished_at"
            " FROM attempts WHERE run_id = ?"
        )
        params: tuple[Any, ...] = (run_id,)
        if task is not None:
            sql += " AND task = ?"
            params += (task,)
        sql += " ORDER BY started_at, task, attempt"
        return [
            AttemptRecord(
                run_id=r[0],
                task=r[1],
                attempt=r[2],
                state=State(r[3]),
                error=r[4],
                worker=r[5],
                started_at=r[6],
                finished_at=r[7],
            )
            for r in self._query(sql, params)
        ]

    def latest_run(self, flow: str | None = None) -> str | None:
        """Id of the most recently created run, optionally restricted to ``flow``."""
        sql = "SELECT run_id FROM runs"
        params: tuple[Any, ...] = ()
        if flow is not None:
            sql += " WHERE flow = ?"
            params = (flow,)
        rows = self._query(sql + " ORDER BY started_at DESC, rowid DESC LIMIT 1", params)
        return rows[0][0] if rows else None

    def list_runs(self) -> list[RunRecord]:
        """All runs, newest first."""
        rows = self._query(
            "SELECT run_id, flow, started_at, finished_at, status FROM runs"
            " ORDER BY started_at DESC, rowid DESC"
        )
        return [RunRecord(*r) for r in rows]
