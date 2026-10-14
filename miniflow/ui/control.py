"""Start, cancel and resume runs from the dashboard (standard library only).

``RunController(flows_dir, store)`` lets the dashboard launch the flow files in
one folder: Python flows (``*.py``) and no-code flows (``*.json``); files whose name
starts with ``_`` are left out. Requests
only ever name a flow from that folder or a run id from the store, never a path.

Each run is a separate ``python -m miniflow run|resume <file> --db <db> --quiet``
process with ``MINIFLOW_FAIL_RATE`` set from the dashboard. It behaves exactly
like a run started in a terminal and writes to the same state database that the
dashboard reads, so the live view needs nothing extra. One run is active at a time.
A JSON flow saved into the folder after startup is found the first time it is asked for.

Runs started by schedules and triggers (:meth:`RunController.enqueue`) wait in a first-in,
first-out queue when a run is active, instead of being refused; the Run button of a person
still says "still running". The queue holds at most ``max_queue`` runs, and one source
(for example ``schedule:nightly``) can wait only once. A trigger's details reach the flow in
the ``MINIFLOW_TRIGGER`` environment variable, as JSON.

:meth:`RunController.cancel` sends the process the interrupt that Ctrl+C sends
(Ctrl+Break on Windows, which :func:`miniflow.cli.entry` turns into
``KeyboardInterrupt``), so the scheduler marks the run INTERRUPTED and it can be
resumed. A process still alive after ``grace`` seconds is killed together with its
workers, and the controller marks its run INTERRUPTED itself. It does the same for
a run whose process died without finishing it, and reports that as an error.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import IO, Any

from miniflow.flowfile import FlowFileError, load_json_flow
from miniflow.store import RunRecord, StateStore

__all__ = ["ControlError", "RunController"]

log = logging.getLogger("miniflow.ui")

_ERROR_LINES = 20
_DEFAULT_MAX_QUEUE = 20
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")

# Each run gets its own process group, so it can be interrupted or killed with its workers.
if sys.platform == "win32":
    _CREATIONFLAGS, _NEW_SESSION = subprocess.CREATE_NEW_PROCESS_GROUP, False
else:
    _CREATIONFLAGS, _NEW_SESSION = 0, True


class ControlError(Exception):
    """A request the controller refuses; :attr:`status` is the HTTP status for it."""

    def __init__(self, message: str, status: HTTPStatus) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class _Child:
    action: str
    flow: str
    proc: subprocess.Popen[bytes]
    stderr: IO[bytes]
    launched_at: float
    run_id: str | None = None
    cancelled: bool = field(default=False)
    source: str = ""
    on_finish: Callable[[str | None], None] | None = None

    def as_json(self) -> dict[str, Any]:
        return {"action": self.action, "flow": self.flow, "run_id": self.run_id}


@dataclass
class _Queued:
    """A scheduled or triggered run waiting for the active run to finish."""

    action: str
    flow: str
    path: Path
    rate: float
    extra: list[str]
    source: str
    trigger: dict[str, Any] | None
    queued_at: float
    on_start: Callable[[], None] | None = None
    on_finish: Callable[[str | None], None] | None = None


def _check_rate(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        raise ControlError("fail_rate must be a number from 0 to 1", HTTPStatus.BAD_REQUEST)
    return float(value)


def _interrupt(proc: subprocess.Popen[bytes]) -> None:
    """Send the process group the interrupt Ctrl+C would (Ctrl+Break on Windows)."""
    with contextlib.suppress(OSError):
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGINT)


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill the process and its worker processes."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)


def _tail(f: IO[bytes]) -> str:
    f.seek(0)
    lines = f.read().decode(errors="replace").strip().splitlines()
    return "\n".join(lines[-_ERROR_LINES:])


class RunController:
    """Launches and stops flow runs for the dashboard; safe to call from any thread."""

    def __init__(
        self, flows_dir: str | os.PathLike[str], store: StateStore, grace: float = 5.0,
        *, keep_runs: int | None = 100, keep_days: float | None = None,
        max_concurrent: int = 1,
    ) -> None:  # fmt: skip
        folder = Path(flows_dir)
        if not folder.is_dir():
            raise NotADirectoryError(f"flows folder {folder} doesn't exist")
        files = sorted([*folder.glob("*.py"), *folder.glob("*.json")], key=lambda p: p.name)
        self.flows: dict[str, Path] = {}
        for p in files:
            if (
                p.is_file()
                and not p.is_symlink()
                and not p.name.startswith("_")
                and p.resolve().parent == folder.resolve()
            ):
                self.flows.setdefault(p.stem, p.resolve())  # a .py and .json of one name: .json
        self.flows = dict(sorted(self.flows.items()))
        self.folder = folder.resolve()
        self.store = store
        self.db = str(Path(store.path).resolve())
        self.grace = grace
        self._lock = threading.Lock()
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self.max_concurrent = max_concurrent
        self._children: list[_Child] = []
        self._error: str | None = None
        self._queue: deque[_Queued] = deque()
        self.max_queue = _DEFAULT_MAX_QUEUE
        self.keep_runs = keep_runs
        self.keep_days = keep_days
        self.store.prune_history(keep_runs=keep_runs, keep_days=keep_days)

    # ---------------------------------------------------------------- actions
    def start(self, flow: str, fail_rate: object, environment: object = None) -> dict[str, Any]:
        """Run ``flow`` with ``MINIFLOW_FAIL_RATE=fail_rate``; returns the active run."""
        rate = _check_rate(fail_rate)
        path = self._flow_path(flow)
        if path is None:
            raise ControlError(f"no flow {flow!r} in the flows folder", HTTPStatus.NOT_FOUND)
        extra = self._environment_args(path, environment)
        return self._launch("run", flow, path, rate, extra)

    def resume(self, run_id: str, fail_rate: object) -> dict[str, Any]:
        """Resume run ``run_id``, reusing the tasks that already succeeded."""
        rate = _check_rate(fail_rate)
        meta = self._run(run_id)
        if meta is None:
            raise ControlError(f"no run {run_id!r}", HTTPStatus.NOT_FOUND)
        path = self._flow_path(meta.flow)
        if path is None:
            raise ControlError(
                f"flow {meta.flow!r} of run {run_id} is not in the flows folder",
                HTTPStatus.NOT_FOUND,
            )
        return self._launch("resume", meta.flow, path, rate, ["--run", run_id])

    def partial(self, flow: str, step: str, environment: object = None) -> dict[str, Any]:
        """Run through one JSON step, reusing only matching earlier results."""
        path = self._flow_path(flow)
        if path is None or path.suffix != ".json":
            raise ControlError(f"no JSON flow {flow!r} in the flows folder", HTTPStatus.NOT_FOUND)
        try:
            extra = self._environment_args(path, environment)
            dag = load_json_flow(
                path, environment=environment if isinstance(environment, str) else None
            ).dag
        except FlowFileError as exc:
            raise ControlError(str(exc), HTTPStatus.BAD_REQUEST) from exc
        if step not in dag:
            raise ControlError(f"no step {step!r} in {flow!r}", HTTPStatus.BAD_REQUEST)
        return self._launch(
            "partial", flow, path, 0.0, ["--until", step, "--reuse", "latest", *extra]
        )

    def _environment_args(self, path: Path, environment: object) -> list[str]:
        if environment is None or environment == "":
            return []
        if path.suffix != ".json":
            raise ControlError("environments only apply to JSON flows", HTTPStatus.BAD_REQUEST)
        if not isinstance(environment, str) or not _ENV_NAME.fullmatch(environment):
            raise ControlError("invalid environment name", HTTPStatus.BAD_REQUEST)
        if not (path.parent / "environments" / f"{environment}.json").is_file():
            raise ControlError(f"no environment {environment!r}", HTTPStatus.BAD_REQUEST)
        return ["--environment", environment]

    def enqueue(
        self,
        flow: str,
        *,
        source: str,
        environment: object = None,
        trigger: dict[str, Any] | None = None,
        on_start: Callable[[], None] | None = None,
        on_finish: Callable[[str | None], None] | None = None,
    ) -> str:
        """Run ``flow`` now, or queue it if a run is active: returns ``"started"`` or ``"queued"``.

        ``on_start()`` is called when the run starts (now or later) and ``on_finish(error)``
        when it ends, with a message if its process failed rather than the flow. ``source``
        names what asked (``schedule:nightly``); it can wait in the queue only once.
        """
        path = self._flow_path(flow)
        if path is None:
            raise ControlError(f"no flow {flow!r} in the flows folder", HTTPStatus.NOT_FOUND)
        item = _Queued(
            "run", flow, path, 0.0, self._environment_args(path, environment), source,
            trigger, time.time(), on_start, on_finish,
        )  # fmt: skip
        with self._lock:
            self._reap()
            if self._can_start(flow) and not self._queue:
                self._spawn_item(item)
                return "started"
            if any(q.source == source for q in self._queue):
                raise ControlError(f"{source} is already waiting in the queue", HTTPStatus.CONFLICT)
            if len(self._queue) >= self.max_queue:
                raise ControlError(
                    f"the run queue is full ({self.max_queue} waiting)",
                    HTTPStatus.TOO_MANY_REQUESTS,
                )
            self._queue.append(item)
            self._start_next()
            return "queued" if item in self._queue else "started"

    def cancel(self, run_id: str | None = None) -> None:
        """Stop an active run and return once its process has exited.

        Runs waiting in the queue are not affected; the next one starts."""
        with self._lock:
            self._reap()
            child = (
                next((c for c in self._children if c.run_id == run_id), None)
                if run_id is not None
                else next(iter(self._children), None)
            )
            if child is None:
                raise ControlError("no matching run is active", HTTPStatus.CONFLICT)
            child.cancelled = True
            _interrupt(child.proc)
        try:
            child.proc.wait(self.grace)
        except subprocess.TimeoutExpired:
            _kill_tree(child.proc)
            child.proc.wait()
        with self._lock:
            if child in self._children:
                self._finish(child)

    def status(self) -> dict[str, Any]:
        """``{"flows": [...], "active": {action, flow, run_id} | None, "error": str | None}``."""
        with self._lock:
            self._reap()
            return {
                "flows": list(self.flows),
                "active": self._children[0].as_json() if self._children else None,
                "running": [child.as_json() for child in self._children],
                "error": self._error,
                "queued": [
                    {"flow": q.flow, "source": q.source, "queued_at": q.queued_at}
                    for q in self._queue
                ],
            }

    def add_flow(self, name: str, path: Path) -> None:
        """Make a newly saved JSON flow available to the Run button."""
        with self._lock:
            self.flows[name] = path.resolve()
            self.flows = dict(sorted(self.flows.items()))

    def shutdown(self) -> None:
        """Drop the queue and cancel the active run (when the dashboard stops)."""
        with self._lock:
            self._queue.clear()
        with contextlib.suppress(ControlError):
            while self.status()["active"] is not None:
                self.cancel()

    # ---------------------------------------------------------------- helpers
    def _launch(
        self, action: str, flow: str, path: Path, rate: float, extra: list[str]
    ) -> dict[str, Any]:
        with self._lock:
            self._reap()
            if self._can_start(flow) and not self._queue:
                return self._spawn(action, flow, path, rate, extra)
            if any(q.source == f"manual:{action}:{flow}" for q in self._queue):
                raise ControlError(f"{flow} is already waiting in the queue", HTTPStatus.CONFLICT)
            if len(self._queue) >= self.max_queue:
                raise ControlError(
                    f"the run queue is full ({self.max_queue} waiting)",
                    HTTPStatus.TOO_MANY_REQUESTS,
                )
            item = _Queued(
                action, flow, path, rate, extra, f"manual:{action}:{flow}", None, time.time()
            )
            self._queue.append(item)
            self._start_next()
            if item in self._queue:
                return {"action": action, "flow": flow, "run_id": None, "queued": True}
            child = next((c for c in self._children if c.source == item.source), None)
            if child is None:
                raise ControlError(self._error or "run could not start", HTTPStatus.BAD_GATEWAY)
            return child.as_json()

    def _flow_path(self, flow: str) -> Path | None:
        """The flow's file. A JSON flow saved after startup (by the Editor of another
        process, or an agent) is found here and remembered."""
        path = self.flows.get(flow)
        if not _ENV_NAME.fullmatch(flow) or flow.startswith("_"):
            return None
        if path is not None:
            return (
                path
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path.resolve().parent == self.folder
                )
                else None
            )
        candidate = self.folder / f"{flow}.json"
        if not candidate.is_file() or candidate.is_symlink():
            return None
        if candidate.resolve().parent != self.folder:
            return None
        self.add_flow(flow, candidate)
        return candidate.resolve()

    def _can_start(self, flow: str) -> bool:
        return len(self._children) < self.max_concurrent and all(
            child.flow != flow for child in self._children
        )

    def _spawn_item(self, item: _Queued) -> None:
        self._spawn(
            item.action, item.flow, item.path, item.rate, item.extra,
            trigger=item.trigger, source=item.source,
            on_start=item.on_start, on_finish=item.on_finish,
        )  # fmt: skip

    def _spawn(
        self,
        action: str,
        flow: str,
        path: Path,
        rate: float,
        extra: list[str],
        *,
        trigger: dict[str, Any] | None = None,
        source: str = "",
        on_start: Callable[[], None] | None = None,
        on_finish: Callable[[str | None], None] | None = None,
    ) -> dict[str, Any]:
        """Start the run's process while the controller lock is held."""
        command = "run" if action == "partial" else action
        cmd = [sys.executable, "-m", "miniflow", command, str(path), "--db", self.db, "--quiet"]
        env = {**os.environ, "MINIFLOW_FAIL_RATE": f"{rate:g}"}
        env.pop("MINIFLOW_TRIGGER", None)
        if trigger is not None:
            env["MINIFLOW_TRIGGER"] = json.dumps(trigger, separators=(",", ":"), sort_keys=True)
        # Not a pipe, so a chatty run can't block; kept open until _finish reads it.
        stderr = tempfile.TemporaryFile()  # noqa: SIM115
        launched_at = time.time()
        try:
            proc = subprocess.Popen(
                [*cmd, *extra],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                creationflags=_CREATIONFLAGS,
                start_new_session=_NEW_SESSION,
            )
        except OSError:
            stderr.close()
            raise
        child = _Child(action, flow, proc, stderr, launched_at, source=source, on_finish=on_finish)
        self._children.append(child)
        self._error = None
        if on_start is not None:
            self._notify(on_start)
        return child.as_json()

    @staticmethod
    def _notify(callback: Callable[..., None], *args: Any) -> None:
        """Callbacks are the schedule service's bookkeeping; they must not break a run."""
        try:
            callback(*args)
        except Exception:
            log.exception("run callback failed")

    def _run(self, run_id: str) -> RunRecord | None:
        return next((r for r in self.store.list_runs() if r.run_id == run_id), None)

    def _find_run(self, child: _Child) -> str | None:
        """The run the child created: the newest run of its flow since it launched."""
        for r in self.store.list_runs():  # newest first
            if r.started_at < child.launched_at:
                return None
            if r.flow == child.flow:
                return r.run_id
        return None

    def _reap(self) -> None:
        """Note active run ids and clean up every process that has exited."""
        for child in list(self._children):
            if child.run_id is None:
                child.run_id = self._find_run(child)
            if child.proc.poll() is not None:
                self._finish(child)

    def _finish(self, child: _Child) -> None:
        """Called with the lock held once the child has exited."""
        error = self._close(child)
        if child.on_finish is not None:
            self._notify(child.on_finish, error)
        self.store.prune_history(keep_runs=self.keep_runs, keep_days=self.keep_days)
        self._start_next()

    def _start_next(self) -> None:
        """Fill free slots with the oldest queued run whose flow is not already active."""
        while self._queue and len(self._children) < self.max_concurrent:
            index = next((i for i, q in enumerate(self._queue) if self._can_start(q.flow)), None)
            if index is None:
                return
            item = self._queue[index]
            del self._queue[index]
            try:
                self._spawn_item(item)
            except OSError as exc:
                self._error = f"{item.flow} could not start: {exc}"
                if item.on_finish is not None:
                    self._notify(item.on_finish, self._error)

    def _close(self, child: _Child) -> str | None:
        """Clean up after an exited child: free the slot, read its output, fix its run."""
        self._children.remove(child)
        tail = _tail(child.stderr)
        child.stderr.close()
        code = child.proc.returncode
        run_id = child.run_id or self._find_run(child)
        if run_id is None:
            if not child.cancelled:
                self._error = f"{child.flow} exited with code {code} before starting a run"
                self._error += f":\n{tail}" if tail else ""
                return self._error
            return "run cancelled before it started"
        meta = self._run(run_id)
        if meta is not None and meta.finished_at is None:
            self.store.finish_run(run_id, "INTERRUPTED")
            if not child.cancelled:
                self._error = (
                    f"run {run_id} of {child.flow} stopped unexpectedly (exit code {code})"
                )
                self._error += f":\n{tail}" if tail else ""
                return self._error
            return f"run {run_id} was cancelled"
        if meta is not None and meta.status != "SUCCESS":
            return f"run {run_id} finished with status {meta.status}"
        return None
