from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path
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
