# MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `miniflow mcp --flows DIR` lets an MCP client build, validate, save and run JSON flows and draft new blocks, which a person approves in the Editor. Runs from agents, the Run button and schedules all go through one dashboard per database.

**Architecture:** Five units, built bottom-up. (1) `RunController` learns flows saved after startup. (2) `miniflow/ui/owner.py`: a dashboard with a flows folder claims `<db>.owner.json`, so only one process launches runs for a database. (3) `miniflow/agent/drafts.py`: static (`ast`-only) checks, draft files, approve and discard, used by the dashboard endpoints, the Editor's Pending blocks panel and `miniflow approve-block`. (4) `miniflow/agent/tools.py`: the tool layer over `FlowWorkspace`, the state store and `miniflow/agent/owner.py`'s `DashboardLink`, which forwards runs to the owning dashboard (starting one if needed). (5) `miniflow/agent/mcp.py`: hand-written JSON-RPC over stdio, plus the `mcp` CLI command.

**Tech Stack:** Python ≥ 3.11 standard library only (`ast`, `http.client`, `json`, `subprocess`, `hashlib`, `secrets`); pytest, hypothesis, pytest-timeout (60 s); plain JavaScript in `miniflow/ui/static/`.

**Spec:** `docs/superpowers/specs/2026-10-14-mcp-server-design.md`

## Global Constraints

- The core package `miniflow/` is standard-library only. Never add to `[project].dependencies`.
- Python ≥ 3.11, full type hints; `uv run mypy` (strict) must pass. `uv run ruff check .` and `uv run ruff format --check .` must pass.
- Every module docstring is its interface contract. New modules start with one that lists their API.
- Process-pool and subprocess code must work under the **spawn** start method (Windows CI). Worker functions at module level, never in a test's local scope.
- Every test stays under the 60 s pytest-timeout.
- Commits: no `Co-Authored-By` trailers, no "Generated with Claude Code" lines (CLAUDE.md).
- The dashboard binds to `127.0.0.1`; control requests keep the Host, Origin and body-size checks of `_control_body`.
- Environment values (`environments/*.env`, `*.json` values) are never returned by any tool.
- Nothing in a block draft is imported or run before a person approves it.
- `stdout` of `miniflow mcp` carries only JSON-RPC messages.

## Review Focus

1. **Relative `--db` under Claude Desktop.** Its working directory is unpredictable, so `--db miniflow.db` may point at a different database from the person's dashboard. Expected: `run_flow` results name the absolute `database` and `dashboard` URL, and the README registration uses absolute paths. Test in Task 8.
2. **Windows console encoding.** The default stdio encoding there isn't UTF-8, so `₹` in a tool argument would be mangled. Expected: `miniflow mcp` reads and writes UTF-8. Test in Task 9 (subprocess round trip with `₹`).
3. **An MCP server killed without cleanup** (the client force-quits). Expected: the dashboard it started keeps running and the next MCP server reuses it rather than starting a second one. Test in Task 8.
4. **A huge or pathological draft** (megabytes of source, deeply nested expressions). Expected: refused with a clear check failure, never a crash. Tests in Task 3 (size limit and hypothesis over arbitrary text).
5. **A stale owner file whose port is now taken by an unrelated HTTP server.** Expected: treated as dead because the token doesn't match, and replaced. Test in Task 2.

---

## File Structure

| File | Responsibility |
|---|---|
| `miniflow/ui/control.py` (modify) | `_flow_path`: find `DIR/<name>.json` saved after startup |
| `miniflow/ui/owner.py` (create) | owner file: `claim`, `release`, `read_owner`, `is_live`, `OwnerError` |
| `miniflow/ui/server.py` (modify) | claim/release on start/stop, `GET /api/owner`, draft endpoints, serve `editor-drafts.js` |
| `miniflow/ui/author.py` (modify) | `FlowWorkspace.file_version`, `save(..., expect_version, base_version)`, `VersionConflict` |
| `miniflow/agent/__init__.py` (create) | package docstring |
| `miniflow/agent/drafts.py` (create) | `check_source`, `write_draft`, `list_drafts`, `approve`, `discard`, `DraftError` |
| `miniflow/agent/owner.py` (create) | `DashboardLink`, `LinkError`: find or start the owning dashboard, forward run requests |
| `miniflow/agent/tools.py` (create) | `Tool`, `ToolError`, `UnknownTool`, `RunLink`, `Toolbox`, `available`, `call`, every tool |
| `miniflow/agent/mcp.py` (create) | `McpServer`: JSON-RPC 2.0 over stdio |
| `miniflow/cli.py` (modify) | `mcp` and `approve-block` subcommands |
| `miniflow/ui/static/editor-drafts.js` (create) | Pending blocks panel |
| `miniflow/ui/static/index.html`, `editor.js`, `style.css` (modify) | button, dialog, init, styles |
| `README.md`, `docs/PLAN.md` (modify) | how to connect an agent; milestone entry |
| `tests/test_control.py`, `tests/test_editor_v2.py` (modify) | rescan; versions |
| `tests/test_owner.py`, `tests/test_agent_drafts.py`, `tests/test_agent_tools.py`, `tests/test_agent_owner.py`, `tests/test_agent_mcp.py` (create) | one per new module |

---

### Task 1: RunController finds flows saved after startup

**Files:**
- Modify: `miniflow/ui/control.py` (constructor at `:138`, `start` `:168`, `resume` `:177`, `partial` `:191`, `enqueue` `:220`)
- Test: `tests/test_control.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `RunController.folder: Path` (resolved flows folder). `start`, `partial`, `enqueue` and `resume` accept a JSON flow that appeared in the folder after the controller was created.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_control.py`; add `import json` at the top)

```python
LATER = {"steps": {"hello": {"block": "log", "settings": {"message": "hi"}}}}


def test_flow_saved_after_startup_can_be_run(controller, store, flows_dir):
    (flows_dir / "later.json").write_text(json.dumps(LATER), "utf-8")
    controller.start("later", 0)
    wait_until(lambda: idle(controller))
    assert status_of(store, run_of(store, "later")) == "SUCCESS"
    assert "later" in controller.flows


@pytest.mark.parametrize("name", ["../outside", "_private", "a/b", "", "later.json"])
def test_names_that_are_not_flows_in_the_folder_are_still_refused(controller, flows_dir, name):
    (flows_dir.parent / "outside.json").write_text(json.dumps(LATER), "utf-8")
    (flows_dir / "_private.json").write_text(json.dumps(LATER), "utf-8")
    with pytest.raises(ControlError):
        controller.start(name, 0)


def test_a_symlinked_flow_saved_later_is_refused(controller, flows_dir, tmp_path):
    target = tmp_path / "real.json"
    target.write_text(json.dumps(LATER), "utf-8")
    try:
        (flows_dir / "linked.json").symlink_to(target)
    except OSError:
        pytest.skip("symlinks need extra rights on this system")
    with pytest.raises(ControlError):
        controller.start("linked", 0)
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_control.py -k "saved_after_startup or still_refused or symlinked_flow" -v`
Expected: `test_flow_saved_after_startup_can_be_run` FAILS with `ControlError: no flow 'later' in the flows folder`; the refusal tests pass already.

- [ ] **Step 3: Implement**

In `RunController.__init__`, after `folder = Path(flows_dir)` and the `is_dir` check, add:

```python
        self.folder = folder.resolve()
```

Add this method in the helpers section (after `_can_start`):

```python
    def _flow_path(self, flow: str) -> Path | None:
        """The flow's file. A JSON flow saved after startup (by the Editor of another
        process, or an agent) is found here and remembered."""
        path = self.flows.get(flow)
        if path is not None or not _ENV_NAME.fullmatch(flow) or flow.startswith("_"):
            return path
        candidate = self.folder / f"{flow}.json"
        if not candidate.is_file() or candidate.is_symlink():
            return None
        if candidate.resolve().parent != self.folder:
            return None
        self.add_flow(flow, candidate)
        return self.flows[flow]
```

Replace `self.flows.get(flow)` with `self._flow_path(flow)` in `start`, `partial` and `enqueue`, and `self.flows.get(meta.flow)` with `self._flow_path(meta.flow)` in `resume`. (`_ENV_NAME` is the existing `[A-Za-z_][A-Za-z0-9_-]*\Z` pattern, the same rule `FlowWorkspace` uses for flow names.) Update the module docstring's first paragraph by appending: "A JSON flow saved into the folder after startup is found the first time it is asked for."

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_control.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add miniflow/ui/control.py tests/test_control.py
git commit -m "Run control: find JSON flows saved after the dashboard started"
```

---

### Task 2: One dashboard runs flows per database (owner file)

**Files:**
- Create: `miniflow/ui/owner.py`
- Modify: `miniflow/ui/server.py` (`DashboardServer.__init__` `:567`, `start` `:631`, `stop` `:643`, `do_GET` routing `:172`)
- Test: `tests/test_owner.py`

**Interfaces:**
- Consumes: `DashboardServer.url`, `.controller`, `.workspace`, `.store.path`.
- Produces (used by Task 8):
  - `owner_path(db: str | os.PathLike[str]) -> Path`
  - `@dataclass(frozen=True) class Owner: pid: int; url: str; flows: str; token: str; started_at: float`
  - `read_owner(db) -> Owner | None`
  - `is_live(owner: Owner, timeout: float = 2.0) -> bool`
  - `claim(db, url: str, flows: str | os.PathLike[str]) -> Owner` (raises `OwnerError(ValueError)`)
  - `release(db, owner: Owner) -> None`
  - `DashboardServer.owner: Owner | None`; `GET /api/owner` → `{"token": str | None}`

- [ ] **Step 1: Write the failing tests** (`tests/test_owner.py`)

```python
"""One dashboard runs flows per state database: the owner file."""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from miniflow.cli import main
from miniflow.ui import DashboardServer
from miniflow.ui.owner import Owner, OwnerError, claim, is_live, owner_path, read_owner, release
from tests.test_author import request

HELLO = {"steps": {"hello": {"block": "log", "settings": {"message": "hi"}}}}


@pytest.fixture
def flows(tmp_path: Path) -> Path:
    d = tmp_path / "flows"
    d.mkdir()
    (d / "hello.json").write_text(json.dumps(HELLO), "utf-8")
    return d


def write_owner(db: Path, url: str, token: str = "old", flows: str = "/x") -> None:
    owner = Owner(pid=1, url=url, flows=flows, token=token, started_at=time.time() - 60)
    owner_path(db).write_text(json.dumps(owner.__dict__), "utf-8")
    old = time.time() - 60
    os.utime(owner_path(db), (old, old))


def test_a_dashboard_with_flows_claims_the_database(tmp_path, flows):
    db = tmp_path / "s.db"
    with DashboardServer(db, port=0, flows=flows) as server:
        owner = read_owner(db)
        assert owner is not None and owner.url == server.url
        assert owner.flows == str(flows.resolve())
        assert is_live(owner)
        assert request(server, "GET", "/api/owner") == (200, {"token": owner.token})
    assert not owner_path(db).exists()  # released on stop


def test_a_read_only_dashboard_claims_nothing(tmp_path):
    db = tmp_path / "s.db"
    with DashboardServer(db, port=0) as server:
        assert not owner_path(db).exists()
        assert request(server, "GET", "/api/owner") == (200, {"token": None})


def test_a_second_dashboard_on_the_same_database_is_refused(tmp_path, flows):
    db = tmp_path / "s.db"
    with DashboardServer(db, port=0, flows=flows) as first:
        second = DashboardServer(db, port=0, flows=flows)
        with pytest.raises(OwnerError, match="already running at " + first.url):
            second.start()
        assert read_owner(db).url == first.url  # the first keeps its claim


def test_the_cli_reports_the_running_dashboard(tmp_path, flows, capsys):
    db = tmp_path / "s.db"
    with DashboardServer(db, port=0, flows=flows) as first:
        code = main(["ui", "--db", str(db), "--flows", str(flows), "--port", "0", "--no-browser"])
        assert code == 2
        assert first.url in capsys.readouterr().err


def test_a_file_left_by_a_crashed_dashboard_is_replaced(tmp_path, flows):
    db = tmp_path / "s.db"
    db.touch()
    write_owner(db, "http://127.0.0.1:9")  # nothing listens on the discard port
    with DashboardServer(db, port=0, flows=flows) as server:
        assert read_owner(db).url == server.url


def test_a_port_reused_by_another_program_is_not_live(tmp_path, flows):
    class Other(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"token": "someone-else"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    other = ThreadingHTTPServer(("127.0.0.1", 0), Other)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        db = tmp_path / "s.db"
        db.touch()
        write_owner(db, f"http://127.0.0.1:{other.server_address[1]}", token="mine")
        assert not is_live(read_owner(db))
        with DashboardServer(db, port=0, flows=flows) as server:
            assert read_owner(db).url == server.url
    finally:
        other.shutdown()
        other.server_close()


def test_a_half_written_new_file_means_another_dashboard_is_starting(tmp_path, flows):
    db = tmp_path / "s.db"
    db.touch()
    owner_path(db).write_text("{", "utf-8")  # fresh and unreadable
    with pytest.raises(OwnerError, match="starting"):
        claim(db, "http://127.0.0.1:1", flows)


def test_release_leaves_someone_elses_claim(tmp_path, flows):
    db = tmp_path / "s.db"
    db.touch()
    mine = claim(db, "http://127.0.0.1:1", flows)
    owner_path(db).unlink()
    theirs = claim(db, "http://127.0.0.1:2", flows)
    release(db, mine)
    assert read_owner(db) == theirs


@pytest.mark.parametrize("url", ["https://127.0.0.1:1", "http://example.com:80", "nonsense"])
def test_only_local_http_addresses_are_probed(url):
    assert not is_live(Owner(pid=1, url=url, flows="/x", token="t", started_at=0.0))
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_owner.py -v`
Expected: ERROR at collection, `ModuleNotFoundError: No module named 'miniflow.ui.owner'`.

- [ ] **Step 3: Create `miniflow/ui/owner.py`**

```python
"""Which dashboard runs flows for a state database (standard library only).

Only one process may launch runs for a database: :class:`~miniflow.ui.control.RunController`
matches each process it starts to "the newest run of that flow since launch", so two
controllers on one database would take each other's runs. A dashboard with a flows folder
claims ``<db>.owner.json`` once it is serving, and removes it when it stops:

    owner_path(db) -> Path
    claim(db, url, flows) -> Owner          OwnerError if a live dashboard holds the file
    release(db, owner) -> None              removes the file only if it is still ``owner``'s
    read_owner(db) -> Owner | None          the file's contents, live or not
    is_live(owner, timeout=2.0) -> bool     GET <url>/api/owner answers with owner's token

The file is created with exclusive create, so two dashboards starting together can't both
win. A file whose dashboard doesn't answer with its token (it crashed, or another program
now has the port) is replaced. A fresh file that can't be read yet belongs to a dashboard
that is writing it, so the claim is refused rather than taken.
"""

from __future__ import annotations

import http.client
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

__all__ = ["Owner", "OwnerError", "claim", "is_live", "owner_path", "read_owner", "release"]

_STARTING_SECONDS = 5.0


class OwnerError(ValueError):
    """Another dashboard runs flows for this database."""


@dataclass(frozen=True)
class Owner:
    pid: int
    url: str
    flows: str
    token: str
    started_at: float


def owner_path(db: str | os.PathLike[str]) -> Path:
    return Path(f"{Path(db).resolve()}.owner.json")


def read_owner(db: str | os.PathLike[str]) -> Owner | None:
    try:
        data = json.loads(owner_path(db).read_text(encoding="utf-8"))
        return Owner(
            pid=int(data["pid"]),
            url=str(data["url"]),
            flows=str(data["flows"]),
            token=str(data["token"]),
            started_at=float(data["started_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def is_live(owner: Owner, timeout: float = 2.0) -> bool:
    url = urlsplit(owner.url)
    if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost") or not url.port:
        return False
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=timeout)
    try:
        conn.request("GET", "/api/owner")
        response = conn.getresponse()
        body = json.loads(response.read() or b"null")
    except (OSError, ValueError):
        return False
    finally:
        conn.close()
    return response.status == 200 and isinstance(body, dict) and body.get("token") == owner.token


def claim(db: str | os.PathLike[str], url: str, flows: str | os.PathLike[str]) -> Owner:
    path = owner_path(db)
    owner = Owner(os.getpid(), url, str(Path(flows).resolve()), secrets.token_hex(16), time.time())
    for _ in range(2):
        try:
            with path.open("x", encoding="utf-8") as f:
                f.write(json.dumps(asdict(owner)))
            return owner
        except FileExistsError:
            current = read_owner(db)
            if current is None:
                try:
                    age = time.time() - path.stat().st_mtime
                except FileNotFoundError:
                    continue  # it was just released; try again
                if age < _STARTING_SECONDS:
                    raise OwnerError(
                        "another dashboard for this database is starting; try again"
                    ) from None
            elif is_live(current):
                raise OwnerError(
                    f"a dashboard for this database is already running at {current.url}"
                ) from None
            path.unlink(missing_ok=True)  # left by a dashboard that stopped without cleaning up
    raise OwnerError(f"couldn't claim {path}; another dashboard is starting")


def release(db: str | os.PathLike[str], owner: Owner) -> None:
    current = read_owner(db)
    if current is not None and current.token == owner.token:
        owner_path(db).unlink(missing_ok=True)
```

- [ ] **Step 4: Wire it into `DashboardServer`**

In `miniflow/ui/server.py`: import `from miniflow.ui.owner import Owner, claim, release`. In `__init__`, before `self.stopping = threading.Event()`, add `self.owner: Owner | None = None`. Replace `start` and `stop` with:

```python
def start(self) -> DashboardServer:
    self._thread = threading.Thread(
        target=self.serve_forever,
        kwargs={"poll_interval": 0.2},
        daemon=True,
        name="miniflow-ui",
    )
    self._thread.start()
    if self.controller is not None and self.workspace is not None:
        # Claimed only once serving, so another process checking the file gets an answer.
        try:
            self.owner = claim(self.store.path, self.url, self.workspace.folder)
        except BaseException:
            self.stop()
            raise
    if self.schedule_service is not None:
        self.schedule_service.start()
    return self


def stop(self) -> None:
    if self.schedule_service is not None:
        self.schedule_service.stop()
    if self.controller is not None:
        self.controller.shutdown()
    if self.owner is not None:
        release(self.store.path, self.owner)
        self.owner = None
    self.stopping.set()
    if self._thread is not None:
        self.shutdown()
        self._thread.join(timeout=5)
    self.server_close()
    self.store.close()
```

In `do_GET`, before `elif parts == ["api", "runs"]:`, add:

```python
            elif parts == ["api", "owner"]:
                self._send_json({"token": None if self.server.owner is None else self.server.owner.token})
```

Add to the endpoint list in the module docstring: ``GET /api/owner`` ``{"token": ...}``: proves this server owns its database (:mod:`miniflow.ui.owner`). Check that `StateStore` has a `path` attribute (it does: `RunController` uses `store.path`). `_cmd_ui` already turns `ValueError` into `error: ...` and exit code 2, and `OwnerError` is a `ValueError`.

- [ ] **Step 5: Run the new tests and the existing dashboard tests**

Run: `uv run pytest tests/test_owner.py tests/test_ui.py tests/test_control.py tests/test_run_queue.py tests/test_schedule.py -v`
Expected: all PASS. If an existing test starts two dashboards with `flows=` on one database, give the second its own database file; that test was relying on the uncoordinated behaviour this task removes.

- [ ] **Step 6: Commit**

```bash
git add miniflow/ui/owner.py miniflow/ui/server.py tests/test_owner.py
git commit -m "Dashboard: claim the database so only one process runs its flows"
```

---

### Task 3: Block drafts (static checks, write, list, approve, discard)

**Files:**
- Create: `miniflow/agent/__init__.py`, `miniflow/agent/drafts.py`
- Test: `tests/test_agent_drafts.py`

**Interfaces:**
- Consumes: `miniflow.blocks.all_blocks`, `load_plugins`, `BlockError`.
- Produces (used by Tasks 4, 5, 6, 7):
  - `class DraftError(ValueError)` with `.report: dict[str, Any] | None`
  - `check_source(folder: Path, name: str, source: str) -> dict` → `{"ok": bool, "checks": [{"check": str, "ok": bool, "detail": str}], "imports": [str]}`
  - `write_draft(folder: Path, name: str, source: str, *, replace: bool = False) -> dict` (a draft entry)
  - `list_drafts(folder: Path) -> list[dict]`; a draft entry is `{"name", "source", "sha256", "drafted_at", "ok", "checks", "imports"}`
  - `approve(folder: Path, name: str, sha256: str) -> list[str]` (block names added)
  - `discard(folder: Path, name: str, sha256: str) -> None`
  - `folder` is always the **flows** folder; drafts live in `folder / "blocks"`.

- [ ] **Step 1: Write the failing tests** (`tests/test_agent_drafts.py`)

```python
"""Block drafts: checked with ast only, never imported until a person approves them."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from textwrap import dedent

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from miniflow.agent.drafts import (
    DraftError,
    approve,
    check_source,
    discard,
    list_drafts,
    write_draft,
)
from miniflow.blocks import all_blocks, load_plugins


def uniq() -> str:
    # Plugin modules and blocks are global to the test process, so every test needs new names.
    return "blk_" + uuid.uuid4().hex[:10]


def good(name: str) -> str:
    return dedent(
        f'''
        """A test block."""
        from miniflow.blocks import Setting, block

        LIMIT = 3

        @block("{name}", settings=[Setting("text", str), Setting("times", int, default=2)])
        def run(text, times):
            """Repeat text."""
            return text * min(times, LIMIT)
        '''
    )


def failed(report: dict) -> set[str]:
    return {c["check"] for c in report["checks"] if not c["ok"]}


@pytest.fixture
def flows(tmp_path: Path) -> Path:
    return tmp_path


def test_a_good_block_passes_every_check(flows):
    report = check_source(flows, (name := uniq()), good(name))
    assert report["ok"], report
    assert report["imports"] == ["miniflow.blocks"]


BAD = {
    "parses": "def (:\n",
    "top-level code": 'print("hi")\n',
    "top-level code#class": "class A:\n    pass\n",
    "top-level code#decorator": "import functools\n@functools.cache\ndef f():\n    pass\n",
    "top-level code#default": "def f(x=open('x')):\n    pass\n",
    "top-level code#late import": "def f():\n    pass\nimport os\n",
    "top-level code#setting call": "import os\nfrom miniflow.blocks import Setting, block\n"
    "@block('NAME', settings=[Setting('x', str, default=os.getcwd())])\ndef f(x):\n    pass\n",
    "one block": "def f():\n    pass\n",
    "block name": "from miniflow.blocks import block\n@block('someone_else')\ndef f():\n    pass\n",
    "settings": "from miniflow.blocks import Setting, block\n"
    "@block('NAME', settings=[Setting('missing', str)])\ndef f(x):\n    pass\n",
    "kind": "from miniflow.blocks import block\n@block('NAME', kind='async')\ndef f():\n    pass\n",
    "size": "X = 1\n" * 30_000,
}


@pytest.mark.parametrize("check", sorted(BAD))
def test_each_check_refuses_its_case(flows, check):
    name = uniq()
    report = check_source(flows, name, BAD[check].replace("NAME", name))
    assert not report["ok"]
    assert check.split("#")[0] in failed(report)


@pytest.mark.parametrize("name", ["json", "os", "log", "Bad", "1x", "x" * 41, "has-dash"])
def test_names_that_clash_or_are_malformed_are_refused(flows, name):
    report = check_source(flows, name, good(name))
    assert failed(report) & {"name", "name is free", "doesn't shadow a module"}


def test_a_draft_is_never_imported(flows, tmp_path):
    marker = tmp_path / "ran.txt"
    source = f"open({str(marker)!r}, 'w').close()\n" + good(name := uniq())
    with pytest.raises(DraftError) as info:
        write_draft(flows, name, source)
    assert "top-level code" in failed(info.value.report)
    assert not marker.exists()
    assert not (flows / "blocks" / f"_draft_{name}.py").exists()


def test_a_written_draft_stays_out_of_the_catalog(flows):
    draft = write_draft(flows, (name := uniq()), good(name))
    assert draft["name"] == name and draft["ok"]
    assert draft["sha256"] == hashlib.sha256(good(name).encode()).hexdigest()
    load_plugins(flows / "blocks")
    assert name not in {b.name for b in all_blocks()}
    assert [d["name"] for d in list_drafts(flows)] == [name]


def test_writing_over_a_draft_needs_replace(flows):
    write_draft(flows, (name := uniq()), good(name))
    with pytest.raises(DraftError, match="replace"):
        write_draft(flows, name, good(name))
    changed = good(name).replace("LIMIT = 3", "LIMIT = 4")
    assert write_draft(flows, name, changed, replace=True)["source"] == changed


def test_approve_loads_the_block(flows):
    draft = write_draft(flows, (name := uniq()), good(name))
    assert approve(flows, name, draft["sha256"]) == [name]
    assert (flows / "blocks" / f"{name}.py").exists()
    assert list_drafts(flows) == []
    assert name in {b.name for b in all_blocks()}


def test_approve_refuses_a_draft_that_changed_since_it_was_shown(flows):
    write_draft(flows, (name := uniq()), good(name))
    with pytest.raises(DraftError, match="changed"):
        approve(flows, name, "0" * 64)
    assert list_drafts(flows)[0]["name"] == name


def test_a_block_that_fails_to_load_goes_back_to_being_a_draft(flows):
    name = uniq()
    missing = "not_a_module_" + uuid.uuid4().hex[:8]
    source = good(name).replace("from miniflow.blocks", f"import {missing}\nfrom miniflow.blocks")
    draft = write_draft(flows, name, source)
    with pytest.raises(DraftError, match="failed to load"):
        approve(flows, name, draft["sha256"])
    assert not (flows / "blocks" / f"{name}.py").exists()
    assert [d["name"] for d in list_drafts(flows)] == [name]


def test_discard_removes_the_draft(flows):
    draft = write_draft(flows, (name := uniq()), good(name))
    with pytest.raises(DraftError, match="changed"):
        discard(flows, name, "0" * 64)
    discard(flows, name, draft["sha256"])
    assert list_drafts(flows) == []


def test_unknown_drafts_are_reported(flows):
    with pytest.raises(DraftError, match="no draft"):
        approve(flows, "nothing_here", "0" * 64)


@given(st.text(max_size=400))
@settings(
    max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_any_text_gives_a_report_never_an_exception(flows, source):
    report = check_source(flows, "anything", source)
    assert isinstance(report["ok"], bool)


def test_deeply_nested_source_is_refused_not_crashed(flows):
    report = check_source(flows, "deep", "X = " + "(" * 5000 + "1" + ")" * 5000 + "\n")
    assert not report["ok"]
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_agent_drafts.py -v`
Expected: ERROR, `No module named 'miniflow.agent'`.

- [ ] **Step 3: Create `miniflow/agent/__init__.py`**

```python
"""Agent features: the tools an agent uses, served over MCP (standard library only).

See :mod:`miniflow.agent.tools` for the tools, :mod:`miniflow.agent.mcp` for the MCP server,
:mod:`miniflow.agent.drafts` for block drafts and :mod:`miniflow.agent.owner` for how runs
reach the dashboard.
"""
```

- [ ] **Step 4: Create `miniflow/agent/drafts.py`**

```python
"""Blocks proposed by an agent, waiting for a person to approve them (standard library only).

An agent may only *draft* a block: :func:`write_draft` stores ``blocks/_draft_<name>.py`` in
the flows folder. :func:`miniflow.blocks.load_plugins` skips files whose name starts with
``_``, so a draft is never imported. :func:`check_source` reads the source with :mod:`ast`
only; it never imports or runs it. A person approves a draft (the Editor's Pending blocks
panel, or ``miniflow approve-block``), which renames it to ``blocks/<name>.py`` and loads it.

    check_source(folder, name, source) -> {"ok", "checks": [{"check", "ok", "detail"}], "imports"}
    write_draft(folder, name, source, *, replace=False) -> draft   only if every check passes
    list_drafts(folder) -> [draft]   {"name", "source", "sha256", "drafted_at", "ok", "checks", "imports"}
    approve(folder, name, sha256) -> [block names added]
    discard(folder, name, sha256) -> None

``folder`` is the flows folder. ``sha256`` is the hex digest of the source the person was
shown; :func:`approve` and :func:`discard` refuse a draft that has changed since. A block
that fails to load when approved is renamed back to a draft. Every failure is a
:class:`DraftError`; its ``report`` holds the check results when they are the reason.

The checks make importing an approved file run nothing but its imports and the ``@block``
registration: top-level code is limited to imports (before the first function), functions,
literal constants and a docstring; the only decorator is ``@block(...)`` with literal
arguments and ``Setting(...)`` calls; defaults and annotations call nothing.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

from miniflow.blocks import BlockError, all_blocks, load_plugins

__all__ = ["DraftError", "approve", "check_source", "discard", "list_drafts", "write_draft"]

_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_PREFIX = "_draft_"
_MAX_SOURCE = 100_000
_KINDS = ("io", "cpu", "async")
_ALLOWED_NAMES = {"Setting", "block", "str", "int", "float", "bool", "list", "dict", "object"}
_LOCK = threading.Lock()


class DraftError(ValueError):
    def __init__(self, message: str, report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = report


def _blocks_dir(folder: Path) -> Path:
    blocks = Path(folder).resolve() / "blocks"
    if blocks.is_symlink():
        raise DraftError("the blocks folder can't be a symlink")
    return blocks


def _add(checks: list[dict[str, Any]], check: str, ok: bool, problem: str) -> None:
    checks.append({"check": check, "ok": ok, "detail": "" if ok else problem})


def _calls_nothing(node: ast.AST | None) -> bool:
    return node is None or not any(isinstance(n, ast.Call) for n in ast.walk(node))


def _literal(node: ast.AST) -> bool:
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False
    return True


def _block_call(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Name) and node.func.id == "block"


def _plain_block_args(call: ast.Call) -> bool:
    """``@block(...)`` arguments: literals, setting types and ``Setting(...)`` calls only."""
    for n in ast.walk(call):
        if isinstance(n, ast.Call):
            if n is not call and not (isinstance(n.func, ast.Name) and n.func.id == "Setting"):
                return False
        elif isinstance(n, ast.Name):
            if n.id not in _ALLOWED_NAMES:
                return False
        elif not isinstance(
            n,
            ast.Constant | ast.List | ast.Tuple | ast.Dict | ast.keyword | ast.expr_context
            | ast.UnaryOp | ast.USub,
        ):  # fmt: skip
            return False
    return True


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _setting_names(call: ast.Call) -> list[str]:
    value = _keyword(call, "settings")
    names: list[str] = []
    for item in value.elts if isinstance(value, ast.List | ast.Tuple) else []:
        if isinstance(item, ast.Call):
            first = item.args[0] if item.args else _keyword(item, "name")
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.append(first.value)
    return names


def _top_level(
    body: list[ast.stmt],
) -> tuple[list[str], list[str], list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, ast.Call]]]:
    problems: list[str] = []
    imports: list[str] = []
    blocks: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, ast.Call]] = []
    seen_function = False
    for node in body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            if seen_function:
                problems.append(f"line {node.lineno}: put imports before the first function")
            if isinstance(node, ast.Import):
                imports += [alias.name for alias in node.names]
            else:
                imports.append("." * node.level + (node.module or ""))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            seen_function = True
            for decorator in node.decorator_list:
                if not (isinstance(decorator, ast.Call) and _block_call(decorator)):
                    problems.append(
                        f"line {decorator.lineno}: only @block(...) may decorate a function"
                    )
                elif _plain_block_args(decorator):
                    blocks.append((node, decorator))
                else:
                    problems.append(
                        f"line {decorator.lineno}: @block(...) takes plain values and Setting(...) only"
                    )
            a = node.args
            parts: list[ast.AST | None] = [*a.defaults, *a.kw_defaults, node.returns]
            parts += [
                x.annotation
                for x in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]
                if x is not None
            ]
            if not all(_calls_nothing(p) for p in parts):
                problems.append(
                    f"line {node.lineno}: {node.name}() calls something in a default value or annotation"
                )
        elif (
            isinstance(node, ast.Assign)
            and all(isinstance(t, ast.Name) for t in node.targets)
            and _literal(node.value)
        ):
            pass
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and _calls_nothing(node.annotation)
            and (node.value is None or _literal(node.value))
        ):
            pass
        else:
            problems.append(
                f"line {node.lineno}: only imports, functions and plain constants can be at the top level"
            )
    return problems, imports, blocks


def _shadows_a_module(name: str) -> bool:
    if name in sys.stdlib_module_names or name in sys.modules:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return True


def check_source(folder: Path, name: str, source: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    _add(
        checks,
        "name",
        bool(_NAME.fullmatch(name)),
        "use lowercase letters, digits and _, starting with a letter, at most 40 characters",
    )
    _add(
        checks,
        "size",
        len(source) <= _MAX_SOURCE,
        f"a block's source must be at most {_MAX_SOURCE} characters",
    )
    if not all(c["ok"] for c in checks):
        return {"ok": False, "checks": checks, "imports": []}
    try:
        tree = ast.parse(source, filename=f"{_PREFIX}{name}.py")
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        where = f"line {exc.lineno}: " if isinstance(exc, SyntaxError) and exc.lineno else ""
        _add(checks, "parses", False, f"{where}{getattr(exc, 'msg', None) or exc}")
        return {"ok": False, "checks": checks, "imports": []}
    _add(checks, "parses", True, "")
    body = list(tree.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]  # the module docstring
    try:
        problems, imports, blocks = _top_level(body)
    except RecursionError:
        problems, imports, blocks = ["the source is nested too deeply"], [], []
    _add(checks, "top-level code", not problems, "; ".join(problems))
    _add(
        checks,
        "one block",
        len(blocks) == 1,
        "no function has @block(...)" if not blocks else "draft one block per file",
    )
    if len(blocks) == 1:
        fn, call = blocks[0]
        first = call.args[0] if call.args else None
        declared = first.value if isinstance(first, ast.Constant) else None
        _add(
            checks,
            "block name",
            declared == name,
            f"@block({declared!r}) should be @block({name!r})",
        )
        params = {a.arg for a in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]}
        missing = [s for s in _setting_names(call) if s not in params and fn.args.kwarg is None]
        _add(
            checks, "settings", not missing, f"not parameters of {fn.name}(): {', '.join(missing)}"
        )
        kind_node = _keyword(call, "kind")
        kind = (
            kind_node.value
            if isinstance(kind_node, ast.Constant)
            else "io"
            if kind_node is None
            else None
        )
        is_async = isinstance(fn, ast.AsyncFunctionDef)
        _add(
            checks,
            "kind",
            kind in _KINDS and (kind == "async") == is_async,
            "kind must be io, cpu or async, and async exactly for 'async def'",
        )
    blocks_dir = _blocks_dir(folder)
    try:
        if blocks_dir.is_dir():
            load_plugins(blocks_dir)
        taken = {b.name for b in all_blocks()}
        _add(checks, "name is free", name not in taken, f"there is already a block called {name!r}")
    except BlockError as exc:
        _add(checks, "name is free", False, f"couldn't load the existing blocks: {exc}")
    _add(
        checks,
        "doesn't shadow a module",
        not _shadows_a_module(name),
        f"a file called {name}.py would hide the module {name!r}; choose another name",
    )
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "imports": sorted(set(imports))}


def _draft_path(blocks: Path, name: str) -> Path:
    path = blocks / f"{_PREFIX}{name}.py"
    if not _NAME.fullmatch(name) or not path.is_file() or path.is_symlink():
        raise DraftError(f"there is no draft called {name!r}")
    return path


def _entry(folder: Path, path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    name = path.stem.removeprefix(_PREFIX)
    try:
        source = data.decode("utf-8")
        report = check_source(folder, name, source)
    except UnicodeDecodeError:
        source = data.decode("utf-8", errors="replace")
        report = {
            "ok": False,
            "checks": [{"check": "parses", "ok": False, "detail": "not UTF-8 text"}],
            "imports": [],
        }
    return {
        "name": name,
        "source": source,
        "sha256": hashlib.sha256(data).hexdigest(),
        "drafted_at": path.stat().st_mtime,
        **report,
    }


def write_draft(folder: Path, name: str, source: str, *, replace: bool = False) -> dict[str, Any]:
    report = check_source(folder, name, source)
    if not report["ok"]:
        raise DraftError("the draft has problems, so nothing was written", report)
    blocks = _blocks_dir(folder)
    with _LOCK:
        blocks.mkdir(exist_ok=True)
        path = blocks / f"{_PREFIX}{name}.py"
        if path.is_symlink():
            raise DraftError("a draft can't be a symlink")
        if path.exists() and not replace:
            raise DraftError(f"there is already a draft called {name!r}; pass replace to change it")
        temporary = blocks / f"{_PREFIX}{name}.tmp"
        temporary.write_bytes(source.encode("utf-8"))
        os.replace(temporary, path)
        return _entry(folder, path)


def list_drafts(folder: Path) -> list[dict[str, Any]]:
    blocks = _blocks_dir(folder)
    if not blocks.is_dir():
        return []
    paths = [
        p for p in sorted(blocks.glob(f"{_PREFIX}*.py"))
        if p.is_file() and not p.is_symlink() and _NAME.fullmatch(p.stem.removeprefix(_PREFIX))
    ]  # fmt: skip
    return [_entry(folder, p) for p in paths]


def _same(path: Path, sha256: str) -> bytes:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise DraftError("the draft changed since you looked at it; review it again")
    return data


def approve(folder: Path, name: str, sha256: str) -> list[str]:
    blocks = _blocks_dir(folder)
    with _LOCK:
        draft = _draft_path(blocks, name)
        data = _same(draft, sha256)
        try:
            report = check_source(folder, name, data.decode("utf-8"))
        except UnicodeDecodeError:
            raise DraftError("the draft isn't UTF-8 text") from None
        if not report["ok"]:
            raise DraftError("the draft doesn't pass its checks", report)
        target = blocks / f"{name}.py"
        if target.exists():
            raise DraftError(f"blocks/{name}.py already exists")
        draft.rename(target)
        try:
            return load_plugins(blocks)
        except BlockError as exc:
            target.rename(draft)
            raise DraftError(f"the block failed to load, so it is a draft again: {exc}") from None


def discard(folder: Path, name: str, sha256: str) -> None:
    blocks = _blocks_dir(folder)
    with _LOCK:
        draft = _draft_path(blocks, name)
        _same(draft, sha256)
        draft.unlink()
```

Then run `uv run ruff format miniflow/agent/drafts.py` to wrap the long lines.

- [ ] **Step 5: Run the tests, ruff and mypy**

Run: `uv run pytest tests/test_agent_drafts.py -v && uv run ruff check miniflow/agent && uv run mypy`
Expected: all PASS, no lint or type errors. If mypy can't narrow `decorator` to `ast.Call` in the `elif`, split the test into a nested `if isinstance(decorator, ast.Call) and _block_call(decorator):` block.

- [ ] **Step 6: Commit**

```bash
git add miniflow/agent/__init__.py miniflow/agent/drafts.py tests/test_agent_drafts.py
git commit -m "Agent: block drafts checked with ast only, approved by a person"
```

---

### Task 4: Dashboard endpoints for drafts

**Files:**
- Modify: `miniflow/ui/server.py` (`do_GET` `:172`, `_post` `:273`, docstring)
- Test: `tests/test_agent_drafts.py` (append)

**Interfaces:**
- Consumes: `list_drafts`, `approve`, `discard`, `DraftError` (Task 3); `FlowWorkspace.catalog()`.
- Produces (used by Task 5):
  - `GET /api/blocks/drafts` → `[draft entry]`
  - `POST /api/blocks/drafts/approve` `{"name", "sha256"}` → `{"added": [str], "blocks": [catalog entry]}`
  - `POST /api/blocks/drafts/discard` `{"name", "sha256"}` → `{"discarded": name}`
  - Errors: `409 {"error": str, "checks": [...]?}`. All exist only when the dashboard has a flows folder and `allow_run` is true; otherwise 404.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_agent_drafts.py`)

```python
from miniflow.ui import DashboardServer
from tests.test_ui import call


@pytest.fixture
def dashboard(tmp_path: Path):
    flows = tmp_path / "flows"
    flows.mkdir()
    with DashboardServer(tmp_path / "s.db", port=0, flows=flows, allow_run=True) as server:
        yield server, flows


def test_the_dashboard_lists_and_approves_drafts(dashboard):
    server, flows = dashboard
    draft = write_draft(flows, (name := uniq()), good(name))
    status, drafts = call(server, "GET", "/api/blocks/drafts")
    assert status == 200 and [d["sha256"] for d in drafts] == [draft["sha256"]]
    status, result = call(
        server, "POST", "/api/blocks/drafts/approve", {"name": name, "sha256": draft["sha256"]}
    )
    assert status == 200 and result["added"] == [name]
    assert {"name": name, "group": "Plugins"}.items() <= next(
        b for b in result["blocks"] if b["name"] == name
    ).items()
    assert call(server, "GET", "/api/blocks/drafts") == (200, [])


def test_the_dashboard_refuses_a_stale_approval(dashboard):
    server, flows = dashboard
    write_draft(flows, (name := uniq()), good(name))
    status, body = call(
        server, "POST", "/api/blocks/drafts/approve", {"name": name, "sha256": "0" * 64}
    )
    assert status == 409 and "changed" in body["error"]


def test_the_dashboard_discards_drafts(dashboard):
    server, flows = dashboard
    draft = write_draft(flows, (name := uniq()), good(name))
    assert call(
        server, "POST", "/api/blocks/drafts/discard", {"name": name, "sha256": draft["sha256"]}
    ) == (200, {"discarded": name})
    assert list_drafts(flows) == []


def test_approval_from_another_site_is_refused(dashboard):
    server, flows = dashboard
    draft = write_draft(flows, (name := uniq()), good(name))
    status, _ = call(
        server,
        "POST",
        "/api/blocks/drafts/approve",
        {"name": name, "sha256": draft["sha256"]},
        headers={"Origin": "http://evil.example"},
    )
    assert status == 403
    assert list_drafts(flows)


def test_a_read_only_dashboard_has_no_draft_endpoints(tmp_path):
    flows = tmp_path / "flows"
    flows.mkdir()
    with DashboardServer(tmp_path / "s.db", port=0, flows=flows, allow_run=False) as server:
        assert call(server, "GET", "/api/blocks/drafts")[0] == 404
        assert (
            call(server, "POST", "/api/blocks/drafts/approve", {"name": "x", "sha256": "0"})[0]
            == 404
        )
```

Note: `call` sends a JSON body even on GET; the server ignores it.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_agent_drafts.py -k dashboard -v`
Expected: FAIL, 404 for `/api/blocks/drafts`.

- [ ] **Step 3: Implement**

In `miniflow/ui/server.py`, import `from miniflow.agent.drafts import DraftError, approve, discard, list_drafts`. Add to `_Handler`:

```python
    def _drafts_enabled(self) -> bool:
        """Draft review is part of editing: it needs a flows folder and a dashboard that may run."""
        return self.server.workspace is not None and self.server.allow_run
```

In `do_GET`, before `elif parts == ["api", "blocks"] ...`, add:

```python
            elif parts == ["api", "blocks", "drafts"] and self._drafts_enabled():
                assert self.server.workspace is not None
                self._send_json(list_drafts(self.server.workspace.folder))
```

In `_post`, after the `upload-csv` branch, add:

```python
            if (
                len(parts) == 4
                and parts[:3] == ["api", "blocks", "drafts"]
                and parts[3] in ("approve", "discard")
                and self._drafts_enabled()
            ):
                assert self.server.workspace is not None
                body = self._control_body()
                name, digest = _text(body, "name"), _text(body, "sha256")
                folder = self.server.workspace.folder
                if parts[3] == "approve":
                    added = approve(folder, name, digest)
                    self._send_json({"added": added, "blocks": self.server.workspace.catalog()})
                else:
                    discard(folder, name, digest)
                    self._send_json({"discarded": name})
                return
```

Before `except WorkspaceError as exc:` in `_post`, add:

```python
        except DraftError as exc:
            reply: dict[str, Any] = {"error": str(exc)}
            if exc.report is not None:
                reply["checks"] = exc.report["checks"]
            self._send_json(reply, HTTPStatus.CONFLICT)
```

Add the three endpoints to the module docstring's endpoint list.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_agent_drafts.py tests/test_ui.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add miniflow/ui/server.py tests/test_agent_drafts.py
git commit -m "Dashboard: list, approve and discard block drafts"
```

---

### Task 5: Pending blocks panel in the Editor

**Files:**
- Create: `miniflow/ui/static/editor-drafts.js`
- Modify: `miniflow/ui/server.py` (`_STATIC` `:51`), `miniflow/ui/static/index.html` (palette `:87`, scripts `:242`), `miniflow/ui/static/editor.js` (`STUDIO_PARTS` `:1190`, `studioInit` `:1207`), `miniflow/ui/static/style.css` (end of file)
- Test: `tests/test_agent_drafts.py` (append), then a browser check

**Interfaces:**
- Consumes: the Task 4 endpoints; `studioApi`, `studioMessage`, `studioPalette`, `Studio` (editor.js).
- Produces: `studioDraftsInit()`, `studioLoadDrafts()` (global functions); DOM ids `studio-drafts`, `studio-drafts-dialog`, `studio-drafts-list`, `studio-drafts-error`, `studio-drafts-close`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_agent_drafts.py`)

```python
from tests.test_ui import get


def test_the_editor_ships_the_pending_blocks_panel(dashboard):
    server, _ = dashboard
    status, ctype, body = get(server, "/static/editor-drafts.js")
    assert status == 200 and ctype.startswith("text/javascript")
    assert b"function studioDraftsInit" in body
    page = get(server, "/")[2]
    for marker in (
        b'id="studio-drafts"',
        b'id="studio-drafts-dialog"',
        b"/static/editor-drafts.js",
    ):
        assert marker in page
    assert b'"studioDraftsInit"' in get(server, "/static/editor.js")[2]
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/test_agent_drafts.py::test_the_editor_ships_the_pending_blocks_panel -v`
Expected: FAIL, 404 for `/static/editor-drafts.js`.

- [ ] **Step 3: Create `miniflow/ui/static/editor-drafts.js`**

```js
/* Pending blocks: block drafts an agent wrote, waiting for a person to approve them.
 * Nothing in a draft runs until it is approved here (or with `miniflow approve-block`). */
const StudioDrafts = { items: [] };

async function studioLoadDrafts() {
  const button = document.getElementById("studio-drafts");
  let drafts;
  try { drafts = await studioApi("/api/blocks/drafts"); }
  catch { button.hidden = true; return; } // a read-only dashboard has no drafts
  StudioDrafts.items = drafts;
  button.hidden = drafts.length === 0;
  button.textContent = `Pending blocks (${drafts.length})`;
  const dialog = document.getElementById("studio-drafts-dialog");
  if (dialog.open) {
    if (drafts.length) studioRenderDrafts(); else dialog.close();
  }
}

function studioDraftError(text) {
  const error = document.getElementById("studio-drafts-error");
  error.textContent = text || "";
  error.hidden = !text;
}

function studioRenderDrafts() {
  document.getElementById("studio-drafts-list").replaceChildren(...StudioDrafts.items.map((draft) => {
    const card = document.createElement("article");
    card.className = "studio-draft";
    const title = document.createElement("h3");
    title.textContent = draft.name;
    const about = document.createElement("p");
    about.className = "hint";
    about.textContent = `Drafted ${new Date(draft.drafted_at * 1000).toLocaleString()} · `
      + (draft.imports.length ? `imports ${draft.imports.join(", ")}` : "no imports");
    const checks = document.createElement("ul");
    checks.className = "studio-draft-checks";
    for (const check of draft.checks) {
      const item = document.createElement("li");
      item.className = check.ok ? "ok" : "bad";
      item.textContent = `${check.ok ? "✓" : "✗"} ${check.check}${check.detail ? `: ${check.detail}` : ""}`;
      checks.append(item);
    }
    const code = document.createElement("pre");
    code.className = "studio-draft-source";
    code.textContent = draft.source;
    const approve = document.createElement("button");
    approve.type = "button";
    approve.className = "btn primary";
    approve.textContent = "Approve";
    approve.disabled = !draft.ok;
    if (!draft.ok) approve.title = "Discard it, or ask the agent to fix the failed checks";
    approve.onclick = () => studioDraftAction("approve", draft);
    const discard = document.createElement("button");
    discard.type = "button";
    discard.className = "btn";
    discard.textContent = "Discard";
    discard.onclick = () => studioDraftAction("discard", draft);
    const actions = document.createElement("div");
    actions.className = "studio-draft-actions";
    actions.append(discard, approve);
    card.append(title, about, checks, code, actions);
    return card;
  }));
}

async function studioDraftAction(action, draft) {
  const question = action === "approve"
    ? `Approve ${draft.name}? This code will run on your computer with your permissions.`
    : `Discard the draft ${draft.name}?`;
  if (!window.confirm(question)) return;
  studioDraftError("");
  try {
    const result = await studioApi(`/api/blocks/drafts/${action}`, { name: draft.name, sha256: draft.sha256 });
    if (action === "approve") {
      Studio.blocks = new Map(result.blocks.map((block) => [block.name, block]));
      studioPalette();
      studioMessage(`Approved ${draft.name}. It is in the Plugins group.`);
    } else {
      studioMessage(`Discarded the draft ${draft.name}.`);
    }
  } catch (error) {
    studioDraftError(String(error.message || error));
  }
  await studioLoadDrafts();
}

function studioDraftsInit() {
  const dialog = document.getElementById("studio-drafts-dialog");
  document.getElementById("studio-drafts").onclick = () => {
    studioDraftError("");
    studioRenderDrafts();
    dialog.showModal();
  };
  document.getElementById("studio-drafts-close").onclick = () => dialog.close();
  studioLoadDrafts();
  // Drafts arrive from an agent at any time; check while the Editor is open.
  setInterval(() => { if (Studio.visible) studioLoadDrafts(); }, 15000);
}
```

- [ ] **Step 4: Wire it in**

`miniflow/ui/server.py`, `_STATIC`: add `"editor-drafts.js": "text/javascript; charset=utf-8",`.

`miniflow/ui/static/index.html`: after `<h2>Blocks</h2>` in the palette add

```html
          <button type="button" id="studio-drafts" class="btn studio-drafts-button" hidden>Pending blocks</button>
```

Before the first `<script` tag add

```html
  <dialog id="studio-drafts-dialog" class="studio-drafts-dialog" aria-labelledby="studio-drafts-title">
    <header>
      <h2 id="studio-drafts-title">Pending blocks</h2>
      <button type="button" id="studio-drafts-close" class="btn">Close</button>
    </header>
    <p class="hint">An agent drafted these blocks. Nothing in them runs until you approve one, and an approved block runs with your permissions, so read the code first.</p>
    <p id="studio-drafts-error" class="studio-message error" hidden></p>
    <div id="studio-drafts-list"></div>
  </dialog>
```

and add `<script src="/static/editor-drafts.js"></script>` just before `<script src="/static/editor.js"></script>`.

`miniflow/ui/static/editor.js`: add `"studioDraftsInit"` to the `STUDIO_PARTS` array, and in `studioInit` call `studioDraftsInit();` right after `studioViewInit();`.

`miniflow/ui/static/style.css`, append:

```css
.studio-drafts-button { width: 100%; margin: 0 0 10px; }
.studio-drafts-dialog { width: min(760px, calc(100vw - 32px)); max-height: calc(100vh - 64px); border: 1px solid var(--border); border-radius: 10px; background: var(--panel); color: var(--text); padding: 16px; }
.studio-drafts-dialog::backdrop { background: rgb(0 0 0 / 0.4); }
.studio-drafts-dialog header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.studio-drafts-dialog h2 { font-size: 15px; margin: 0; }
.studio-draft { border-top: 1px solid var(--border); padding: 12px 0; }
.studio-draft h3 { font-size: 14px; margin: 0 0 4px; }
.studio-draft-checks { list-style: none; padding: 0; margin: 8px 0; font-size: 12px; }
.studio-draft-checks .ok { color: var(--muted); }
.studio-draft-checks .bad { color: var(--failed-ink); }
.studio-draft-source { max-height: 320px; overflow: auto; border: 1px solid var(--border); border-radius: 7px; padding: 10px; font-size: 12px; white-space: pre; }
.studio-draft-actions { display: flex; gap: 8px; justify-content: flex-end; }
```

Check `style.css` defines a `.btn.primary` (or the equivalent primary-button class used by the Save button) and use that class for Approve.

- [ ] **Step 5: Run the test**

Run: `uv run pytest tests/test_agent_drafts.py tests/test_ui.py -v`
Expected: all PASS.

- [ ] **Step 6: Check it in the browser**

Write a scratch flows folder with a draft, start the dashboard and open the Editor:

```bash
mkdir -p /tmp/mf-drafts && uv run python -c "
from pathlib import Path
from miniflow.agent.drafts import write_draft
src = '''from miniflow.blocks import Setting, block

@block(\"shout\", settings=[Setting(\"text\", str)])
def shout(text):
    \"\"\"Upper-case some text.\"\"\"
    return text.upper()
'''
write_draft(Path('/tmp/mf-drafts'), 'shout', src)"
uv run miniflow serve --flows /tmp/mf-drafts --db /tmp/mf-drafts/s.db --no-browser --port 8790
```

In the browser pane at `http://127.0.0.1:8790/#editor`: the palette shows **Pending blocks (1)**; it opens the dialog with the source, every check ✓ and "imports miniflow.blocks"; **Approve** asks to confirm, then `shout` appears in the Plugins group and the button disappears. Repeat with a second draft and **Discard**. Check the dialog in the Brutalist, Glass and Terminal themes and at 375 px width. Stop the server with Ctrl+C.

- [ ] **Step 7: Commit**

```bash
git add miniflow/ui/static/editor-drafts.js miniflow/ui/static/index.html miniflow/ui/static/editor.js miniflow/ui/static/style.css miniflow/ui/server.py tests/test_agent_drafts.py
git commit -m "Editor: Pending blocks panel to review and approve block drafts"
```

---

### Task 6: `miniflow approve-block`

**Files:**
- Modify: `miniflow/cli.py` (new `_cmd_approve_block` after `_cmd_blocks` `:556`; parser after the `blocks` parser `:712`)
- Test: `tests/test_agent_drafts.py` (append)

**Interfaces:**
- Consumes: `list_drafts`, `approve`, `DraftError` (Task 3).
- Produces: `miniflow approve-block NAME --flows DIR`; exit 0 approved, 1 not approved or failed, 2 no such draft or folder.

- [ ] **Step 1: Write the failing tests** (append)

```python
from miniflow.cli import main


def test_approve_block_asks_first(flows, monkeypatch, capsys):
    write_draft(flows, (name := uniq()), good(name))
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert main(["approve-block", name, "--flows", str(flows)]) == 1
    out = capsys.readouterr().out
    assert "Repeat text." in out and "not approved" in out
    assert list_drafts(flows)


def test_approve_block_with_yes(flows, monkeypatch, capsys):
    write_draft(flows, (name := uniq()), good(name))
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert main(["approve-block", name, "--flows", str(flows)]) == 0
    assert (flows / "blocks" / f"{name}.py").exists()
    assert f"approved: {name}" in capsys.readouterr().out


def test_approve_block_with_no_such_draft(flows, capsys):
    assert main(["approve-block", "nothing", "--flows", str(flows)]) == 2
    assert "no draft" in capsys.readouterr().err
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_agent_drafts.py -k approve_block -v`
Expected: FAIL, argparse `invalid choice: 'approve-block'` (SystemExit 2).

- [ ] **Step 3: Implement** (in `miniflow/cli.py`)

```python
def _cmd_approve_block(args: argparse.Namespace) -> int:
    from miniflow.agent.drafts import DraftError, approve, list_drafts

    folder = Path(args.flows)
    if not folder.is_dir():
        print(f"error: flows folder {folder} doesn't exist", file=sys.stderr)
        return 2
    draft = next((d for d in list_drafts(folder) if d["name"] == args.name), None)
    if draft is None:
        print(
            f"error: there is no draft called {args.name!r} in {folder / 'blocks'}", file=sys.stderr
        )
        return 2
    print(draft["source"].rstrip() + "\n")
    for check in draft["checks"]:
        detail = f": {check['detail']}" if check["detail"] else ""
        print(f"{'ok  ' if check['ok'] else 'FAIL'} {check['check']}{detail}")
    print("imports: " + (", ".join(draft["imports"]) or "none"))
    if not draft["ok"]:
        print("error: fix the failed checks first", file=sys.stderr)
        return 1
    answer = input(f"Approve {args.name}? It will run with your permissions. [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        print("not approved")
        return 1
    try:
        added = approve(folder, args.name, draft["sha256"])
    except DraftError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"approved: {', '.join(added) or args.name}")
    return 0
```

Parser, after the `blocks` subparser:

```python
    ab = sub.add_parser("approve-block", help="review and approve a block an agent drafted")
    ab.add_argument("name", help="the draft's block name")
    ab.add_argument("--flows", required=True, metavar="DIR", help="folder of JSON flows")
    ab.set_defaults(func=_cmd_approve_block)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_agent_drafts.py tests/test_cli.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add miniflow/cli.py tests/test_agent_drafts.py
git commit -m "CLI: approve-block to review and approve a drafted block"
```

---

### Task 7: Tool layer: reading, writing flows, drafts

**Files:**
- Modify: `miniflow/ui/author.py` (`FlowWorkspace.save` `:370`, `__all__` `:27`)
- Create: `miniflow/agent/tools.py`
- Test: `tests/test_editor_v2.py` (append), `tests/test_agent_tools.py`

**Interfaces:**
- Consumes: `FlowWorkspace` (`read`, `validate`, `save`, `test`, `versions`, `version`, `catalog`, `templates`, `create_template`, `environments`, `list`); `block_catalog`, `get_block`; Task 3 drafts.
- Produces (used by Tasks 8 and 9):
  - `FlowWorkspace.file_version(name) -> str | None` (16 hex chars of sha256 of the file bytes)
  - `FlowWorkspace.save(name, document, message="", *, expect_version=False, base_version=None)`; raises `VersionConflict(WorkspaceError)` with `.current: str | None` when `expect_version` and the file's version isn't `base_version`
  - `class ToolError(Exception)`: `.data: dict[str, Any] | None`
  - `class UnknownTool(LookupError)`
  - `@dataclass(frozen=True) class Tool: name, description, schema, group ("read" | "write" | "run"), fn`
  - `TOOLS: dict[str, Tool]`, `tool(name, description, properties=None, required=(), group="read")` decorator
  - `class RunLink(Protocol)`: `url() -> str`, `run(flow: str, environment: str | None) -> dict`, `cancel(run_id: str) -> dict`, `control(*, start: bool = True) -> dict`
  - `available(*, read_only: bool, runs: bool) -> list[Tool]`
  - `Toolbox(flows, db, *, read_only=False, runs: RunLink | None = None)`: `.workspace`, `.folder`, `.db` (resolved), `.runs`, `.tools: dict[str, Tool]`
  - `call(box: Toolbox, name: str, args: object) -> dict[str, Any]`

- [ ] **Step 1: Write the failing workspace tests** (append to `tests/test_editor_v2.py`)

```python
from miniflow.ui.author import VersionConflict


def test_file_version_tracks_the_bytes(ws: FlowWorkspace):
    assert ws.file_version("demo") is None
    ws.save("demo", V1)
    first = ws.file_version("demo")
    assert first is not None and len(first) == 16
    ws.save("demo", V2)
    assert ws.file_version("demo") != first


def test_save_with_a_version_check(ws: FlowWorkspace):
    ws.save("demo", V1, expect_version=True, base_version=None)  # new flow
    with pytest.raises(VersionConflict) as info:
        ws.save("demo", V2, expect_version=True, base_version=None)  # it exists now
    assert info.value.current == ws.file_version("demo")
    with pytest.raises(VersionConflict):
        ws.save("demo", V2, expect_version=True, base_version="0" * 16)
    ws.save("demo", V2, expect_version=True, base_version=ws.file_version("demo"))
    assert ws.read("demo") == V2
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_editor_v2.py -k "file_version or version_check" -v`
Expected: ImportError for `VersionConflict`.

- [ ] **Step 3: Implement in `miniflow/ui/author.py`**

Add `import hashlib`. Add `"VersionConflict"` to `__all__`. After `WorkspaceError`:

```python
class VersionConflict(WorkspaceError):
    """The flow file isn't the version the caller read (someone else saved it meanwhile)."""

    def __init__(self, name: str, current: str | None) -> None:
        if current is None:
            message = f"flow {name!r} doesn't exist any more"
        else:
            message = f"flow {name!r} changed since you read it; read it again and merge"
        super().__init__(message)
        self.current = current


def _version(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]
```

In `FlowWorkspace`:

```python
    def file_version(self, name: str) -> str | None:
        """Short hash of the flow file's bytes, or None if there is no such file."""
        try:
            return _version(self._path(name).read_bytes())
        except FileNotFoundError:
            return None
```

Change `save`'s signature to `def save(self, name: str, document: Any, message: str = "", *, expect_version: bool = False, base_version: str | None = None) -> dict[str, Any]:` and, inside `with self._lock:` right after `previous = ...`, add:

```python
            if expect_version:
                current = None if previous is None else _version(previous)
                if current != base_version:
                    conflict = VersionConflict(name, current)
                    if current is not None and base_version is None:
                        conflict.args = (f"flow {name!r} already exists; read it and pass its version",)
                    raise conflict
```

Extend `save`'s docstring (add one): "With ``expect_version``, the save is refused with :class:`VersionConflict` unless the file's :meth:`file_version` is ``base_version`` (``None``: the flow must not exist yet)." Add a line to the module docstring: "Saves can check the file's version first, so two editors can't overwrite each other unnoticed."

- [ ] **Step 4: Run the workspace tests**

Run: `uv run pytest tests/test_editor_v2.py tests/test_author.py -v`
Expected: all PASS.

- [ ] **Step 5: Write the failing tool tests** (`tests/test_agent_tools.py`)

```python
"""The agent tool layer: reading, writing and validating flows, block drafts."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from miniflow.agent.tools import TOOLS, Toolbox, ToolError, UnknownTool, available, call

HELLO = {
    "description": "Say hello",
    "steps": {"hello": {"block": "log", "settings": {"message": "hi"}}},
}
BYE = {"steps": {"bye": {"block": "log", "settings": {"message": "bye"}}}}


class FakeLink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def url(self) -> str:
        return "http://127.0.0.1:1"

    def run(self, flow: str, environment: str | None) -> dict[str, Any]:
        self.calls.append(("run", flow))
        return {"action": "run", "flow": flow, "run_id": None, "queued": True}

    def cancel(self, run_id: str) -> dict[str, Any]:
        self.calls.append(("cancel", run_id))
        return {}

    def control(self, *, start: bool = True) -> dict[str, Any]:
        return {
            "running": [],
            "queued": [{"flow": "hello", "source": "manual:run:hello", "queued_at": 0}],
        }


@pytest.fixture
def flows(tmp_path: Path) -> Path:
    d = tmp_path / "flows"
    d.mkdir()
    (d / "hello.json").write_text(json.dumps(HELLO), "utf-8")
    env = d / "environments"
    env.mkdir()
    (env / "prod.json").write_text(json.dumps({"params": {}}), "utf-8")
    (env / "prod.env").write_text("TOKEN=supersecret-value\n", "utf-8")
    return d


@pytest.fixture
def box(flows: Path, tmp_path: Path) -> Toolbox:
    return Toolbox(flows, tmp_path / "s.db", runs=FakeLink())


def test_modes_choose_the_tools():
    names = lambda tools: {t.name for t in tools}  # noqa: E731
    assert "save_flow" not in names(available(read_only=True, runs=True))
    assert "run_flow" not in names(available(read_only=True, runs=True))
    assert "run_flow" not in names(available(read_only=False, runs=False))
    assert "save_flow" in names(available(read_only=False, runs=False))
    assert {"list_blocks", "get_flow"} <= names(available(read_only=True, runs=False))


def test_every_tool_has_an_object_schema():
    for tool in TOOLS.values():
        assert tool.schema["type"] == "object" and tool.description
        assert set(tool.schema["required"]) <= set(tool.schema["properties"])


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "name is required"),
        ({"name": 3}, "name must be string"),
        ({"name": "x", "other": 1}, "unknown argument"),
    ],
)
def test_arguments_are_checked(box, args, message):
    with pytest.raises(ToolError, match=message):
        call(box, "get_flow", args)


def test_unknown_or_hidden_tools(flows, tmp_path):
    with pytest.raises(UnknownTool):
        call(Toolbox(flows, tmp_path / "s.db"), "nope", {})
    with pytest.raises(UnknownTool):
        call(Toolbox(flows, tmp_path / "s.db", read_only=True), "save_flow", {})


def test_list_and_describe_blocks(box):
    names = {b["name"] for b in call(box, "list_blocks", {})["blocks"]}
    assert "log" in names
    only = call(box, "list_blocks", {"group": "transform"})["blocks"]
    assert only and {b["group"] for b in only} == {"Transform"}
    log = call(box, "describe_block", {"name": "log"})
    assert log["example_step"]["block"] == "log" and "message" in log["example_step"]["settings"]
    with pytest.raises(ToolError, match="no block called"):
        call(box, "describe_block", {"name": "lgo"})


def test_get_and_list_flows(box):
    flow = call(box, "get_flow", {"name": "hello"})
    assert flow["document"] == HELLO and flow["version"] == box.workspace.file_version("hello")
    listed = call(box, "list_flows", {})["flows"]
    assert listed == [
        {
            "name": "hello",
            "description": "Say hello",
            "valid": True,
            "problems": [],
            "schedules": [],
            "version": flow["version"],
        }
    ]
    with pytest.raises(ToolError, match="no flow"):
        call(box, "get_flow", {"name": "missing"})


def test_validate_reports_problems(box):
    result = call(
        box, "validate_flow", {"name": "x", "document": {"steps": {"a": {"block": "lgo"}}}}
    )
    assert result["valid"] is False and any("lgo" in p for p in result["problems"])


def test_save_new_then_update_with_its_version(box):
    created = call(box, "save_flow", {"name": "bye", "document": BYE, "message": "first"})
    assert created["saved"] and created["version"] == box.workspace.file_version("bye")
    changed = {"steps": {"bye": {"block": "log", "settings": {"message": "see you"}}}}
    call(
        box,
        "save_flow",
        {
            "name": "bye",
            "document": changed,
            "message": "second",
            "base_version": created["version"],
        },
    )
    assert box.workspace.read("bye") == changed
    assert box.workspace.versions("bye")[0]["message"] == "first (agent)"


def test_save_refuses_conflicts_and_returns_the_current_document(box):
    with pytest.raises(ToolError, match="already exists") as info:
        call(box, "save_flow", {"name": "hello", "document": BYE, "message": "oops"})
    assert info.value.data["current_document"] == HELLO
    with pytest.raises(ToolError, match="changed since"):
        call(
            box,
            "save_flow",
            {"name": "hello", "document": BYE, "message": "x", "base_version": "0" * 16},
        )
    assert box.workspace.read("hello") == HELLO


def test_save_refuses_invalid_flows(box):
    with pytest.raises(ToolError, match="problems") as info:
        call(
            box,
            "save_flow",
            {"name": "bad", "document": {"steps": {"a": {"block": "lgo"}}}, "message": "x"},
        )
    assert info.value.data["problems"]
    assert not (box.folder / "bad.json").exists()


def test_history_and_restore(box):
    version = call(box, "get_flow", {"name": "hello"})["version"]
    call(
        box,
        "save_flow",
        {"name": "hello", "document": BYE, "message": "replace", "base_version": version},
    )
    old = call(box, "flow_history", {"name": "hello"})["versions"][0]["id"]
    restored = call(box, "restore_flow_version", {"name": "hello", "version_id": old})
    assert restored["document"] == HELLO and box.workspace.read("hello") == HELLO


def test_templates_and_tests(box):
    template = call(box, "list_templates", {})["templates"][0]["name"]
    created = call(
        box, "create_flow_from_template", {"template": template, "name": "from_template"}
    )
    assert created["version"] == box.workspace.file_version("from_template")
    assert call(box, "test_flow", {"name": "hello"})["total"] == 0


def test_environment_values_never_leave(box):
    assert call(box, "list_environments", {}) == {"environments": ["prod"]}
    everything = json.dumps(
        [call(box, name, {}) for name in ("list_flows", "list_environments", "list_templates")]
    )
    assert "supersecret" not in everything


def good_block(name: str) -> str:
    return f'from miniflow.blocks import Setting, block\n\n@block("{name}", settings=[Setting("text", str)])\ndef run(text):\n    """Echo."""\n    return text\n'


def test_draft_tools(box):
    name = "blk_" + uuid.uuid4().hex[:10]
    result = call(box, "draft_block", {"name": name, "source": good_block(name)})
    assert result["drafted"] and "Pending blocks" in result["next"]
    assert [d["name"] for d in call(box, "list_block_drafts", {})["drafts"]] == [name]
    with pytest.raises(ToolError, match="problems") as info:
        call(box, "draft_block", {"name": name + "x", "source": "print(1)\n"})
    assert info.value.data["checks"]


@given(
    name=st.sampled_from(sorted(TOOLS)),
    args=st.dictionaries(
        st.sampled_from(["name", "document", "message", "source", "group", "run_id", "limit", "x"]),
        st.none() | st.booleans() | st.integers() | st.text(max_size=12) | st.just({}),
        max_size=4,
    ),
)
@settings(
    max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_any_input_gives_a_result_or_a_tool_error(box, name, args):
    try:
        result = call(box, name, args)
    except (ToolError, UnknownTool):
        return
    assert isinstance(result, dict)
    json.dumps(result, default=str)
```

- [ ] **Step 6: Run them to see them fail**

Run: `uv run pytest tests/test_agent_tools.py -v`
Expected: ERROR, `No module named 'miniflow.agent.tools'`.

- [ ] **Step 7: Create `miniflow/agent/tools.py`** (run tools come in Task 8; this file already declares `RunLink` and the `run` group)

```python
"""The tools an agent uses to build and run MiniFlow flows (standard library only).

Each tool is a function ``fn(box, args) -> dict`` registered with :func:`tool`: a name, a
description written for the agent, a JSON Schema for its arguments, and a group: ``read``
(changes nothing), ``write`` (flows and block drafts) or ``run`` (starts, cancels and reads
runs). :mod:`miniflow.agent.mcp` serves them over MCP; an Editor panel can call the same list.

    Toolbox(flows, db, *, read_only=False, runs=None)   what the tools work on
    available(*, read_only, runs) -> [Tool]             read only: no write or run tools;
                                                        runs=False: no run tools
    call(box, name, args) -> dict                       UnknownTool, or ToolError for an
                                                        expected failure (bad input, invalid
                                                        flow, conflict, missing flow)

``runs`` is a :class:`RunLink` (normally :class:`miniflow.agent.owner.DashboardLink`): run
tools never start runs in this process, so the dashboard that owns the database keeps one
queue. A :class:`ToolError` has a message for the agent and optional ``data``, for example
the current document after a conflicting save. Every save an agent makes is labelled
``(agent)`` in the flow's history. Environment values are never returned.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from miniflow.agent.drafts import DraftError, list_drafts, write_draft
from miniflow.blocks import BlockError, Setting, get_block
from miniflow.flowfile import FlowFileError
from miniflow.ui.author import FlowWorkspace, VersionConflict, WorkspaceError, block_catalog

__all__ = [
    "TOOLS",
    "RunLink",
    "Tool",
    "ToolError",
    "Toolbox",
    "UnknownTool",
    "available",
    "call",
    "tool",
]

Group = Literal["read", "write", "run"]


class ToolError(Exception):
    def __init__(self, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.data = data


class UnknownTool(LookupError):
    pass


class RunLink(Protocol):
    def url(self) -> str: ...
    def run(self, flow: str, environment: str | None) -> dict[str, Any]: ...
    def cancel(self, run_id: str) -> dict[str, Any]: ...
    def control(self, *, start: bool = True) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    group: Group
    fn: Callable[[Toolbox, dict[str, Any]], dict[str, Any]]


TOOLS: dict[str, Tool] = {}


def tool(
    name: str,
    description: str,
    properties: dict[str, dict[str, Any]] | None = None,
    required: Iterable[str] = (),
    group: Group = "read",
) -> Callable[
    [Callable[[Toolbox, dict[str, Any]], dict[str, Any]]],
    Callable[[Toolbox, dict[str, Any]], dict[str, Any]],
]:
    schema = {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
    }

    def register(
        fn: Callable[[Toolbox, dict[str, Any]], dict[str, Any]],
    ) -> Callable[[Toolbox, dict[str, Any]], dict[str, Any]]:
        TOOLS[name] = Tool(name, description, schema, group, fn)
        return fn

    return register


def available(*, read_only: bool, runs: bool) -> list[Tool]:
    def shown(t: Tool) -> bool:
        if t.group == "read":
            return True
        if read_only:
            return False
        return t.group == "write" or runs

    return [t for t in TOOLS.values() if shown(t)]


class Toolbox:
    def __init__(
        self,
        flows: str | Path,
        db: str | Path,
        *,
        read_only: bool = False,
        runs: RunLink | None = None,
    ) -> None:
        self.workspace = FlowWorkspace(flows)
        self.folder = self.workspace.folder
        self.db = Path(db).resolve()
        self.runs = runs
        self.tools = {t.name: t for t in available(read_only=read_only, runs=runs is not None)}


_JSON_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _check_args(t: Tool, args: object) -> dict[str, Any]:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    props: dict[str, dict[str, Any]] = t.schema["properties"]
    unknown = sorted(set(args) - set(props))
    if unknown:
        raise ToolError(f"unknown argument(s): {', '.join(map(str, unknown))}")
    for key in t.schema["required"]:
        if key not in args:
            raise ToolError(f"{key} is required")
    for key, value in args.items():
        kind = props[key]["type"]
        want = _JSON_TYPES[kind]
        if not isinstance(value, want) or (want is int and isinstance(value, bool)):
            raise ToolError(f"{key} must be {kind}")
    return args


def call(box: Toolbox, name: str, args: object) -> dict[str, Any]:
    t = box.tools.get(name)
    if t is None:
        raise UnknownTool(name)
    checked = _check_args(t, args)
    try:
        return t.fn(box, checked)
    except ToolError:
        raise
    except FileNotFoundError:
        raise ToolError(f"there is no flow called {checked.get('name')!r}") from None
    except DraftError as exc:
        raise ToolError(str(exc), exc.report) from None
    except FlowFileError as exc:
        raise ToolError("; ".join(exc.problems), {"problems": exc.problems}) from None
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"the flow file isn't valid JSON (line {exc.lineno}, column {exc.colno})"
        ) from None
    except (WorkspaceError, BlockError) as exc:
        raise ToolError(str(exc)) from None


# ------------------------------------------------------------------ helpers
_NAME = {
    "type": "string",
    "description": "flow name: letters, digits, _ or -, starting with a letter",
}
_DOCUMENT = {
    "type": "object",
    "description": "the whole JSON flow document, as get_flow returns it",
}
_PLACEHOLDERS: dict[type, Any] = {str: "text", int: 0, float: 0, bool: False, list: [], dict: {}}


def _placeholder(s: Setting) -> Any:
    if s.secret:
        return "{{ env.NAME }}"
    if s.choices:
        return s.choices[0]
    first = s.type[0] if isinstance(s.type, tuple) else s.type
    return _PLACEHOLDERS.get(first, "value")


def _flow(box: Toolbox, name: str) -> dict[str, Any]:
    return {
        "name": name,
        "document": box.workspace.read(name),
        "version": box.workspace.file_version(name),
    }


# --------------------------------------------------------------------- read
@tool(
    "list_blocks",
    "List the blocks a flow step can use, with each setting's type, default and whether it is "
    "required or secret. Includes approved plugin blocks from the flows folder.",
    {
        "group": {
            "type": "string",
            "description": "only this palette group, e.g. Transform or Plugins",
        }
    },
)
def _list_blocks(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    blocks = box.workspace.catalog()
    if "group" in args:
        blocks = [b for b in blocks if b["group"].lower() == args["group"].lower()]
    return {"blocks": blocks}


@tool(
    "describe_block",
    "One block in detail: its settings, documentation and an example step to paste into a flow.",
    {"name": {"type": "string", "description": "block name"}},
    ["name"],
)
def _describe_block(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    box.workspace.catalog()  # loads the folder's plugin blocks
    spec = get_block(args["name"])
    entry = next(b for b in block_catalog() if b["name"] == spec.name)
    example = {
        "block": spec.name,
        "settings": {s.name: _placeholder(s) for s in spec.settings if s.required},
    }
    return {**entry, "doc": inspect.getdoc(spec.fn) or "", "example_step": example}


@tool(
    "list_flows",
    "List the flows in the folder: description, whether each is valid, its schedules and version.",
)
def _list_flows(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    flows = []
    for name in box.workspace.list():
        try:
            document = box.workspace.read(name)
        except (OSError, ValueError) as exc:
            flows.append({"name": name, "valid": False, "problems": [f"can't read it: {exc}"]})
            continue
        result = box.workspace.validate(name, document)
        doc = document if isinstance(document, dict) else {}
        schedules = doc.get("schedules")
        flows.append(
            {
                "name": name,
                "description": str(doc.get("description", "")),
                "valid": result["valid"],
                "problems": result["problems"],
                "schedules": sorted(schedules) if isinstance(schedules, dict) else [],
                "version": box.workspace.file_version(name),
            }
        )
    return {"flows": flows}


@tool(
    "get_flow",
    "Read a flow's JSON document and its version. Pass the version as base_version when saving changes.",
    {"name": _NAME},
    ["name"],
)
def _get_flow(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    return _flow(box, args["name"])


@tool("list_templates", "Starter flows that create_flow_from_template can copy.")
def _list_templates(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    return {"templates": box.workspace.templates()}


@tool(
    "list_environments", "Names of the environments a run can use (their values are never shown)."
)
def _list_environments(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    return {"environments": box.workspace.environments()}


@tool(
    "flow_history",
    "Earlier saved versions of a flow, newest first, with who saved them and why.",
    {"name": _NAME},
    ["name"],
)
def _flow_history(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    return {"versions": box.workspace.versions(args["name"])}


@tool("list_block_drafts", "Blocks drafted for a person to approve, with their check results.")
def _list_block_drafts(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    return {"drafts": list_drafts(box.folder)}


@tool(
    "validate_flow",
    "Check a flow document without saving it. Returns every problem in plain language.",
    {"name": _NAME, "document": _DOCUMENT},
    ["name", "document"],
)
def _validate_flow(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    result = box.workspace.validate(args["name"], args["document"])
    return {
        "valid": result["valid"],
        "problems": result["problems"],
        "steps": result.get("steps", []),
    }


@tool(
    "test_flow",
    "Run the sample-input checks declared in a flow's tests section (the saved flow, or document if given).",
    {"name": _NAME, "document": _DOCUMENT},
    ["name"],
)
def _test_flow(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    document = args["document"] if "document" in args else box.workspace.read(args["name"])
    reports = box.workspace.test(args["name"], document)
    return {
        "passed": sum(bool(r["passed"]) for r in reports),
        "total": len(reports),
        "tests": reports,
    }


# -------------------------------------------------------------------- write
@tool(
    "save_flow",
    "Save a flow after validating it. For an existing flow, pass the version from get_flow as "
    "base_version; if the person changed it meanwhile the save is refused with the current "
    "document so you can merge. Leave base_version out for a new flow. The message goes into "
    "the flow's history.",
    {
        "name": _NAME,
        "document": _DOCUMENT,
        "message": {"type": "string", "description": "what changed, for the history"},
        "base_version": {"type": "string", "description": "version from get_flow"},
    },
    ["name", "document", "message"],
    group="write",
)
def _save_flow(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    name = args["name"]
    try:
        result = box.workspace.save(
            name,
            args["document"],
            message=f"{args['message']} (agent)".strip(),
            expect_version=True,
            base_version=args.get("base_version"),
        )
    except VersionConflict as exc:
        data: dict[str, Any] = {"current_version": exc.current}
        if exc.current is not None:
            data["current_document"] = box.workspace.read(name)
        raise ToolError(str(exc), data) from None
    if not result["valid"]:
        raise ToolError(
            "the flow has problems, so it wasn't saved", {"problems": result["problems"]}
        )
    return {
        "saved": True,
        "name": name,
        "version": box.workspace.file_version(name),
        "steps": result["steps"],
    }


@tool(
    "create_flow_from_template",
    "Create a new flow (and its sample data) from a starter template.",
    {"template": {"type": "string"}, "name": _NAME},
    ["template", "name"],
    group="write",
)
def _create_flow_from_template(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    box.workspace.create_template(args["template"], args["name"])
    return _flow(box, args["name"])


@tool(
    "restore_flow_version",
    "Make an earlier version (an id from flow_history) current again. The replaced version stays in the history.",
    {"name": _NAME, "version_id": {"type": "string"}},
    ["name", "version_id"],
    group="write",
)
def _restore_flow_version(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    name, vid = args["name"], args["version_id"]
    try:
        document = box.workspace.version(name, vid)
    except FileNotFoundError:
        raise ToolError(f"flow {name!r} has no version {vid!r}; see flow_history") from None
    result = box.workspace.save(name, document, message=f"restored {vid} (agent)")
    if not result["valid"]:
        raise ToolError("that version no longer validates", {"problems": result["problems"]})
    return _flow(box, name)


@tool(
    "draft_block",
    "Propose a new block written in Python. It is checked without running it and saved as a "
    "draft; nothing runs until a person approves it in the MiniFlow Editor. Blocks look like: "
    'from miniflow.blocks import Setting, block / @block("name", settings=[Setting("x", str)]) '
    "/ def fn(x): ... returning JSON-like data. Top-level code may only be imports, functions and "
    "plain constants.",
    {
        "name": {"type": "string", "description": "block name: lowercase letters, digits and _"},
        "source": {"type": "string", "description": "the whole Python file"},
        "replace": {"type": "boolean", "description": "replace a pending draft of the same name"},
    },
    ["name", "source"],
    group="write",
)
def _draft_block(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    draft = write_draft(
        box.folder, args["name"], args["source"], replace=args.get("replace", False)
    )
    return {
        "drafted": True,
        "name": draft["name"],
        "checks": draft["checks"],
        "imports": draft["imports"],
        "next": "Ask the person to review and approve it in the MiniFlow Editor under Pending "
        f"blocks (or: miniflow approve-block {draft['name']} --flows {box.folder}). Flows can't "
        "use it until then.",
    }
```

Run `uv run ruff format miniflow/agent/tools.py` afterwards.

- [ ] **Step 8: Run the tests, lint and types**

Run: `uv run pytest tests/test_agent_tools.py tests/test_editor_v2.py -v && uv run ruff check miniflow && uv run mypy`
Expected: all PASS. If the hypothesis test finds an exception type that escapes `call`, decide whether it is a bad-input case (map it to `ToolError` in `call`) or a bug (fix the tool); don't widen the mapping to `Exception`.

- [ ] **Step 9: Commit**

```bash
git add miniflow/ui/author.py miniflow/agent/tools.py tests/test_agent_tools.py tests/test_editor_v2.py
git commit -m "Agent tools: read, validate, save with version checks, block drafts"
```

---

### Task 8: Runs through the owning dashboard (`DashboardLink`, run tools)

**Files:**
- Create: `miniflow/agent/owner.py`
- Modify: `miniflow/agent/tools.py` (append run tools; map `LinkError` in `call`)
- Test: `tests/test_agent_owner.py`, `tests/test_agent_tools.py` (append)

**Interfaces:**
- Consumes: `read_owner`, `is_live` (Task 2); `RunLink`, `tool`, `ToolError` (Task 7); `_interrupt`, `_kill_tree`, `_CREATIONFLAGS`, `_NEW_SESSION` from `miniflow.ui.control`; `StateStore`.
- Produces (used by Task 9):
  - `class LinkError(Exception)`
  - `DashboardLink(flows, db, *, start: bool = True, timeout: float = 15.0)` implementing `RunLink`, plus `close() -> None`
  - tools `run_flow`, `get_run`, `list_runs`, `cancel_run` (group `run`); `run_flow` result: `{"state": "started", "run_id": str | None, "dashboard": url, "database": str}` or `{"state": "queued", "position": int | None, "dashboard": url, "database": str}`

- [ ] **Step 1: Write the failing run-tool tests** (append to `tests/test_agent_tools.py`)

```python
from miniflow import DAG, StateStore, task
from tests import _sched_flows as F


class RecordingLink(FakeLink):
    """Starts are recorded as a finished run in the store, as the dashboard's child would."""

    def __init__(self, db: Path) -> None:
        super().__init__()
        self.db = db

    def run(self, flow: str, environment: str | None) -> dict[str, Any]:
        with StateStore(self.db) as store:
            run_id = store.create_run(flow, DAG.from_tasks([task(F.const, name="a", args=(1,))]))
            store.finish_run(run_id, "SUCCESS")
        return {"action": "run", "flow": flow, "run_id": None}


def test_run_flow_finds_the_new_run_id(flows, tmp_path):
    db = tmp_path / "s.db"
    box = Toolbox(flows, db, runs=RecordingLink(db))
    result = call(box, "run_flow", {"name": "hello"})
    assert result["state"] == "started" and result["run_id"]
    assert result["database"] == str(db.resolve()) and result["dashboard"].startswith("http://")
    run = call(box, "get_run", {"run_id": result["run_id"]})
    assert run["status"] == "SUCCESS" and run["finished"] and run["steps"][0]["name"] == "a"
    listed = call(box, "list_runs", {"flow": "hello"})
    assert [r["run_id"] for r in listed["runs"]] == [result["run_id"]]


def test_run_flow_reports_a_queued_run(box):
    result = call(box, "run_flow", {"name": "hello"})
    assert result == {
        "state": "queued",
        "position": 1,
        "dashboard": "http://127.0.0.1:1",
        "database": str(box.db),
    }


def test_unknown_runs_and_limits(box):
    with pytest.raises(ToolError, match="no run"):
        call(box, "get_run", {"run_id": "nope"})
    with pytest.raises(ToolError, match="limit"):
        call(box, "list_runs", {"limit": 0})
```

- [ ] **Step 2: Write the failing link tests** (`tests/test_agent_owner.py`)

```python
"""DashboardLink: runs from an agent go to the one dashboard that owns the database."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from miniflow import StateStore
from miniflow.agent.owner import DashboardLink, LinkError
from miniflow.ui import DashboardServer
from miniflow.ui.owner import is_live, owner_path, read_owner

HELLO = {"steps": {"hello": {"block": "log", "settings": {"message": "hi"}}}}
SLOW = {"steps": {"pause": {"block": "wait", "settings": {"seconds": 3}}}}


def wait_until(cond, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


@pytest.fixture
def flows(tmp_path: Path) -> Path:
    d = tmp_path / "flows"
    d.mkdir()
    (d / "hello.json").write_text(json.dumps(HELLO), "utf-8")
    (d / "slow.json").write_text(json.dumps(SLOW), "utf-8")
    return d


def statuses(db: Path, flow: str) -> list[str]:
    with StateStore(db) as store:
        return [r.status for r in store.list_runs() if r.flow == flow]


def test_without_a_dashboard_the_link_starts_one_and_stops_it(tmp_path, flows):
    db = tmp_path / "s.db"
    link = DashboardLink(flows, db)
    try:
        link.run("hello", None)
        wait_until(lambda: statuses(db, "hello") == ["SUCCESS"])
        owner = read_owner(db)
        assert owner is not None and owner.url == link.url()
    finally:
        link.close()
    wait_until(lambda: not owner_path(db).exists())


def test_an_existing_dashboard_is_used(tmp_path, flows):
    db = tmp_path / "s.db"
    with DashboardServer(db, port=0, flows=flows) as server:
        link = DashboardLink(flows, db, start=False)
        assert link.url() == server.url
        link.run("hello", None)
        wait_until(lambda: statuses(db, "hello") == ["SUCCESS"])
        link.close()  # it didn't start this dashboard, so it leaves it alone
        assert is_live(read_owner(db))


def test_agent_runs_share_the_dashboards_queue(tmp_path, flows):
    db = tmp_path / "s.db"
    with DashboardServer(db, port=0, flows=flows) as server:
        server.controller.start("slow", 0)
        link = DashboardLink(flows, db, start=False)
        assert link.run("slow", None).get("queued") is True
        assert [q["flow"] for q in link.control()["queued"]] == ["slow"]


def test_a_dashboard_for_another_folder_is_an_error(tmp_path, flows):
    other = tmp_path / "other"
    other.mkdir()
    db = tmp_path / "s.db"
    with DashboardServer(db, port=0, flows=other):
        with pytest.raises(LinkError, match="runs flows from"):
            DashboardLink(flows, db, start=False).url()


def test_no_dashboard_and_no_starting(tmp_path, flows):
    with pytest.raises(LinkError, match="no dashboard"):
        DashboardLink(flows, tmp_path / "s.db", start=False).url()
    assert DashboardLink(flows, tmp_path / "s.db", start=False).control(start=False) == {
        "running": [],
        "queued": [],
    }


def test_two_links_starting_together_share_one_dashboard(tmp_path, flows):
    db = tmp_path / "s.db"
    links = [DashboardLink(flows, db), DashboardLink(flows, db)]
    urls: list[str] = []
    threads = [threading.Thread(target=lambda l=link: urls.append(l.url())) for link in links]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(40)
        assert len(urls) == 2 and urls[0] == urls[1]
    finally:
        for link in links:
            link.close()


def test_a_dashboard_left_running_is_reused(tmp_path, flows):
    db = tmp_path / "s.db"
    first = DashboardLink(flows, db)
    try:
        url = first.url()  # the first MCP server "crashes" here: close() never runs
        second = DashboardLink(flows, db)
        assert second.url() == url
        second.close()
        assert is_live(read_owner(db))
    finally:
        first.close()
```

- [ ] **Step 3: Run them to see them fail**

Run: `uv run pytest tests/test_agent_owner.py tests/test_agent_tools.py -k "run or link or dashboard" -v`
Expected: ERROR, `No module named 'miniflow.agent.owner'`; run tools are `UnknownTool`.

- [ ] **Step 4: Create `miniflow/agent/owner.py`**

```python
"""The MCP server's link to the dashboard that runs flows (standard library only).

Exactly one dashboard runs flows for a state database (:mod:`miniflow.ui.owner`). The MCP
server never runs flows itself; :class:`DashboardLink` sends run requests to that dashboard:

    DashboardLink(flows, db, *, start=True, timeout=15.0)
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
        self, flows: str | Path, db: str | Path, *, start: bool = True, timeout: float = 15.0
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
        self._stderr = tempfile.TemporaryFile()  # noqa: SIM115 - read in _tail, closed in close
        command = [
            sys.executable, "-m", "miniflow", "serve",
            "--flows", str(self.flows), "--db", str(self.db), "--no-browser",
        ]  # fmt: skip
        self._child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,  # stdout belongs to the MCP protocol
            stderr=self._stderr,
            creationflags=_CREATIONFLAGS,
            start_new_session=_NEW_SESSION,
        )

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
        except (OSError, ValueError) as exc:
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
```

(`_interrupt` and friends are module-private in `control.py` but live in the same package; reuse them rather than copying the Windows/POSIX branches.)

- [ ] **Step 5: Add the run tools** (append to `miniflow/agent/tools.py`; add imports `import time`, `from dataclasses import asdict`, `from miniflow.agent.owner import LinkError`, `from miniflow.store import StateStore`; in `call`, add `except LinkError as exc: raise ToolError(str(exc)) from None` next to the other mappings; extend the docstring list with the four run tools)

```python
# --------------------------------------------------------------------- runs
_RUN_ID_WAIT = 5.0


def _link(box: Toolbox) -> RunLink:
    if box.runs is None:  # run tools are hidden then; this guards direct calls
        raise ToolError("runs are turned off for this server")
    return box.runs


def _newest_run(box: Toolbox, flow: str, since: float) -> str | None:
    with StateStore(box.db) as store:
        return next(
            (r.run_id for r in store.list_runs() if r.flow == flow and r.started_at >= since), None
        )


@tool(
    "run_flow",
    "Start a run of a saved flow and return at once (it may take up to a few seconds to learn "
    "the run id). Runs happen in the MiniFlow dashboard, in the same queue as the person's Run "
    "button; one is started for you if none is running. Check on it later with get_run. If the "
    "result says queued, call list_runs later to find its run id.",
    {
        "name": _NAME,
        "environment": {"type": "string", "description": "an environment from list_environments"},
    },
    ["name"],
    group="run",
)
def _run_flow(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    link = _link(box)
    name = args["name"]
    since = time.time() - 1.0  # clock tolerance between this process and the run's
    active = link.run(name, args.get("environment"))
    where = {"dashboard": link.url(), "database": str(box.db)}
    if active.get("queued"):
        queue = link.control().get("queued", [])
        position = next((i + 1 for i, q in enumerate(queue) if q.get("flow") == name), None)
        return {"state": "queued", "position": position, **where}
    run_id = active.get("run_id")
    deadline = time.monotonic() + _RUN_ID_WAIT
    while run_id is None and time.monotonic() < deadline:
        run_id = _newest_run(box, name, since)
        if run_id is None:
            time.sleep(0.1)
    return {"state": "started", "run_id": run_id, **where}


@tool(
    "get_run",
    "How a run is going or went: its status, and for each step its state, attempts, error and a preview of its result.",
    {"run_id": {"type": "string"}},
    ["run_id"],
    group="run",
)
def _get_run(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    with StateStore(box.db) as store:
        meta = next((r for r in store.list_runs() if r.run_id == args["run_id"]), None)
        if meta is None:
            raise ToolError(f"there is no run {args['run_id']!r}")
        tasks = store.changes_since(meta.run_id, 0.0)
    steps = [
        {
            "name": t.task, "state": t.state.value, "attempts": t.attempts, "error": t.error,
            "preview": t.preview, "from_cache": t.from_cache,
            "started_at": t.started_at, "finished_at": t.finished_at,
        }
        for t in tasks
    ]  # fmt: skip
    return {**asdict(meta), "finished": meta.finished_at is not None, "steps": steps}


@tool(
    "list_runs",
    "Recent runs, newest first, with how many steps are in each state, then runs waiting in the queue.",
    {
        "flow": {"type": "string"},
        "limit": {"type": "integer", "description": "1 to 200, default 20"},
    },
    group="run",
)
def _list_runs(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    limit = args.get("limit", 20)
    if not 1 <= limit <= 200:
        raise ToolError("limit must be from 1 to 200")
    flow = args.get("flow")
    with StateStore(box.db) as store:
        runs = [r for r in store.list_runs() if flow is None or r.flow == flow][:limit]
        out = []
        for r in runs:
            counts: dict[str, int] = {}
            for state in store.get_states(r.run_id).values():
                counts[state.value] = counts.get(state.value, 0) + 1
            out.append({**asdict(r), "counts": counts})
    queued = [
        q
        for q in _link(box).control(start=False).get("queued", [])
        if flow is None or q.get("flow") == flow
    ]
    return {"runs": out, "queued": queued}


@tool(
    "cancel_run",
    "Stop an active run. It ends INTERRUPTED and the person can resume it from the dashboard.",
    {"run_id": {"type": "string"}},
    ["run_id"],
    group="run",
)
def _cancel_run(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    _link(box).cancel(args["run_id"])
    return _get_run(box, args)
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_agent_owner.py tests/test_agent_tools.py tests/test_owner.py -v && uv run mypy`
Expected: all PASS. These tests start real dashboards and runs; each should finish in a few seconds. If `test_two_links_starting_together_share_one_dashboard` is slow on Windows, keep it but make sure it stays well under 60 s.

- [ ] **Step 7: Commit**

```bash
git add miniflow/agent/owner.py miniflow/agent/tools.py tests/test_agent_owner.py tests/test_agent_tools.py
git commit -m "Agent tools: runs go through the dashboard that owns the database"
```

---

### Task 9: MCP over stdio and `miniflow mcp`

**Files:**
- Create: `miniflow/agent/mcp.py`
- Modify: `miniflow/cli.py` (`_cmd_mcp`, parser)
- Test: `tests/test_agent_mcp.py`

**Interfaces:**
- Consumes: `Toolbox`, `call`, `ToolError`, `UnknownTool` (Task 7); `DashboardLink` (Task 8); `miniflow.__version__`.
- Produces: `McpServer(box)` with `handle(message) -> dict | None`, `handle_line(line) -> dict | None`, `serve(stdin, stdout) -> None`; `PROTOCOL_VERSIONS`; CLI `miniflow mcp --flows DIR [--db DB] [--read-only] [--no-runs]`.

- [ ] **Step 1: Write the failing tests** (`tests/test_agent_mcp.py`)

```python
"""MiniFlow's MCP server: JSON-RPC 2.0 over stdio."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from miniflow.agent import tools
from miniflow.agent.mcp import PROTOCOL_VERSIONS, McpServer
from miniflow.agent.tools import Toolbox

HELLO = {"steps": {"hello": {"block": "log", "settings": {"message": "hi"}}}}


@pytest.fixture
def flows(tmp_path: Path) -> Path:
    d = tmp_path / "flows"
    d.mkdir()
    (d / "hello.json").write_text(json.dumps(HELLO), "utf-8")
    return d


@pytest.fixture
def server(flows, tmp_path) -> McpServer:
    return McpServer(Toolbox(flows, tmp_path / "s.db"))


def req(method: str, params: dict | None = None, id_: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": id_,
        "method": method,
        **({"params": params} if params is not None else {}),
    }


@pytest.mark.parametrize(
    ("asked", "expected"), [("2025-06-18", "2025-06-18"), ("1999-01-01", PROTOCOL_VERSIONS[0])]
)
def test_initialize_negotiates_the_version(server, asked, expected):
    reply = server.handle(
        req(
            "initialize",
            {
                "protocolVersion": asked,
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        )
    )
    result = reply["result"]
    assert reply["id"] == 1 and result["protocolVersion"] == expected
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "miniflow" and "draft_block" in result["instructions"]


def test_notifications_get_no_reply(server):
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_ping(server):
    assert server.handle(req("ping")) == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_tools_list_matches_the_toolbox(server):
    listed = server.handle(req("tools/list"))["result"]["tools"]
    assert {t["name"] for t in listed} == set(server.box.tools)
    assert all(t["inputSchema"]["type"] == "object" for t in listed)


def test_a_tool_call_returns_structured_and_text_content(server):
    result = server.handle(req("tools/call", {"name": "get_flow", "arguments": {"name": "hello"}}))[
        "result"
    ]
    assert result["isError"] is False and result["structuredContent"]["document"] == HELLO
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_a_tool_error_is_a_result_the_agent_can_read(server):
    result = server.handle(req("tools/call", {"name": "get_flow", "arguments": {}}))["result"]
    assert result["isError"] is True and "name is required" in result["structuredContent"]["error"]


def test_an_unexpected_exception_is_reported_not_raised(server, monkeypatch):
    def boom(box, args):
        raise RuntimeError("kaput")

    monkeypatch.setitem(
        server.box.tools,
        "ping_me",
        tools.Tool(
            "ping_me", "test", {"type": "object", "properties": {}, "required": []}, "read", boom
        ),
    )
    result = server.handle(req("tools/call", {"name": "ping_me"}))["result"]
    assert (
        result["isError"] is True and "RuntimeError: kaput" in result["structuredContent"]["error"]
    )


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ("not json", -32700),
        ("[]", -32600),
        ('{"jsonrpc": "1.0", "id": 1, "method": "ping"}', -32600),
        ('{"jsonrpc": "2.0", "id": true, "method": "ping"}', -32600),
        ('{"jsonrpc": "2.0", "id": 1, "method": "nope"}', -32601),
        ('{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope"}}', -32602),
        ('{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}', -32602),
    ],
)
def test_protocol_errors(server, line, code):
    assert server.handle_line(line)["error"]["code"] == code


def test_a_printing_plugin_cannot_corrupt_stdout(flows, tmp_path):
    name = "noisy_" + uuid.uuid4().hex[:8]
    (flows / "blocks").mkdir()
    (flows / "blocks" / f"{name}.py").write_text(
        f'from miniflow.blocks import block\nprint("noise from {name}")\n\n@block("{name}")\ndef run():\n    return 1\n',
        "utf-8",
    )
    stdin = io.StringIO(
        json.dumps(req("tools/call", {"name": "list_blocks", "arguments": {}})) + "\n"
    )
    stdout = io.StringIO()
    McpServer(Toolbox(flows, tmp_path / "s.db")).serve(stdin, stdout)
    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1 and "noise" not in lines[0]
    assert name in {
        b["name"] for b in json.loads(lines[0])["result"]["structuredContent"]["blocks"]
    }


def test_a_real_subprocess_round_trip_in_utf8(flows, tmp_path):
    messages = [
        req(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
            1,
        ),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        req("tools/list", None, 2),
        req(
            "tools/call",
            {
                "name": "save_flow",
                "arguments": {
                    "name": "rupees",
                    "document": {"description": "₹ report", **HELLO},
                    "message": "₹",
                },
            },
            3,
        ),
        req("tools/call", {"name": "get_flow", "arguments": {"name": "rupees"}}, 4),
    ]
    stdin = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages).encode("utf-8")
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "miniflow",
            "mcp",
            "--flows",
            str(flows),
            "--db",
            str(tmp_path / "s.db"),
            "--no-runs",
        ],
        input=stdin,
        capture_output=True,
        timeout=50,
    )
    assert done.returncode == 0, done.stderr.decode(errors="replace")
    replies = [json.loads(line) for line in done.stdout.decode("utf-8").splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3, 4]
    assert "run_flow" not in {t["name"] for t in replies[1]["result"]["tools"]}
    assert replies[2]["result"]["isError"] is False
    assert replies[3]["result"]["structuredContent"]["document"]["description"] == "₹ report"


def test_mcp_needs_an_existing_flows_folder(tmp_path, capsys):
    from miniflow.cli import main

    assert main(["mcp", "--flows", str(tmp_path / "missing")]) == 2
    assert "doesn't exist" in capsys.readouterr().err
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_agent_mcp.py -v`
Expected: ERROR, `No module named 'miniflow.agent.mcp'`.

- [ ] **Step 3: Create `miniflow/agent/mcp.py`**

```python
"""MiniFlow as an MCP server over stdio (standard library only).

``miniflow mcp --flows DIR`` reads JSON-RPC 2.0 messages from stdin, one per line, and
writes one reply per line to stdout, following the Model Context Protocol's stdio
transport. It answers ``initialize``, ``ping``, ``tools/list`` and ``tools/call``, and
ignores notifications. The tools are :mod:`miniflow.agent.tools`.

    McpServer(box)
    .handle(message) -> dict | None     one decoded message in; its reply, or None for a
                                        notification
    .handle_line(line) -> dict | None   the same from one line of text, parse errors included
    .serve(stdin, stdout) -> None       until stdin closes

A tool's expected failure is a normal result with ``isError: true`` so the agent can read
it and correct itself; malformed requests get JSON-RPC errors. While serving,
``sys.stdout`` points at stderr, so a block that prints can't corrupt the stream.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, TextIO

from miniflow import __version__
from miniflow.agent.tools import Toolbox, ToolError, UnknownTool, call

__all__ = ["INSTRUCTIONS", "PROTOCOL_VERSIONS", "McpServer"]

log = logging.getLogger("miniflow.agent")

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "MiniFlow runs workflows made of blocks. To build a flow: find blocks with list_blocks or "
    "describe_block, write the flow document, call validate_flow until it reports no problems, "
    "then save_flow. Before changing a flow, read it with get_flow and pass its version as "
    "base_version. run_flow returns at once; check the run later with get_run. If no block "
    "does what is needed, propose one with draft_block; a person must approve it in the "
    "Editor before any flow can use it."
)
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _result(id_: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _content(payload: dict[str, Any], is_error: bool) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": _dumps(payload)}],
        "structuredContent": payload,
        "isError": is_error,
    }


class McpServer:
    def __init__(self, box: Toolbox) -> None:
        self.box = box

    def handle_line(self, line: str) -> dict[str, Any] | None:
        try:
            message = json.loads(line)
        except ValueError:
            return _error(None, PARSE_ERROR, "not valid JSON")
        return self.handle(message)

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return _error(
                None,
                INVALID_REQUEST,
                "expected one JSON-RPC 2.0 message (batches aren't supported)",
            )
        id_ = message.get("id")
        if "method" not in message and ("result" in message or "error" in message):
            return None  # a reply to a request this server never sends
        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _error(
                id_ if isinstance(id_, str | int) else None,
                INVALID_REQUEST,
                "expected a JSON-RPC 2.0 request",
            )
        if "id" not in message:
            return None  # a notification: initialized, cancelled, ...
        if isinstance(id_, bool) or not isinstance(id_, str | int):
            return _error(None, INVALID_REQUEST, "id must be a string or a number")
        params = message.get("params", {})
        if not isinstance(params, dict):
            return _error(id_, INVALID_PARAMS, "params must be an object")
        method = message["method"]
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(
                id_,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "miniflow", "version": __version__},
                    "instructions": INSTRUCTIONS,
                },
            )
        if method == "ping":
            return _result(id_, {})
        if method == "tools/list":
            tools = [
                {"name": t.name, "description": t.description, "inputSchema": t.schema}
                for t in self.box.tools.values()
            ]
            return _result(id_, {"tools": tools})
        if method == "tools/call":
            return self._call(id_, params)
        return _error(id_, METHOD_NOT_FOUND, f"unknown method {method!r}")

    def _call(self, id_: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            return _error(id_, INVALID_PARAMS, "name must be a string")
        try:
            result = call(self.box, name, params.get("arguments", {}))
        except UnknownTool:
            return _error(id_, INVALID_PARAMS, f"unknown tool {name!r}")
        except ToolError as exc:
            payload: dict[str, Any] = {"error": str(exc)}
            if exc.data:
                payload["details"] = exc.data
            return _result(id_, _content(payload, True))
        except Exception as exc:
            log.exception("tool %s failed", name)
            return _result(
                id_,
                _content({"error": f"internal error in {name}: {type(exc).__name__}: {exc}"}, True),
            )
        return _result(id_, _content(result, False))

    def serve(self, stdin: TextIO, stdout: TextIO) -> None:
        real_stdout = sys.stdout
        sys.stdout = sys.stderr  # print() from any block goes to stderr, not into the protocol
        try:
            for line in stdin:
                if not line.strip():
                    continue
                reply = self.handle_line(line)
                if reply is not None:
                    stdout.write(_dumps(reply) + "\n")
                    stdout.flush()
        finally:
            sys.stdout = real_stdout
```

- [ ] **Step 4: Add the CLI command** (in `miniflow/cli.py`; add `import io`)

```python
def _cmd_mcp(args: argparse.Namespace) -> int:
    from miniflow.agent.mcp import McpServer
    from miniflow.agent.owner import DashboardLink
    from miniflow.agent.tools import Toolbox

    flows = Path(args.flows)
    if not flows.is_dir():
        print(f"error: flows folder {flows} doesn't exist", file=sys.stderr)
        return 2
    link = None if args.read_only or args.no_runs else DashboardLink(flows, args.db)
    box = Toolbox(flows, args.db, read_only=args.read_only, runs=link)
    # MCP messages are UTF-8 whatever the console's encoding (Windows uses a code page).
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")
    try:
        McpServer(box).serve(stdin, stdout)
    except KeyboardInterrupt:
        pass
    finally:
        if link is not None:
            link.close()
        stdout.flush()
        stdin.detach()  # closing the wrappers would close the real stdin and stdout
        stdout.detach()
    return 0
```

Parser, after `approve-block`:

```python
m = sub.add_parser(
    "mcp", help="let an agent (Claude Code, Claude Desktop...) build and run flows over MCP"
)
m.add_argument("--flows", required=True, metavar="DIR", help="folder of JSON flows")
m.add_argument("--db", default=DEFAULT_DB, help=f"state database (default {DEFAULT_DB})")
m.add_argument("--read-only", action="store_true", help="only tools that change nothing")
m.add_argument("--no-runs", action="store_true", help="no run tools; never start a dashboard")
m.set_defaults(func=_cmd_mcp)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_agent_mcp.py -v && uv run mypy && uv run ruff check .`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add miniflow/agent/mcp.py miniflow/cli.py tests/test_agent_mcp.py
git commit -m "MCP server over stdio: miniflow mcp"
```

---

### Task 10: Docs, full check and the demo

**Files:**
- Modify: `README.md` (new section after "## Dashboard", before "## Evaluation"), `docs/PLAN.md` (new section after the V3 section)

**Interfaces:**
- Consumes: everything above.
- Produces: user documentation; nothing in code.

- [ ] **Step 1: README section**

Add after the Dashboard section:

````markdown
## Agents (MCP)

`miniflow mcp` lets an agent such as Claude Code or Claude Desktop build and run flows: it
reads the block list, writes and validates flows, saves them, runs them and reads the
results. Connect it to Claude Code with absolute paths:

```bash
claude mcp add miniflow -- uv run --directory /path/to/miniflow miniflow mcp --flows /path/to/flows --db /path/to/miniflow.db
```

For Claude Desktop, add this to its `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "miniflow": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/miniflow", "miniflow", "mcp",
               "--flows", "/path/to/flows", "--db", "/path/to/miniflow.db"]
    }
  }
}
```

- Every save the agent makes is validated first and labelled `(agent)` in the flow's
  history, so the Editor's History tab can undo it. It can't overwrite a flow you changed
  after it read it, and it never sees the values in your environment files.
- Runs go to the dashboard for that database (`miniflow serve`), in the same queue as your
  Run button. If none is running, the agent starts one and stops it when it exits.
- The agent can't install code. It can draft a block; you review it in the Editor under
  **Pending blocks** (or with `miniflow approve-block NAME --flows DIR`) and approve or
  discard it. An agent that can also run shell commands could write files itself, so for
  Claude Code the review is a check, and its own permission prompts are the guard.
- `--read-only` keeps only the tools that change nothing; `--no-runs` drops the run tools.
````

- [ ] **Step 2: PLAN.md entry**

Add after the V3 section:

```markdown
### V4 milestone 1: agents over MCP ✅
Design: `docs/superpowers/specs/2026-10-14-mcp-server-design.md`; plan:
`docs/superpowers/plans/2026-10-14-mcp-server.md`.
- `miniflow mcp`: a standard-library MCP server (`miniflow/agent/`) with tools to read blocks,
  validate, save (with version checks), test and run flows, and draft blocks.
- One dashboard owns each database (`<db>.owner.json`); the MCP server sends runs to it.
- Block drafts are checked with `ast` only and approved by a person in the Editor's Pending
  blocks panel or with `miniflow approve-block`.
- Next: an "Ask Claude" panel in the Editor on the same tools; a conflict check in the
  Editor's own save.
```

- [ ] **Step 3: Full verification**

Run each and read the output:

```bash
uv run pytest
```
Expected: all pass, none near the 60 s timeout.

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
```
Expected: no findings.

- [ ] **Step 4: End-to-end demo with Claude Code**

```bash
claude mcp add miniflow-dev -- uv run --directory "$PWD" miniflow mcp --flows "$PWD/examples" --db "$PWD/miniflow.db"
```

In a Claude Code session in another folder, ask: "Using miniflow, make a flow that reads `data/sales.csv`, converts amounts to rupees with `convert_currency`, keeps orders over 5000 and writes `big_orders.xlsx`; then run it." Confirm: it calls `list_blocks`, `validate_flow` (and fixes problems), `save_flow`, `run_flow`, `get_run`; the result names the dashboard; the flow is in the Editor with an `(agent)` history entry; the run is on the Runs tab. Then ask for a block that doesn't exist and approve it in the Editor's Pending blocks panel. Remove the server afterwards with `claude mcp remove miniflow-dev`, and delete the demo flow and `big_orders.xlsx` from `examples/` if they shouldn't be committed.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/PLAN.md
git commit -m "Docs: connecting an agent over MCP"
```
