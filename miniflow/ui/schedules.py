"""Launch flow-file schedules through the same controller as manual dashboard runs."""

from __future__ import annotations

import threading
import time
from dataclasses import asdict
from typing import Any

from miniflow.flowfile import FlowFileError, load_json_flow
from miniflow.schedule import Schedule
from miniflow.store import StateStore
from miniflow.ui.control import ControlError, RunController


class ScheduleService:
    """Single local clock loop; missed and failed launches are persisted."""

    def __init__(self, controller: RunController, store: StateStore) -> None:
        self.controller = controller
        self.store = store
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._items: dict[tuple[str, str], tuple[Schedule, float, float]] = {}
        self._seen_files: dict[str, tuple[str, int]] = {}
        self._problems: dict[str, str] = {}
        self._last_launch: tuple[str, str, float] | None = None

    def start(self) -> None:
        self.refresh()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="miniflow-schedules")
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

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
                    spec = load_json_flow(path)
                except (OSError, FlowFileError, ValueError) as exc:
                    self._problems[flow] = str(exc)
                    continue
                self._problems.pop(flow, None)
                for schedule in spec.schedules:
                    key = (flow, schedule.name)
                    active.add(key)
                    old = self._items.get(key)
                    if old is None or old[0] != schedule or old[2] != modified:
                        self._items[key] = (schedule, schedule.next_after(now), modified)
            for key in list(self._items):
                if key not in active:
                    del self._items[key]
            for flow in list(self._seen_files):
                if flow not in self.controller.flows:
                    del self._seen_files[flow]
                    self._problems.pop(flow, None)

    def tick(self, now: float | None = None) -> None:
        """Launch due schedules once; exposed for deterministic clock tests."""
        now = time.time() if now is None else now
        self.refresh(now)
        with self._lock:
            status = self.controller.status()
            if self._last_launch is not None and status["active"] is None and status["error"]:
                flow, name, due = self._last_launch
                self.store.record_schedule_event(flow, name, due, "FAILED", status["error"])
                self._last_launch = None
            elif status["active"] is None:
                self._last_launch = None
            for (flow, name), (schedule, due, modified) in list(self._items.items()):
                if due > now:
                    continue
                self._items[(flow, name)] = (schedule, schedule.next_after(now), modified)
                if status["active"] is not None:
                    self.store.record_schedule_event(
                        flow, name, due, "MISSED", f"{status['active']['flow']} was still running"
                    )
                    continue
                try:
                    self.controller.start(flow, 0.0)
                except (ControlError, OSError) as exc:
                    self.store.record_schedule_event(flow, name, due, "FAILED", str(exc))
                else:
                    self.store.record_schedule_event(flow, name, due, "LAUNCHED")
                    self._last_launch = (flow, name, due)
                    status = self.controller.status()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            schedules = [
                {"flow": flow, **asdict(schedule), "next_run": due}
                for (flow, _), (schedule, due, _) in sorted(self._items.items())
            ]
            return {
                "schedules": schedules,
                "problems": dict(self._problems),
                "events": [asdict(event) for event in self.store.list_schedule_events()],
            }

    def _loop(self) -> None:
        while not self._stopping.wait(0.25):
            self.tick()
