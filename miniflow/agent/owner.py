"""The MCP server's link to the dashboard that runs flows (standard library only).

Exactly one dashboard runs flows for a state database (:mod:`miniflow.ui.owner`). The MCP
server never runs flows itself; :class:`DashboardLink` sends run requests to that dashboard:

    DashboardLink(flows, db, *, start=True, timeout=10.0)
    .url() -> str                     the live dashboard's address, starting one if needed
    .run(flow, environment) -> dict   POST /api/control/run: the run, or {"queued": True}
    .cancel(run_id) -> dict           POST /api/control/cancel
    .control(*, start=True) -> dict   GET /api/control: running and queued runs; with
                                      start=False and no dashboard, {"running": [], "queued": []}
    .close() -> None                  stops the dashboard this link started, if any

A dashboard serving another flows folder for the same database is a :class:`LinkError`. A
dashboard the link starts (``python -m miniflow serve --no-browser``) is its child: ``close``
interrupts it as Ctrl+C would, so its active runs end INTERRUPTED and can be resumed. If two
links start one at the same moment, one wins the owner file and both use it.
"""

from __future__ import annotations

import http.client
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlsplit

from miniflow.ui.control import _CREATIONFLAGS, _NEW_SESSION, _interrupt, _kill_tree
from miniflow.ui.owner import Owner, is_live, read_owner

__all__ = ["DashboardLink", "LinkError"]

_GRACE = 15.0


class LinkError(Exception):
    """The dashboard that runs flows can't be reached or started."""


class DashboardLink:
    def __init__(
        self, flows: str | Path, db: str | Path, *, start: bool = True, timeout: float = 10.0
    ) -> None:
        self.flows = Path(flows).resolve()
        self.db = Path(db).resolve()
        self.start = start
        self.timeout = timeout
        self._child: subprocess.Popen[bytes] | None = None
        self._stderr: IO[bytes] | None = None
        self._lock = threading.Lock()

    def _live(self) -> Owner | None:
        owner = read_owner(self.db)
        return owner if owner is not None and is_live(owner) else None

    def _same_folder(self, owner: Owner) -> str:
        if Path(owner.flows) != self.flows:
            raise LinkError(
                f"the dashboard at {owner.url} runs flows from {owner.flows}, not {self.flows}; "
                "one database can't serve both folders"
            )
        return owner.url

    def url(self) -> str:
        with self._lock:
            owner = self._live()
            if owner is not None:
                return self._same_folder(owner)
            if not self.start:
                raise LinkError(
                    f"no dashboard is running for {self.db}; start one with: "
                    f"miniflow serve --flows {self.flows} --db {self.db}"
                )
            if self._child is None or self._child.poll() is not None:
                self._spawn()
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                owner = self._live()
                if owner is not None:
                    return self._same_folder(owner)
                time.sleep(0.1)  # if our child lost the race, the winner's file appears here
            raise LinkError(f"the dashboard didn't start within {self.timeout:g} s{self._tail()}")

    def _spawn(self) -> None:
        if self._stderr is not None:
            self._stderr.close()
        self._stderr = tempfile.TemporaryFile()  # noqa: SIM115 - read in _tail, closed in close
        command = [
            sys.executable, "-m", "miniflow", "serve",
            "--flows", str(self.flows), "--db", str(self.db), "--no-browser", "--port", "0",
        ]  # fmt: skip
        try:
            self._child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,  # stdout belongs to the MCP protocol
                stderr=self._stderr,
                creationflags=_CREATIONFLAGS,
                start_new_session=_NEW_SESSION,
            )
        except OSError as exc:
            self._stderr.close()
            self._stderr = None
            raise LinkError(f"couldn't start the dashboard: {exc}") from None

    def _tail(self) -> str:
        if self._stderr is None:
            return ""
        self._stderr.seek(0)
        text = self._stderr.read().decode(errors="replace").strip()
        return f":\n{text[-2000:]}" if text else ""

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None, url: str | None = None
    ) -> Any:
        address = urlsplit(url or self.url())
        conn = http.client.HTTPConnection(address.hostname or "127.0.0.1", address.port, timeout=30)
        data = None if body is None else json.dumps(body).encode()
        try:
            conn.request(
                method,
                path,
                body=data,
                headers={"Content-Type": "application/json"} if data else {},
            )
            response = conn.getresponse()
            payload = json.loads(response.read() or b"null")
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise LinkError(f"couldn't reach the dashboard: {exc}") from None
        finally:
            conn.close()
        if response.status >= 400:
            message = payload.get("error") if isinstance(payload, dict) else None
            raise LinkError(message or f"the dashboard answered {response.status}")
        return payload

    def run(self, flow: str, environment: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"flow": flow, "fail_rate": 0}
        if environment is not None:
            body["environment"] = environment
        result: dict[str, Any] = self._request("POST", "/api/control/run", body)
        return result

    def cancel(self, run_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._request("POST", "/api/control/cancel", {"run_id": run_id})
        return result

    def control(self, *, start: bool = True) -> dict[str, Any]:
        if not start:
            owner = self._live()
            if owner is None:
                return {"running": [], "queued": []}
            result: dict[str, Any] = self._request(
                "GET", "/api/control", url=self._same_folder(owner)
            )
            return result
        result = self._request("GET", "/api/control")
        return result

    def close(self) -> None:
        child, self._child = self._child, None
        if child is not None and child.poll() is None:
            _interrupt(child)
            try:
                child.wait(_GRACE)
            except subprocess.TimeoutExpired:
                _kill_tree(child)
                child.wait()
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None
