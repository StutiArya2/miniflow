"""Dashboard server (standard library only).

Endpoints:

``GET /``                          the single-page dashboard
``GET /static/<file>``             its CSS and JavaScript (package data)
``GET /api/runs``                  recent runs with a count of tasks per state
``GET /api/runs/<id>``             one run: metadata, graph + layout, task rows, attempt log
``GET /api/runs/<id>/events``      Server-Sent Events stream of changes to that run
``GET /api/control``               flows the dashboard can start and the active run
``POST /api/control/run``          ``{"flow": name, "fail_rate": 0..1}``: start a run
``POST /api/control/resume``       ``{"run_id": id, "fail_rate": 0..1}``: resume a run
``POST /api/control/partial``      ``{"flow": name, "step": name}``: run through one step
``POST /api/control/cancel``       ``{}``: stop the active run

The server reads the SQLite :class:`~miniflow.store.StateStore` that the
scheduler writes. It works while a run is in progress (in the same process
with ``miniflow run --ui`` or in another one with ``miniflow ui``) and after it.
It binds to ``127.0.0.1``. It is read-only unless it is given a ``flows`` folder
(``miniflow ui --flows DIR``); only then do the ``/api/control`` endpoints exist,
backed by a :class:`~miniflow.ui.control.RunController`.

A control request is refused unless its ``Host`` is this server, its ``Origin``
(if sent) is this server, and its body is JSON of at most 4 KB. Browsers don't
send JSON to another site without asking the server first, and this server never
agrees, so other web pages can't start runs.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from miniflow.flowfile import FlowFileError
from miniflow.states import State
from miniflow.store import RunRecord, StateStore
from miniflow.ui.author import FlowWorkspace, WorkspaceError
from miniflow.ui.control import ControlError, RunController
from miniflow.ui.layout import layered_layout

__all__ = ["DashboardServer"]

log = logging.getLogger("miniflow.ui")

_STATIC = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "editor.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}
_POLL_SECONDS = 0.25
_HEARTBEAT_SECONDS = 10.0
_RUNS_LIMIT = 200
_MAX_BODY = 4096
_MAX_FLOW_BODY = 262144
_ACTIONS = ("run", "resume", "partial", "cancel")


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


class _ExampleRunner:
    """Runs one bundled example at a time in a background thread (the gallery's Run button)."""

    def __init__(self, db: str, offline: bool) -> None:
        self.db = db
        self.offline = offline
        self._lock = threading.Lock()
        self.current: str | None = None

    def start(self, name: str) -> bool:
        """Start ``name``; False if another example is still running."""
        with self._lock:
            if self.current is not None:
                return False
            self.current = name
        threading.Thread(
            target=self._run, args=(name,), daemon=True, name=f"example-{name}"
        ).start()
        return True

    def _run(self, name: str) -> None:
        from miniflow.examples import load
        from miniflow.executors import ExecutorRouter
        from miniflow.scheduler import Scheduler

        try:
            if self.offline:
                os.environ["MINIFLOW_OFFLINE"] = "1"  # read by the examples; workers inherit it
            _, dag = load(name)
            router = ExecutorRouter()
            with StateStore(self.db) as store:
                scheduler = Scheduler(dag, router=router, store=store, flow=name)
                try:
                    scheduler.run()
                finally:
                    router.shutdown(wait=not scheduler.abandoned_work, cancel_futures=True)
        except Exception:
            log.exception("example %r failed to run", name)
        finally:
            with self._lock:
                self.current = None


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
            elif parts == ["api", "examples"]:
                self._send_json(self.server.examples_json())
            elif parts == ["api", "blocks"] and self.server.workspace is not None:
                self._send_json(self.server.workspace.catalog())
            elif parts == ["api", "flows"] and self.server.workspace is not None:
                self._send_json(self.server.workspace.list())
            elif (
                len(parts) == 3
                and parts[:2] == ["api", "flows"]
                and self.server.workspace is not None
            ):
                try:
                    self._send_json(self.server.workspace.read(parts[2]))
                except (FileNotFoundError, WorkspaceError):
                    self._send_error(HTTPStatus.NOT_FOUND, "flow not found")
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
            elif parts == ["api", "control"] and self.server.controller is not None:
                self._send_json(self.server.controller.status())
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "not found")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the browser went away

    def do_POST(self) -> None:
        self.close_connection = True  # a refused request's body is never read
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        controller = self.server.controller
        try:
            if (
                len(parts) == 3
                and parts[:2] == ["api", "flows"]
                and parts[2] in ("validate", "save", "test")
                and self.server.workspace is not None
            ):
                body = self._control_body(_MAX_FLOW_BODY)
                name = _text(body, "name")
                if "flow" not in body:
                    raise ControlError("flow is required", HTTPStatus.BAD_REQUEST)
                if parts[2] == "test":
                    self._send_json(self.server.workspace.test(name, body["flow"]))
                    return
                if parts[2] == "save":
                    result = self.server.workspace.save(name, body["flow"])
                    if result["valid"] and controller is not None:
                        controller.add_flow(name, self.server.workspace.folder / f"{name}.json")
                else:
                    result = self.server.workspace.validate(name, body["flow"])
                self._send_json(
                    result, HTTPStatus.OK if result["valid"] else HTTPStatus.BAD_REQUEST
                )
                return
            if len(parts) == 4 and parts[:2] == ["api", "examples"] and parts[3] == "run":
                if not self.server.allow_run:
                    self._send_error(HTTPStatus.FORBIDDEN, "this dashboard is read-only")
                elif not self._same_origin():
                    self._send_error(
                        HTTPStatus.FORBIDDEN, "requests must come from the dashboard page"
                    )
                elif parts[2] not in self.server.example_names:
                    self._send_error(HTTPStatus.NOT_FOUND, f"no example {parts[2]!r}")
                elif not self.server.runner.start(parts[2]):
                    self._send_error(HTTPStatus.CONFLICT, "an example is still running")
                else:
                    self._send_json({"started": parts[2]}, HTTPStatus.ACCEPTED)
                return
            if (
                controller is None
                or len(parts) != 3
                or parts[:2] != ["api", "control"]
                or parts[2] not in _ACTIONS
            ):
                self._send_error(HTTPStatus.NOT_FOUND, "not found")
                return
            body = self._control_body()
            if parts[2] == "run":
                active = controller.start(_text(body, "flow"), body.get("fail_rate"))
                self._send_json(active, HTTPStatus.ACCEPTED)
            elif parts[2] == "resume":
                active = controller.resume(_text(body, "run_id"), body.get("fail_rate"))
                self._send_json(active, HTTPStatus.ACCEPTED)
            elif parts[2] == "partial":
                active = controller.partial(_text(body, "flow"), _text(body, "step"))
                self._send_json(active, HTTPStatus.ACCEPTED)
            else:
                controller.cancel()
                self._send_json(controller.status())
        except ControlError as exc:
            self._send_error(exc.status, str(exc))
        except WorkspaceError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except FlowFileError as exc:
            self._send_json({"problems": exc.problems}, HTTPStatus.BAD_REQUEST)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _same_origin(self) -> bool:
        hosts = self.server.hosts
        origin = self.headers.get("Origin")
        return (
            self.headers.get("Host", "").lower() in hosts
            and (origin is None or origin.lower() in {f"http://{h}" for h in hosts})
            and self.headers.get("X-MiniFlow-Action") == "run"
        )

    def _control_body(self, limit: int = _MAX_BODY) -> dict[str, Any]:
        """Refuse requests that didn't come from the dashboard page, then parse the body."""
        hosts = self.server.hosts
        if self.headers.get("Host", "").lower() not in hosts:
            raise ControlError("unexpected Host header", HTTPStatus.FORBIDDEN)
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() not in {f"http://{h}" for h in hosts}:
            raise ControlError("requests from other sites are not allowed", HTTPStatus.FORBIDDEN)
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ControlError("the body must be JSON", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise ControlError("Content-Length is required", HTTPStatus.LENGTH_REQUIRED) from None
        if not 0 <= length <= limit:
            raise ControlError(
                f"the body is over {limit} bytes", HTTPStatus.REQUEST_ENTITY_TOO_LARGE
            )
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise ControlError("the body is not valid JSON", HTTPStatus.BAD_REQUEST) from None
        if not isinstance(body, dict):
            raise ControlError("the body must be a JSON object", HTTPStatus.BAD_REQUEST)
        return body

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


def _text(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str):
        raise ControlError(f"{key} must be a string", HTTPStatus.BAD_REQUEST)
    return value


class DashboardServer(ThreadingHTTPServer):
    """``DashboardServer(db_path, port=8765).start()`` serves in a background thread.

    If ``port`` is taken, the next free port (up to ``port + 20``) is used; pass
    ``port=0`` for any free port. :attr:`url` gives the address actually bound.
    With ``flows=DIR`` the dashboard can also start, cancel and resume runs of the
    flow files in ``DIR``; :meth:`stop` then cancels the run it started, if any.
    """

    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        db: str | Path,
        port: int = 8765,
        host: str = "127.0.0.1",
        flows: str | Path | None = None,
        *,
        allow_run: bool = False,
        offline: bool = False,
    ) -> None:
        if str(db) == ":memory:":
            raise ValueError("the dashboard needs a database file, not ':memory:'")
        from miniflow.examples import catalog

        self.allow_run = allow_run
        self.offline = offline
        self.runner = _ExampleRunner(str(db), offline)
        self.catalog = catalog()
        self.example_names = {ex["name"] for ex in self.catalog}
        self.store = StateStore(db)
        self.api = _Api(self.store)
        try:
            self.workspace = None if flows is None else FlowWorkspace(flows)
            self.controller = None if flows is None else RunController(flows, self.store)
        except OSError:
            self.store.close()
            raise
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

    def examples_json(self) -> dict[str, Any]:
        return {
            "allowRun": self.allow_run,
            "offline": self.offline,
            "running": self.runner.current,
            "examples": self.catalog,
        }

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host!s}:{port}"

    @property
    def hosts(self) -> set[str]:
        """``Host`` header values that address this server."""
        host, port = self.server_address[:2]
        return {f"{host!s}:{port}", f"localhost:{port}"}

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
        if self.controller is not None:
            self.controller.shutdown()
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
