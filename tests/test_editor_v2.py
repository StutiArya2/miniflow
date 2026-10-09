"""Editor 2.0 server side: version history, diffs, restore, layout and signatures."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from miniflow.ui import DashboardServer
from miniflow.ui.author import FlowWorkspace, WorkspaceError, flow_diff
from tests.test_author import request

V1 = {"steps": {"hello": {"block": "log", "settings": {"message": "Hello"}}}}
V2 = {
    "steps": {
        "hello": {"block": "log", "settings": {"message": "Hello there", "level": "warning"}},
        "bye": {"block": "log", "settings": {"message": "Bye"}, "after": ["hello"]},
    }
}
V3 = {"steps": {"bye": {"block": "log", "settings": {"message": "Bye"}}}}


@pytest.fixture
def ws(tmp_path: Path) -> FlowWorkspace:
    return FlowWorkspace(tmp_path)


# ---------------------------------------------------------------- history
def test_saving_keeps_the_previous_version(ws: FlowWorkspace, tmp_path: Path):
    assert ws.versions("demo") == []
    ws.save("demo", V1)
    assert ws.versions("demo") == []  # nothing earlier to keep yet
    ws.save("demo", V2, message="add a goodbye")
    ws.save("demo", V3)
    versions = ws.versions("demo")
    assert [v["steps"] for v in versions] == [2, 1]  # newest first: V2, then V1
    assert versions[0]["message"] == "add a goodbye"
    assert versions[0]["user"]
    assert versions[0]["saved_at"] >= versions[1]["saved_at"]
    assert ws.version("demo", versions[1]["id"]) == V1
    assert json.loads((tmp_path / "demo.json").read_text(encoding="utf-8")) == V3
    assert (tmp_path / ".history" / "demo").is_dir()


def test_versions_saved_before_history_get_the_file_time(ws: FlowWorkspace, tmp_path: Path):
    (tmp_path / "demo.json").write_text(json.dumps(V1), encoding="utf-8")  # e.g. written by hand
    os.utime(tmp_path / "demo.json", (1_700_000_000, 1_700_000_000))
    ws.save("demo", V2)
    (old,) = ws.versions("demo")
    assert old["saved_at"] == 1_700_000_000


@pytest.mark.skipif(sys.platform == "win32", reason="the pwd module is POSIX-only")
def test_versions_record_the_os_account(ws: FlowWorkspace, monkeypatch):
    import pwd

    monkeypatch.setenv("USER", "someone-else")
    ws.save("demo", V1)
    ws.save("demo", V2)
    assert ws.versions("demo")[0]["user"] == pwd.getpwuid(os.getuid()).pw_name


def test_an_unchanged_save_adds_no_version(ws: FlowWorkspace):
    ws.save("demo", V1)
    ws.save("demo", V1)
    assert ws.versions("demo") == []


def test_invalid_saves_add_no_version(ws: FlowWorkspace):
    ws.save("demo", V1)
    assert ws.save("demo", {"steps": {"x": {"block": "nope"}}})["valid"] is False
    assert ws.versions("demo") == []


def test_restore_brings_back_a_version_and_keeps_the_current_one(ws: FlowWorkspace):
    ws.save("demo", V1)
    ws.save("demo", V2)
    first = ws.versions("demo")[0]["id"]
    result = ws.restore("demo", first)
    assert result["valid"]
    assert ws.read("demo") == V1
    newest = ws.versions("demo")[0]
    assert ws.version("demo", newest["id"]) == V2
    ws.save("demo", V3)
    assert ws.versions("demo")[0]["message"] == f"restored {first}"


def test_history_is_capped(tmp_path: Path):
    ws = FlowWorkspace(tmp_path, history_limit=3)
    for i in range(6):
        ws.save("demo", {"steps": {"s": {"block": "log", "settings": {"message": str(i)}}}})
    assert len(ws.versions("demo")) == 3


@pytest.mark.parametrize("bad", ["../demo", "x/../../y", "", "nope"])
def test_bad_version_ids_are_refused(ws: FlowWorkspace, bad: str):
    ws.save("demo", V1)
    with pytest.raises((WorkspaceError, FileNotFoundError)):
        ws.version("demo", bad)


# ------------------------------------------------------------------- diff
def test_diff_lists_added_removed_and_changed_steps():
    diff = flow_diff(V1, V2)
    assert diff["added"] == ["bye"]
    assert diff["removed"] == []
    assert diff["changed"] == {
        "hello": [
            {"field": "settings.level", "old": None, "new": "warning"},
            {"field": "settings.message", "old": "Hello", "new": "Hello there"},
        ]
    }
    assert flow_diff(V2, V3)["removed"] == ["hello"]
    assert flow_diff(V2, V3)["changed"] == {
        "bye": [{"field": "after", "old": ["hello"], "new": None}]
    }
    assert flow_diff(V1, V1) == {"added": [], "removed": [], "changed": {}, "flow": []}


def test_diff_reports_flow_level_changes():
    old = {"description": "a", "params": {"n": {"type": "int"}}, "steps": V1["steps"]}
    new = {"description": "b", "params": {"n": {"type": "float"}}, "steps": V1["steps"]}
    assert flow_diff(old, new)["flow"] == [
        {"field": "description", "old": "a", "new": "b"},
        {"field": "params", "old": {"n": {"type": "int"}}, "new": {"n": {"type": "float"}}},
    ]


# ------------------------------------------------------------------- HTTP
def test_history_over_http(tmp_path: Path):
    with DashboardServer(tmp_path / "runs.db", port=0, flows=tmp_path) as server:
        request(server, "POST", "/api/flows/save", {"name": "demo", "flow": V1, "message": "first"})
        request(server, "POST", "/api/flows/save", {"name": "demo", "flow": V2, "message": "more"})
        status, versions = request(server, "GET", "/api/flows/demo/versions")
        assert status == 200
        assert [v["message"] for v in versions] == ["first"]
        vid = versions[0]["id"]
        assert request(server, "GET", f"/api/flows/demo/versions/{vid}") == (200, V1)
        status, diff = request(server, "GET", f"/api/flows/demo/versions/{vid}/diff")
        assert status == 200
        assert diff["added"] == ["bye"]  # from that version to the current file
        status, result = request(
            server, "POST", "/api/flows/restore", {"name": "demo", "version": vid}
        )
        assert status == 200
        assert result["valid"]
        assert request(server, "GET", "/api/flows/demo") == (200, V1)
        assert request(server, "GET", "/api/flows/demo/versions/nope")[0] == 404


def test_layout_endpoint(tmp_path: Path):
    graph = {
        "tasks": {
            "a": {"after": []},
            "b": {"after": ["a"]},
            "c": {"after": ["a"]},
            "d": {"after": ["b", "c"]},
        }
    }
    with DashboardServer(tmp_path / "runs.db", port=0, flows=tmp_path) as server:
        status, layout = request(server, "POST", "/api/layout", {"graph": graph})
        assert status == 200
        assert [layout[n]["col"] for n in "abcd"] == [0, 1, 1, 2]
        assert request(server, "POST", "/api/layout", {"graph": {"tasks": "x"}})[0] == 400


def test_validate_returns_step_signatures(tmp_path: Path):
    ws = FlowWorkspace(tmp_path)
    result = ws.validate("demo", V2)
    assert set(result["signatures"]) == {"hello", "bye"}
    changed = ws.validate("demo", {"steps": {**V2["steps"], "hello": V1["steps"]["hello"]}})
    assert changed["signatures"]["hello"] != result["signatures"]["hello"]
    # Upstream changes aren't in a step's own signature; the editor marks children stale.
    assert changed["signatures"]["bye"] == result["signatures"]["bye"]


def test_editor_scripts_are_served(tmp_path: Path):
    import http.client
    from urllib.parse import urlsplit

    with DashboardServer(tmp_path / "runs.db", port=0, flows=tmp_path) as server:
        url = urlsplit(server.url)
        for name in ("editor-history.js", "editor-view.js", "editor-versions.js", "editor-data.js"):
            conn = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
            conn.request("GET", f"/static/{name}")
            res = conn.getresponse()
            body = res.read()
            conn.close()
            assert res.status == 200, name
            assert b"use strict" in body


def test_editor_signatures_ignore_the_files_a_step_writes(tmp_path: Path):
    """Writing a step's output file must not make the step look changed."""
    ws = FlowWorkspace(tmp_path)
    flow = {
        "steps": {"save": {"block": "write_json", "settings": {"path": "out.json", "data": [1]}}}
    }
    before = ws.validate("demo", flow)["signatures"]["save"]
    (tmp_path / "out.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert ws.validate("demo", flow)["signatures"]["save"] == before
    changed = {
        "steps": {"save": {"block": "write_json", "settings": {"path": "out.json", "data": [2]}}}
    }
    assert ws.validate("demo", changed)["signatures"]["save"] != before


def test_runs_record_the_editor_signatures(tmp_path: Path):
    from miniflow import StateStore
    from miniflow.cli import main

    flow = {
        "steps": {"save": {"block": "write_json", "settings": {"path": "out.json", "data": [1]}}}
    }
    (tmp_path / "demo.json").write_text(json.dumps(flow), encoding="utf-8")
    assert main(["run", str(tmp_path / "demo.json"), "--db", str(tmp_path / "s.db"), "-q"]) == 0
    with StateStore(tmp_path / "s.db") as store:
        graph = store.get_graph(store.latest_run("demo"))
    expected = FlowWorkspace(tmp_path).validate("demo", flow)["signatures"]["save"]
    assert graph["tasks"]["save"]["config_signature"] == expected


def test_editor_scripts_support_palette_drag_and_drop(tmp_path: Path):
    """The palette hands blocks to the canvas through the browser's drag-and-drop."""
    import http.client
    from urllib.parse import urlsplit

    with DashboardServer(tmp_path / "runs.db", port=0, flows=tmp_path) as server:
        url = urlsplit(server.url)

        def get(path: str) -> str:
            conn = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
            conn.request("GET", path)
            body = conn.getresponse().read().decode()
            conn.close()
            return body

        editor, view = get("/static/editor.js"), get("/static/editor-view.js")
    assert "studioDraggable(button" in editor  # blocks and reusable flows are draggable
    assert "studioDropInit(host)" in view  # the canvas accepts drops
    assert 'addEventListener("drop"' in view


def test_editor_reports_parts_that_failed_to_load(tmp_path: Path):
    """A dashboard older than the editor scripts must say so, not show an empty palette."""
    import http.client
    from urllib.parse import urlsplit

    with DashboardServer(tmp_path / "runs.db", port=0, flows=tmp_path) as server:
        url = urlsplit(server.url)
        conn = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
        conn.request("GET", "/static/editor.js")
        editor = conn.getresponse().read().decode()
        conn.close()
        served = {}
        for name in ("editor-history.js", "editor-view.js", "editor-versions.js", "editor-data.js"):
            conn = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
            conn.request("GET", f"/static/{name}")
            served[name] = conn.getresponse().read().decode()
            conn.close()
    # every function the editor checks for is defined by one of the scripts it loads
    start = editor.index("const STUDIO_PARTS = [") + len("const STUDIO_PARTS = [")
    parts = [p.strip().strip('"') for p in editor[start : editor.index("]", start)].split(",")]
    assert parts
    for part in parts:
        assert any(f"function {part}(" in text for text in [editor, *served.values()]), part
