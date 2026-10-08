"""Start, cancel and resume runs from the dashboard (standard library only).

``RunController(flows_dir, store)`` lets the dashboard launch the flow files in
one folder: Python flows (``*.py``) and no-code flows (``*.json``); files whose name
starts with ``_`` are left out. Requests
only ever name a flow from that folder or a run id from the store, never a path.

Each run is a separate ``python -m miniflow run|resume <file> --db <db> --quiet``
process with ``MINIFLOW_FAIL_RATE`` set from the dashboard. It behaves exactly
like a run started in a terminal and writes to the same state database that the
dashboard reads, so the live view needs nothing extra. One run is active at a time.

:meth:`RunController.cancel` sends the process the interrupt that Ctrl+C sends
(Ctrl+Break on Windows, which :func:`miniflow.cli.entry` turns into
``KeyboardInterrupt``), so the scheduler marks the run INTERRUPTED and it can be
resumed. A process still alive after ``grace`` seconds is killed together with its
workers, and the controller marks its run INTERRUPTED itself. It does the same for
a run whose process died without finishing it, and reports that as an error.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import IO, Any

from miniflow.store import RunRecord, StateStore

__all__ = ["ControlError", "RunController"]

_ERROR_LINES = 20

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

    def as_json(self) -> dict[str, Any]:
        return {"action": self.action, "flow": self.flow, "run_id": self.run_id}


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
        self, flows_dir: str | os.PathLike[str], store: StateStore, grace: float = 5.0
    ) -> None:
        folder = Path(flows_dir)
        if not folder.is_dir():
            raise NotADirectoryError(f"flows folder {folder} doesn't exist")
        files = sorted([*folder.glob("*.py"), *folder.glob("*.json")], key=lambda p: p.name)
        self.flows: dict[str, Path] = {}
        for p in files:
            if p.is_file() and not p.name.startswith("_"):
                self.flows.setdefault(p.stem, p.resolve())  # a .py and .json of one name: .json
        self.flows = dict(sorted(self.flows.items()))
        self.store = store
        self.db = str(Path(store.path).resolve())
        self.grace = grace
        self._lock = threading.Lock()
        self._child: _Child | None = None
        self._error: str | None = None

    # ---------------------------------------------------------------- actions
    def start(self, flow: str, fail_rate: object) -> dict[str, Any]:
        """Run ``flow`` with ``MINIFLOW_FAIL_RATE=fail_rate``; returns the active run."""
        rate = _check_rate(fail_rate)
        path = self.flows.get(flow)
        if path is None:
            raise ControlError(f"no flow {flow!r} in the flows folder", HTTPStatus.NOT_FOUND)
        return self._launch("run", flow, path, rate, [])

    def resume(self, run_id: str, fail_rate: object) -> dict[str, Any]:
        """Resume run ``run_id``, reusing the tasks that already succeeded."""
        rate = _check_rate(fail_rate)
        meta = self._run(run_id)
        if meta is None:
            raise ControlError(f"no run {run_id!r}", HTTPStatus.NOT_FOUND)
        path = self.flows.get(meta.flow)
        if path is None:
            raise ControlError(
                f"flow {meta.flow!r} of run {run_id} is not in the flows folder",
                HTTPStatus.NOT_FOUND,
            )
        return self._launch("resume", meta.flow, path, rate, ["--run", run_id])

    def cancel(self) -> None:
        """Stop the active run and return once its process has exited."""
        with self._lock:
            self._reap()
            child = self._child
            if child is None:
                raise ControlError("no run is active", HTTPStatus.CONFLICT)
            child.cancelled = True
            _interrupt(child.proc)
        try:
            child.proc.wait(self.grace)
        except subprocess.TimeoutExpired:
            _kill_tree(child.proc)
            child.proc.wait()
        with self._lock:
            if self._child is child:
                self._finish(child)

    def status(self) -> dict[str, Any]:
        """``{"flows": [...], "active": {action, flow, run_id} | None, "error": str | None}``."""
        with self._lock:
            self._reap()
            return {
                "flows": list(self.flows),
                "active": self._child.as_json() if self._child else None,
                "error": self._error,
            }

    def shutdown(self) -> None:
        """Cancel the active run, if there is one (when the dashboard stops)."""
        with contextlib.suppress(ControlError):
            self.cancel()

    # ---------------------------------------------------------------- helpers
    def _launch(
        self, action: str, flow: str, path: Path, rate: float, extra: list[str]
    ) -> dict[str, Any]:
        with self._lock:
            self._reap()
            if self._child is not None:
                raise ControlError(f"{self._child.flow} is still running", HTTPStatus.CONFLICT)
            cmd = [sys.executable, "-m", "miniflow", action, str(path), "--db", self.db, "--quiet"]
            env = {**os.environ, "MINIFLOW_FAIL_RATE": f"{rate:g}"}
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
            self._child = _Child(action, flow, proc, stderr, launched_at)
            self._error = None
            return self._child.as_json()

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
        """Note the active run's id and clean up after it if its process has exited."""
        child = self._child
        if child is None:
            return
        if child.run_id is None:
            child.run_id = self._find_run(child)
        if child.proc.poll() is not None:
            self._finish(child)

    def _finish(self, child: _Child) -> None:
        """Called with the lock held once the child has exited."""
        self._child = None
        tail = _tail(child.stderr)
        child.stderr.close()
        code = child.proc.returncode
        run_id = child.run_id or self._find_run(child)
        if run_id is None:
            if not child.cancelled:
                self._error = f"{child.flow} exited with code {code} before starting a run"
                self._error += f":\n{tail}" if tail else ""
            return
        meta = self._run(run_id)
        if meta is not None and meta.finished_at is None:
            self.store.finish_run(run_id, "INTERRUPTED")
            if not child.cancelled:
                self._error = (
                    f"run {run_id} of {child.flow} stopped unexpectedly (exit code {code})"
                )
                self._error += f":\n{tail}" if tail else ""
