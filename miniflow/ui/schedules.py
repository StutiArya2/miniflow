"""Launch flow-file schedules and triggers through the same controller as manual runs.

Time-based schedules (interval, daily, cron) fire at their due times; ``catch_up`` runs one
run after the dashboard was down through launches. ``watch`` schedules start a run when new
files appear and have stopped growing; ``webhook`` schedules start one when a caller presents
the secret token. Every launch goes through the controller's queue, so a busy slot makes the
run wait instead of being lost, and every outcome is recorded as a schedule event
(``LAUNCHED``, ``QUEUED``, ``MISSED``, ``FAILED``).
"""

from __future__ import annotations

import glob
import hmac
import json
import math
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any

from miniflow.schedule import Schedule, parse_schedules
from miniflow.store import StateStore
from miniflow.ui.control import ControlError, RunController

_CATCH_UP_LIMIT = 1000


@dataclass
class _Watch:
    """What a ``watch`` schedule has seen so far."""

    schedule: Schedule
    folder: Path
    next_poll: float = 0.0
    known: set[str] | None = None  # None until the first look records what is already there
    pending: dict[str, tuple[int, int]] = field(default_factory=dict)  # path -> (size, mtime)


class ScheduleService:
    """Single local clock loop; every launch, wait, miss and failure is persisted."""

    def __init__(self, controller: RunController, store: StateStore) -> None:
        self.controller = controller
        self.store = store
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._items: dict[tuple[str, str], tuple[Schedule, float, float]] = {}
        self._watches: dict[tuple[str, str], _Watch] = {}
        self._catch_ups: list[tuple[str, str, float, int, bool]] = []
        self._seen_files: dict[str, tuple[str, int]] = {}
        self._problems: dict[str, str] = {}

    def start(self) -> None:
        self.refresh()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="miniflow-schedules")
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # --------------------------------------------------------------- reading
    def refresh(self, now: float | None = None) -> None:
        """Read changed flow files; newly saved schedules need no server restart."""
        now = time.time() if now is None else now
        with self._lock:
            active: set[tuple[str, str]] = set()
            for flow, path in list(self.controller.flows.items()):
                if path.suffix != ".json" or not path.is_file():
                    continue
                try:
                    modified = path.stat().st_mtime_ns
                    marker = (str(path), modified)
                    if self._seen_files.get(flow) == marker:
                        active.update(key for key in self._items if key[0] == flow)
                        continue
                    self._seen_files[flow] = marker
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(raw, dict):
                        raise ValueError("a flow file should be a JSON object")
                    schedules, problems = parse_schedules(raw.get("schedules"))
                    if problems:
                        raise ValueError("; ".join(problems))
                except (OSError, ValueError) as exc:
                    self._problems[flow] = str(exc)
                    continue
                self._problems.pop(flow, None)
                for schedule in schedules:
                    key = (flow, schedule.name)
                    active.add(key)
                    self._register(key, schedule, path, modified, now)
            for key in list(self._items):
                if key not in active:
                    del self._items[key]
                    self._watches.pop(key, None)
            for flow in list(self._seen_files):
                if flow not in self.controller.flows:
                    del self._seen_files[flow]
                    self._problems.pop(flow, None)

    def _register(
        self, key: tuple[str, str], schedule: Schedule, path: Path, modified: int, now: float
    ) -> None:
        old = self._items.get(key)
        if old is not None and old[0] == schedule and old[2] == modified:
            return
        due = schedule.next_after(now) if schedule.timed else math.inf
        self._items[key] = (schedule, due, modified)
        if schedule.kind == "watch":
            if old is None or old[0] != schedule:
                self._watches[key] = _Watch(schedule, path.parent)
        else:
            self._watches.pop(key, None)
        if old is None and schedule.catch_up and schedule.timed:
            self._plan_catch_up(key, schedule, now)

    def _plan_catch_up(self, key: tuple[str, str], schedule: Schedule, now: float) -> None:
        """Note that launches were missed while nothing was running, if any were."""
        last = self.store.last_schedule_due(*key)
        if last is None:
            return  # never ran: there is nothing to catch up on
        count, latest, due = 0, last, schedule.next_after(last)
        while due <= now and count < _CATCH_UP_LIMIT:
            count, latest, due = count + 1, due, schedule.next_after(due)
        if count:
            self._catch_ups.append((*key, latest, count, count >= _CATCH_UP_LIMIT))

    # ---------------------------------------------------------------- firing
    def tick(self, now: float | None = None) -> None:
        """Do whatever is due once; exposed for deterministic clock tests."""
        now = time.time() if now is None else now
        self.refresh(now)
        with self._lock:
            catch_ups, self._catch_ups = self._catch_ups, []
            for flow, name, due, count, capped in catch_ups:
                noun = "run" if count == 1 else "runs"
                note = f"caught up {count}{'+' if capped else ''} missed {noun}"
                self._fire(flow, name, due, "schedule", note=note)
            for (flow, name), (schedule, due, modified) in list(self._items.items()):
                if not schedule.timed or due > now:
                    continue
                self._items[(flow, name)] = (schedule, schedule.next_after(now), modified)
                self._fire(flow, name, due, "schedule")
            for (flow, name), watch in list(self._watches.items()):
                self._poll(flow, name, watch, now)

    def _poll(self, flow: str, name: str, watch: _Watch, now: float) -> None:
        if now < watch.next_poll:
            return
        watch.next_poll = now + (watch.schedule.poll_seconds or 5.0)
        assert watch.schedule.watch is not None
        found = glob.glob(watch.schedule.watch, root_dir=watch.folder, recursive=True)
        current: dict[str, tuple[int, int]] = {}
        for relative in found:
            path = os.path.abspath(watch.folder / relative)
            try:
                if os.path.isfile(path):
                    info = os.stat(path)
                    current[path] = (info.st_size, info.st_mtime_ns)
            except OSError:
                continue  # it vanished between the listing and now
        if watch.known is None:
            watch.known = set(current)
            return
        watch.known &= set(current)  # a file that went away may come back and count again
        ready: list[str] = []
        pending: dict[str, tuple[int, int]] = {}
        for path in sorted(current):
            if path in watch.known:
                continue
            if watch.pending.get(path) == current[path]:
                ready.append(path)  # unchanged since the last look: finished being written
            else:
                pending[path] = current[path]
        watch.pending = pending
        if ready:
            watch.known.update(ready)
            names = ", ".join(os.path.basename(p) for p in ready)
            noun = "file" if len(ready) == 1 else "files"
            self._fire(
                flow, name, now, "watch",
                trigger={"type": "file", "files": ready}, note=f"{len(ready)} new {noun}: {names}",
            )  # fmt: skip

    def _fire(
        self,
        flow: str,
        name: str,
        due: float,
        kind: str,
        *,
        trigger: dict[str, Any] | None = None,
        note: str = "",
        strict: bool = False,
    ) -> str:
        """Queue a run for a schedule or trigger and record what happened to it.

        Returns ``"started"``, ``"queued"`` or ``"missed"``; with ``strict`` a refusal is raised
        (webhook callers are told) as well as recorded.
        """
        queued_at = time.time()
        running = self.controller.status()["active"]

        def started() -> None:
            waited = time.time() - queued_at
            extra = f"after waiting {waited:.0f} s" if waited >= 1 else ""
            message = ", ".join(part for part in (note, extra) if part)
            self.store.record_schedule_event(flow, name, due, "LAUNCHED", message or None)

        def finished(error: str | None) -> None:
            if error:
                self.store.record_schedule_event(flow, name, due, "FAILED", error)

        try:
            state = self.controller.enqueue(
                flow, source=f"{kind}:{flow}/{name}", trigger=trigger,
                on_start=started, on_finish=finished,
            )  # fmt: skip
        except ControlError as exc:
            missed = exc.status in (HTTPStatus.CONFLICT, HTTPStatus.TOO_MANY_REQUESTS)
            self.store.record_schedule_event(
                flow, name, due, "MISSED" if missed else "FAILED", str(exc)
            )
            if strict:
                raise
            return "missed"
        except OSError as exc:
            self.store.record_schedule_event(flow, name, due, "FAILED", str(exc))
            if strict:
                raise ControlError(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR) from exc
            return "missed"
        if state == "queued":
            why = f"{running['flow']} was running" if running else "earlier runs were waiting"
            self.store.record_schedule_event(
                flow, name, due, "QUEUED", ", ".join(p for p in (note, why) if p)
            )
        return state

    # -------------------------------------------------------------- webhooks
    def fire_webhook(self, flow: str, name: str, token: str, body: Any) -> str:
        """Start the run for a ``webhook`` schedule if ``token`` is right; refusals are raised."""
        with self._lock:
            item = self._items.get((flow, name))
        if item is None or item[0].kind != "webhook":
            raise ControlError(f"no webhook {name!r} in flow {flow!r}", HTTPStatus.NOT_FOUND)
        variable = item[0].webhook or ""
        secret = os.environ.get(variable, "")
        if not secret:
            raise ControlError(
                f"this webhook's token variable {variable} is not set on the dashboard",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        if not token or not hmac.compare_digest(token.encode(), secret.encode()):
            raise ControlError("wrong or missing token", HTTPStatus.UNAUTHORIZED)
        with self._lock:
            return self._fire(
                flow, name, time.time(), "webhook",
                trigger={"type": "webhook", "body": body}, note="webhook call", strict=True,
            )  # fmt: skip

    # --------------------------------------------------------------- reading
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            schedules = []
            for (flow, _), (schedule, due, _) in sorted(self._items.items()):
                entry: dict[str, Any] = {"flow": flow, **asdict(schedule), "kind": schedule.kind}
                entry["next_run"] = due if math.isfinite(due) else None
                if schedule.kind == "webhook":
                    entry["configured"] = bool(os.environ.get(schedule.webhook or ""))
                    entry["url"] = f"/api/hooks/{flow}/{schedule.name}"
                schedules.append(entry)
            return {
                "schedules": schedules,
                "problems": dict(self._problems),
                "events": [asdict(event) for event in self.store.list_schedule_events()],
                "queued": self.controller.status()["queued"],
            }

    def _loop(self) -> None:
        while not self._stopping.wait(0.25):
            self.tick()
