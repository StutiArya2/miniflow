"""Browser authoring uses the same JSON-flow validator as the CLI."""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from urllib.parse import urlsplit

from miniflow.ui import DashboardServer
from miniflow.ui.author import FlowWorkspace, block_catalog


def request(server: DashboardServer, method: str, path: str, body: dict | None = None):
    url = urlsplit(server.url)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    try:
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def test_editor_can_validate_save_and_read_a_flow(tmp_path: Path):
    workspace = tmp_path / "flows"
    workspace.mkdir()
    with DashboardServer(tmp_path / "runs.db", port=0, flows=workspace) as server:
        flow = {"steps": {"message": {"block": "log", "settings": {"message": "Hello"}}}}
        assert request(server, "GET", "/api/flows") == (200, [])
        assert request(server, "POST", "/api/flows/validate", {"name": "hello", "flow": flow})[1][
            "valid"
        ]
        assert request(server, "POST", "/api/flows/save", {"name": "hello", "flow": flow})[0] == 200
        assert request(server, "GET", "/api/flows") == (200, ["hello"])
        assert request(server, "GET", "/api/flows/hello") == (200, flow)
        assert "hello" in server.controller.status()["flows"]


def test_invalid_and_escaping_flows_are_not_saved(tmp_path: Path):
    workspace = FlowWorkspace(tmp_path)
    invalid = {"steps": {"oops": {"block": "lgo", "settings": {}}}}
    result = workspace.save("broken", invalid)
    assert result["valid"] is False
    assert not (tmp_path / "broken.json").exists()
    with DashboardServer(tmp_path / "runs.db", port=0, flows=tmp_path) as server:
        status, _ = request(
            server, "POST", "/api/flows/save", {"name": "../escape", "flow": invalid}
        )
        assert status == 400
    assert not (tmp_path.parent / "escape.json").exists()


def test_block_catalog_has_form_fields_without_secret_defaults():
    blocks = {block["name"]: block for block in block_catalog()}
    assert {"read_csv", "filter", "email"} <= blocks.keys()
    password = next(item for item in blocks["email"]["settings"] if item["name"] == "password")
    assert password["secret"] is True
    assert "default" not in password


def test_workspace_catalog_loads_local_plugin_blocks():
    examples = Path(__file__).parent.parent / "examples"
    blocks = {block["name"] for block in FlowWorkspace(examples).catalog()}
    assert "convert_currency" in blocks


def test_browser_can_run_unsaved_flow_tests(tmp_path: Path):
    with DashboardServer(tmp_path / "runs.db", port=0, flows=tmp_path) as server:
        flow = {
            "steps": {
                "note": {"block": "log", "settings": {"message": "Count {{ source.count }}"}},
                "source": {"block": "read_json", "settings": {"path": "missing.json"}},
            },
            "tests": [
                {
                    "name": "sample",
                    "inputs": {"source": [1, 2]},
                    "expect": {"note": {"level": "info", "message": "Count 2"}},
                }
            ],
        }
        status, result = request(
            server, "POST", "/api/flows/test", {"name": "unsaved", "flow": flow}
        )
        assert status == 200
        assert result == [{"name": "sample", "passed": True, "problems": []}]
        assert not (tmp_path / "unsaved.json").exists()
