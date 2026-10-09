from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path
from textwrap import dedent
from urllib.parse import urlsplit

import pytest
from hypothesis import given

from miniflow import DAG, Scheduler, State, StateStore, task
from miniflow.cli import main
from miniflow.ui import DashboardServer, layered_layout
from tests import _sched_flows as F
from tests.conftest import random_dags

EXAMPLE = Path(__file__).parent.parent / "examples" / "pipeline.py"


def graph_json(edges: dict[str, list[str]]) -> dict:
    return {"tasks": {n: {"kind": "io", "after": ups} for n, ups in edges.items()}}


# ------------------------------------------------------------------ layout
def test_diamond_layout():
    layout = layered_layout(graph_json({"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]}))
    assert layout["a"]["col"] == 0
    assert layout["b"]["col"] == layout["c"]["col"] == 1
    assert layout["d"]["col"] == 2
    assert {layout["b"]["row"], layout["c"]["row"]} == {0, 1}


def test_longest_path_decides_column():
    layout = layered_layout(graph_json({"a": [], "b": ["a"], "c": ["b"], "d": ["a", "c"]}))
    assert layout["d"]["col"] == 3


def test_barycenter_uncrosses_parallel_chains():
    # Children defined in the "wrong" order would cross without reordering.
    layout = layered_layout(graph_json({"r1": [], "r2": [], "c2": ["r2"], "c1": ["r1"]}))
    assert layout["c1"]["row"] == layout["r1"]["row"]
    assert layout["c2"]["row"] == layout["r2"]["row"]


def test_empty_layout():
    assert layered_layout({"tasks": {}}) == {}


@given(random_dags())
def test_layout_properties(edges):
    layout = layered_layout(graph_json(edges))
    assert set(layout) == set(edges)
    for n, ups in edges.items():
        for u in ups:
            assert layout[u]["col"] < layout[n]["col"]
    positions = [(p["col"], p["row"]) for p in layout.values()]
    assert len(set(positions)) == len(positions)
    by_col: dict[int, list[int]] = {}
    for p in layout.values():
        by_col.setdefault(p["col"], []).append(p["row"])
    for rows in by_col.values():
        assert sorted(rows) == list(range(len(rows)))


# ------------------------------------------------------------------ server
def get(server: DashboardServer, path: str) -> tuple[int, str, bytes]:
    url = urlsplit(server.url)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
    try:
        conn.request("GET", path)
        res = conn.getresponse()
        return res.status, res.getheader("Content-Type") or "", res.read()
    finally:
        conn.close()


@pytest.fixture
def finished_run(tmp_path):
    db = tmp_path / "state.db"
    tasks = [
        task(F.const, name="a", args=(1,)),
        task(F.add, name="b", after=["a"], args=(2,)),
        task(F.always_fail, name="bad", after=["a"], kwargs={"message": "nope"}),
        task(F.ignore, name="child", after=["bad"]),
    ]
    with StateStore(db) as store:
        result = Scheduler(DAG.from_tasks(tasks), store=store, flow="demo").run()
    return db, result.run_id


@pytest.fixture
def server(finished_run):
    db, _ = finished_run
    with DashboardServer(db, port=0) as s:
        yield s


def test_index_and_static_files(server):
    status, ctype, body = get(server, "/")
    assert status == 200
    assert ctype.startswith("text/html")
    assert b"MiniFlow" in body
    for name, kind in [("app.js", "javascript"), ("style.css", "css")]:
        status, ctype, body = get(server, f"/static/{name}")
        assert status == 200
        assert kind in ctype
        assert body


@pytest.mark.parametrize(
    "path", ["/static/../store.py", "/static/secret.py", "/nope", "/api", "/api/runs/x/y/z"]
)
def test_unknown_paths_are_404(server, path):
    assert get(server, path)[0] == 404


def test_runs_endpoint(server, finished_run):
    _, run_id = finished_run
    status, ctype, body = get(server, "/api/runs")
    assert status == 200
    assert ctype == "application/json"
    (run,) = json.loads(body)
    assert run["run_id"] == run_id
    assert run["flow"] == "demo"
    assert run["status"] == "FAILED"
    assert run["total"] == 4
    assert run["counts"]["SUCCESS"] == 2
    assert run["counts"]["FAILED"] == 1
    assert run["counts"]["SKIPPED"] == 1


def test_run_detail_endpoint(server, finished_run):
    _, run_id = finished_run
    status, _, body = get(server, f"/api/runs/{run_id}")
    assert status == 200
    d = json.loads(body)
    assert d["run"]["run_id"] == run_id
    assert set(d["graph"]["tasks"]) == {"a", "b", "bad", "child"}
    assert set(d["layout"]) == {"a", "b", "bad", "child"}
    states = {t["task"]: t["state"] for t in d["tasks"]}
    assert states == {"a": "SUCCESS", "b": "SUCCESS", "bad": "FAILED", "child": "SKIPPED"}
    bad = next(t for t in d["tasks"] if t["task"] == "bad")
    assert "ValueError: nope" in bad["error"]
    workers = {a["task"]: a["worker"] for a in d["attempts"]}
    assert workers["a"].startswith("thread-")
    assert d["since"] > 0


def test_missing_run_is_404(server):
    assert get(server, "/api/runs/doesnotexist")[0] == 404
    assert get(server, "/api/runs/doesnotexist/events")[0] == 404


def test_finished_run_stream_ends_with_done(server, finished_run):
    _, run_id = finished_run
    status, ctype, body = get(server, f"/api/runs/{run_id}/events")
    assert status == 200
    assert ctype == "text/event-stream"
    text = body.decode()
    assert "event: attempts" in text
    assert "event: run" in text
    assert text.rstrip().endswith('data: {"status":"FAILED"}')


def test_live_stream_pushes_changes(tmp_path):
    """SSE delivers state changes of a run that is still going, then 'done'."""
    db = tmp_path / "live.db"
    gate = threading.Event()

    def wait_for_gate() -> str:
        assert gate.wait(20)
        return "released"

    tasks = [task(wait_for_gate, name="blocked"), task(F.ignore, name="after", after=["blocked"])]
    store = StateStore(db)
    scheduler = Scheduler(DAG.from_tasks(tasks), store=store, flow="live")
    runner = threading.Thread(target=scheduler.run)
    runner.start()
    try:
        with DashboardServer(db, port=0) as server:
            deadline = time.monotonic() + 10
            run_id = None
            while run_id is None:
                assert time.monotonic() < deadline
                run_id = store.latest_run("live")
                time.sleep(0.02)
            url = urlsplit(server.url)
            conn = http.client.HTTPConnection(url.hostname, url.port, timeout=20)
            conn.request("GET", f"/api/runs/{run_id}/events")
            res = conn.getresponse()
            seen: list[str] = []
            states: dict[str, str] = {}
            while True:
                line = res.fp.readline().decode()
                assert line, "stream closed before 'done'"
                if line.startswith("event: "):
                    seen.append(line[7:].strip())
                elif line.startswith("data: ") and seen[-1] == "tasks":
                    for t in json.loads(line[6:])["tasks"]:
                        states[t["task"]] = t["state"]
                    if states.get("blocked") == "RUNNING":
                        gate.set()  # let the run finish while we're listening
                if seen and seen[-1] == "done":
                    break
            conn.close()
    finally:
        gate.set()
        runner.join(10)
        store.close()
    assert gate.is_set()
    assert states == {"blocked": "SUCCESS", "after": "SUCCESS"}
    assert seen.count("tasks") >= 2  # the initial snapshot and at least one live update


def test_memory_database_rejected():
    with pytest.raises(ValueError, match="database file"):
        DashboardServer(":memory:")


def test_busy_port_falls_back_to_next(tmp_path):
    db = tmp_path / "s.db"
    with DashboardServer(db, port=0) as first:
        port = first.server_address[1]
        with DashboardServer(db, port=port) as second:
            assert second.server_address[1] != port
            assert get(second, "/api/runs")[0] == 200


# --------------------------------------------------------------------- CLI
def test_run_with_ui_flag(tmp_path, capsys):
    db = tmp_path / "s.db"
    args = ["run", str(EXAMPLE), "--db", str(db), "--quiet", "--ui", "--no-ui-wait"]
    assert main([*args, "--no-browser", "--port", "0"]) == 0
    assert "dashboard: http://127.0.0.1:" in capsys.readouterr().err
    with StateStore(db) as store:
        run_id = store.latest_run()
        assert run_id is not None
        assert set(store.get_states(run_id).values()) == {State.SUCCESS}


def test_ui_command(tmp_path, capsys, monkeypatch):
    served = []
    monkeypatch.setattr(
        "miniflow.cli._serve_until_interrupted", lambda server: served.append(get(server, "/"))
    )
    assert main(["ui", "--db", str(tmp_path / "new.db"), "--no-browser", "--port", "0"]) == 0
    assert served[0][0] == 200
    err = capsys.readouterr().err
    assert "doesn't exist yet" in err
    assert "dashboard: http://127.0.0.1:" in err


# ----------------------------------------------------------------- control
CONTROL_FLOWS = {
    "hello": """
        import os

        @task
        def rate():
            return os.environ.get("MINIFLOW_FAIL_RATE")
        """,
    "sleepy": """
        import os, time

        @task
        def fast():
            return 21

        @task(after=[fast])
        def slow(x):
            if os.environ.get("MINIFLOW_FAIL_RATE") != "0":
                time.sleep(60)
            return x * 2
        """,
}


def call(
    server: DashboardServer,
    method: str,
    path: str,
    body: object = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, object]:
    url = urlsplit(server.url)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=30)
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    try:
        conn.request(
            method, path, body=data, headers={"Content-Type": "application/json", **(headers or {})}
        )
        res = conn.getresponse()
        return res.status, json.loads(res.read() or b"null")
    finally:
        conn.close()


def wait_until(cond, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


@pytest.fixture
def flows_dir(tmp_path):
    d = tmp_path / "flows"
    d.mkdir()
    for name, body in CONTROL_FLOWS.items():
        (d / f"{name}.py").write_text("from miniflow import task\n" + dedent(body), "utf-8")
    return d


@pytest.fixture
def control_server(tmp_path, flows_dir):
    with DashboardServer(tmp_path / "c.db", port=0, flows=flows_dir) as s:
        yield s


def idle(server: DashboardServer) -> bool:
    return call(server, "GET", "/api/control")[1]["active"] is None


def slow_is_running(server: DashboardServer) -> bool:
    run_id = server.store.latest_run("sleepy")
    return run_id is not None and server.store.get_states(run_id).get("slow") is State.RUNNING


def run_status(server: DashboardServer, run_id: str) -> str:
    return next(r.status for r in server.store.list_runs() if r.run_id == run_id)


def test_controls_are_off_without_flows(server):
    assert call(server, "GET", "/api/control")[0] == 404
    assert call(server, "POST", "/api/control/run", {"flow": "x", "fail_rate": 0})[0] == 404
    assert call(server, "POST", "/api/runs", {})[0] == 404


def test_control_status_lists_flows(control_server):
    assert call(control_server, "GET", "/api/control") == (
        200,
        {"flows": ["hello", "sleepy"], "active": None, "error": None, "queued": []},
    )


def test_run_button_starts_the_flow(control_server):
    status, body = call(
        control_server, "POST", "/api/control/run", {"flow": "hello", "fail_rate": 0.5}
    )
    assert status == 202
    assert body == {"action": "run", "flow": "hello", "run_id": None}
    wait_until(lambda: idle(control_server))
    run_id = control_server.store.latest_run("hello")
    assert run_id is not None
    assert run_status(control_server, run_id) == "SUCCESS"
    assert control_server.store.load_completed(run_id) == {"rate": "0.5"}


def test_cancel_then_resume(control_server):
    run = {"flow": "sleepy", "fail_rate": 0.4}
    assert call(control_server, "POST", "/api/control/run", run)[0] == 202
    wait_until(lambda: slow_is_running(control_server))
    first = control_server.store.latest_run("sleepy")

    status, body = call(control_server, "POST", "/api/control/cancel", {})
    assert status == 200
    assert body["active"] is None
    assert run_status(control_server, first) == "INTERRUPTED"

    status, body = call(
        control_server, "POST", "/api/control/resume", {"run_id": first, "fail_rate": 0}
    )
    assert status == 202
    assert body["action"] == "resume"
    wait_until(lambda: idle(control_server))
    second = control_server.store.latest_run("sleepy")
    assert second != first
    assert run_status(control_server, second) == "SUCCESS"


def test_cancel_with_nothing_running_is_409(control_server):
    status, body = call(control_server, "POST", "/api/control/cancel", {})
    assert status == 409
    assert "error" in body


@pytest.mark.parametrize(
    ("path", "body", "headers", "expected"),
    [
        ("/api/control/run", None, {"Host": "evil.example"}, 403),
        ("/api/control/run", None, {"Host": "evil.example:8765"}, 403),
        ("/api/control/run", None, {"Origin": "http://evil.example"}, 403),
        ("/api/control/run", None, {"Origin": "null"}, 403),
        ("/api/control/run", None, {"Content-Type": "text/plain"}, 415),
        ("/api/control/run", None, {"Content-Type": "application/x-www-form-urlencoded"}, 415),
        ("/api/control/run", b"{not json", None, 400),
        ("/api/control/run", [], None, 400),
        ("/api/control/run", {"fail_rate": 0}, None, 400),
        ("/api/control/run", {"flow": "hello"}, None, 400),
        ("/api/control/run", {"flow": "hello", "fail_rate": 2}, None, 400),
        ("/api/control/run", {"flow": "nope", "fail_rate": 0}, None, 404),
        ("/api/control/run", {"flow": "x" * 5000, "fail_rate": 0}, None, 413),
        ("/api/control/resume", {"run_id": 7, "fail_rate": 0}, None, 400),
        ("/api/control/resume", {"run_id": "nope", "fail_rate": 0}, None, 404),
        ("/api/control/explode", {}, None, 404),
    ],
)
def test_bad_control_requests_are_refused(control_server, path, body, headers, expected):
    body = {"flow": "hello", "fail_rate": 0} if body is None else body
    status, reply = call(control_server, "POST", path, body, headers)
    assert status == expected
    assert "error" in reply
    assert control_server.store.list_runs() == []
    assert idle(control_server)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_requests_from_the_dashboard_itself_are_allowed(control_server, host):
    port = control_server.server_address[1]
    headers = {"Host": f"{host}:{port}", "Origin": f"http://{host}:{port}"}
    status, _ = call(
        control_server, "POST", "/api/control/run", {"flow": "hello", "fail_rate": 0}, headers
    )
    assert status == 202
    wait_until(lambda: idle(control_server))


def test_stopping_the_dashboard_cancels_its_run(tmp_path, flows_dir):
    with DashboardServer(tmp_path / "c.db", port=0, flows=flows_dir) as server:
        call(server, "POST", "/api/control/run", {"flow": "sleepy", "fail_rate": 0.4})
        wait_until(lambda: slow_is_running(server))
        run_id = server.store.latest_run("sleepy")
    with StateStore(tmp_path / "c.db") as store:
        assert next(r.status for r in store.list_runs() if r.run_id == run_id) == "INTERRUPTED"


def test_missing_flows_folder_is_an_error(tmp_path):
    with pytest.raises(NotADirectoryError):
        DashboardServer(tmp_path / "c.db", port=0, flows=tmp_path / "nope")


def test_ui_command_with_flows(tmp_path, monkeypatch, flows_dir):
    served = []
    monkeypatch.setattr(
        "miniflow.cli._serve_until_interrupted",
        lambda server: served.append(call(server, "GET", "/api/control")),
    )
    args = ["ui", "--db", str(tmp_path / "c.db"), "--flows", str(flows_dir)]
    assert main([*args, "--no-browser", "--port", "0"]) == 0
    assert served == [
        (200, {"flows": ["hello", "sleepy"], "active": None, "error": None, "queued": []})
    ]


def test_ui_command_with_missing_flows_folder(tmp_path, capsys):
    args = ["ui", "--db", str(tmp_path / "c.db"), "--flows", str(tmp_path / "nope")]
    assert main([*args, "--no-browser", "--port", "0"]) == 2
    assert "nope" in capsys.readouterr().err
