"""Read-only dashboard server (standard library only).

Endpoints (all ``GET``):

``/``                          the single-page dashboard
``/static/<file>``             its CSS and JavaScript (package data)
``/api/runs``                  recent runs with a count of tasks per state
``/api/runs/<id>``             one run: metadata, graph + layout, task rows, attempt log
``/api/runs/<id>/events``      Server-Sent Events stream of changes to that run

The server reads the SQLite :class:`~miniflow.store.StateStore` that the
scheduler writes. It works while a run is in progress (in the same process
with ``miniflow run --ui`` or in another one with ``miniflow ui``) and after it.
It binds to ``127.0.0.1`` and cannot change anything.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from miniflow.states import State
from miniflow.store import RunRecord, StateStore
from miniflow.ui.layout import layered_layout

__all__ = ["DashboardServer"]

log = logging.getLogger("miniflow.ui")

_STATIC = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}
_POLL_SECONDS = 0.25
_HEARTBEAT_SECONDS = 10.0
_RUNS_LIMIT = 200


def _static(name: str) -> bytes:
    return resources.files("miniflow.ui").joinpath("static", name).read_bytes()


def _run_json(r: RunRecord) -> dict[str, Any]:
    return asdict(r)


class _Api:
    """JSON views over the store, shared by the request handlers."""

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def run(self, run_id: str) -> RunRecord | None:
        return next((r for r in self.store.list_runs() if r.run_id == run_id), None)

    def runs(self) -> list[dict[str, Any]]:
        out = []
        for r in self.store.list_runs()[:_RUNS_LIMIT]:
            counts = dict.fromkeys((s.value for s in State), 0)
            for s in self.store.get_states(r.run_id).values():
                counts[s.value] += 1
            out.append({**_run_json(r), "counts": counts, "total": sum(counts.values())})
        return out

    def detail(self, run_id: str) -> dict[str, Any] | None:
        meta = self.run(run_id)
        if meta is None:
            return None
        graph = self.store.get_graph(run_id)
        tasks = self.store.changes_since(run_id, 0.0)
        return {
            "run": _run_json(meta),
            "graph": graph,
            "layout": layered_layout(graph),
            "tasks": [asdict(t) for t in tasks],
            "attempts": [asdict(a) for a in self.store.attempts(run_id)],
            "since": max((t.updated_at for t in tasks), default=0.0),
            "now": time.time(),
        }


class _Handler(BaseHTTPRequestHandler):
    server: DashboardServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), format % args)

    # ---------------------------------------------------------------- routing
    def do_GET(self) -> None:
        url = urlsplit(self.path)
        parts = [p for p in url.path.split("/") if p]
        try:
            if not parts:
                self._send_bytes(_static("index.html"), _STATIC["index.html"])
            elif parts[0] == "static" and len(parts) == 2 and parts[1] in _STATIC:
                self._send_bytes(_static(parts[1]), _STATIC[parts[1]])
            elif parts == ["api", "runs"]:
                self._send_json(self.server.api.runs())
            elif len(parts) == 3 and parts[:2] == ["api", "runs"]:
                detail = self.server.api.detail(parts[2])
                if detail is None:
                    self._send_error(HTTPStatus.NOT_FOUND, f"no run {parts[2]!r}")
                else:
                    self._send_json(detail)
            elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "events":
                since = float(parse_qs(url.query).get("since", ["0"])[0] or 0)
                self._stream(parts[2], since)
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "not found")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the browser went away

    # --------------------------------------------------------------- replies
    def _send_bytes(self, body: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(data, separators=(",", ":")).encode()
        self._send_bytes(body, "application/json", status)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status)

    def _stream(self, run_id: str, since: float) -> None:
        """Push task changes, attempts and run status until the run ends."""
        api = self.server.api
        meta = api.run(run_id)
        if meta is None:
            self._send_error(HTTPStatus.NOT_FOUND, f"no run {run_id!r}")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        n_attempts = -1
        last_meta: dict[str, Any] | None = None
        self._last_write = time.monotonic()
        while not self.server.stopping.is_set():
            changes = api.store.changes_since(run_id, since)
            if changes:
                since = max(t.updated_at for t in changes)
                self._event("tasks", {"tasks": [asdict(t) for t in changes], "since": since})
            attempts = api.store.attempts(run_id)
            if len(attempts) != n_attempts:
                n_attempts = len(attempts)
                self._event("attempts", [asdict(a) for a in attempts])
            meta = api.run(run_id)
            meta_json = _run_json(meta) if meta else None
            if meta_json != last_meta:
                last_meta = meta_json
                self._event("run", meta_json)
            if meta is not None and meta.finished_at is not None and not changes:
                self._event("done", {"status": meta.status})
                return
            if time.monotonic() - self._last_write > _HEARTBEAT_SECONDS:
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                self._last_write = time.monotonic()
            self.server.stopping.wait(_POLL_SECONDS)

    def _event(self, name: str, data: Any) -> None:
        payload = json.dumps(data, separators=(",", ":"))
        self.wfile.write(f"event: {name}\ndata: {payload}\n\n".encode())
        self.wfile.flush()
        self._last_write = time.monotonic()


class DashboardServer(ThreadingHTTPServer):
    """``DashboardServer(db_path, port=8765).start()`` serves in a background thread.

    If ``port`` is taken, the next free port (up to ``port + 20``) is used; pass
    ``port=0`` for any free port. :attr:`url` gives the address actually bound.
    """

    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, db: str | Path, port: int = 8765, host: str = "127.0.0.1") -> None:
        if str(db) == ":memory:":
            raise ValueError("the dashboard needs a database file, not ':memory:'")
        self.store = StateStore(db)
        self.api = _Api(self.store)
        self.stopping = threading.Event()
        self._thread: threading.Thread | None = None
        last_error: OSError | None = None
        for candidate in [port] if port == 0 else range(port, port + 21):
            try:
                super().__init__((host, candidate), _Handler)
                break
            except OSError as exc:  # address in use
                last_error = exc
        else:
            self.store.close()
            raise OSError(f"no free port in {port}-{port + 20}") from last_error

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host!s}:{port}"

    def start(self) -> DashboardServer:
        self._thread = threading.Thread(
            target=self.serve_forever,
            kwargs={"poll_interval": 0.2},
            daemon=True,
            name="miniflow-ui",
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self.stopping.set()
        if self._thread is not None:
            self.shutdown()
            self._thread.join(timeout=5)
        self.server_close()
        self.store.close()

    def __enter__(self) -> DashboardServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
