"""No-code editor additions: palette groups, step run options, finish actions and outputs."""

from __future__ import annotations

import http.client
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from miniflow.ui import DashboardServer
from miniflow.ui.author import FlowWorkspace, block_catalog, block_group
from tests.test_author import request

GROUPS = {
    "Read & write", "Transform", "Text & dates", "Excel", "Messages, AI & cloud",
    "Email & utilities", "Databases", "System",
}  # fmt: skip
# Other tests register plugin blocks in the same process; those are grouped as "Plugins".
KNOWN = GROUPS | {"Plugins"}


def test_every_builtin_block_has_a_palette_group():
    catalog = block_catalog()
    assert catalog
    assert {block["group"] for block in catalog} <= KNOWN
    by_name = {block["name"]: block["group"] for block in catalog}
    assert by_name["read_csv"] == "Read & write"
    assert by_name["slack_message"] == "Messages, AI & cloud"
    assert by_name["email"] == "Email & utilities"


@pytest.mark.parametrize(
    ("ref", "group"),
    [
        ("miniflow.blocks.builtin.web:slack_message", "Messages, AI & cloud"),
        ("miniflow.blocks.builtin.newmodule:thing", "Other"),
        ("my_blocks:shout", "Plugins"),
        ("miniflow.blocks.builtinish:x", "Plugins"),
    ],
)
def test_block_group(ref: str, group: str):
    assert block_group(ref) == group


# What the editor writes for run options, finish actions and outputs.
FLOW = {
    "steps": {
        "numbers": {
            "block": "read_json",
            "settings": {"path": "numbers.json"},
            "retries": 2,
            "timeout": 30,
            "kind": "cpu",
        },
        "report": {
            "block": "log",
            "settings": {"message": "{{ numbers }}"},
            "after": ["numbers"],
        },
    },
    "on_success": {"block": "log", "settings": {"message": "done: {{ report }}"}},
    "on_failure": {
        "block": "log",
        "settings": {"message": "{{ failure.step }}: {{ failure.error }}"},
    },
    "outputs": {"total": "{{ numbers }}"},
}


def test_editor_written_options_validate_and_round_trip(tmp_path: Path):
    ws = FlowWorkspace(tmp_path)
    assert ws.validate("demo", FLOW)["valid"]
    ws.save("demo", FLOW)
    assert ws.read("demo") == FLOW


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"retries": -1}, "retries"),
        ({"timeout": 0}, "timeout"),
        ({"kind": "async"}, "kind"),
    ],
)
def test_bad_run_options_are_reported_on_the_step(tmp_path: Path, change: dict, problem: str):
    flow = {"steps": {"numbers": {"block": "read_json", "settings": {"path": "x.json"}, **change}}}
    result = FlowWorkspace(tmp_path).validate("demo", flow)
    assert not result["valid"]
    # the editor links a problem to its step by the "step 'name'" text
    assert any("step 'numbers'" in p and problem in p for p in result["problems"])


def test_the_failure_action_cannot_use_step_results(tmp_path: Path):
    """Why the editor offers only failure values there, not steps."""
    flow = {**FLOW, "on_failure": {"block": "log", "settings": {"message": "{{ numbers }}"}}}
    assert not FlowWorkspace(tmp_path).validate("demo", flow)["valid"]


def test_catalog_over_http_has_groups(tmp_path: Path):
    with DashboardServer(tmp_path / "runs.db", port=0, flows=tmp_path) as server:
        status, catalog = request(server, "GET", "/api/blocks")
    assert status == 200
    assert all(block["group"] in KNOWN for block in catalog)


def _get(server: DashboardServer, path: str) -> str:
    url = urlsplit(server.url)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        assert response.status == 200, path
        return response.read().decode()
    finally:
        conn.close()


def test_editor_serves_the_no_code_additions(tmp_path: Path):
    with DashboardServer(tmp_path / "runs.db", port=0, flows=tmp_path) as server:
        page = _get(server, "/")
        editor = _get(server, "/static/editor.js")
        history = _get(server, "/static/editor-history.js")
        view = _get(server, "/static/editor-view.js")
    for element in ("studio-finish-panel", "studio-finish-editor", "studio-problems"):
        assert f'id="{element}"' in page
    for function in (
        "function studioSettingsForm",
        "function studioRunOptions",
        "function studioRenderFinish",
        "function studioQuickAdd",
        "function studioRemoveWire",
        "function studioRenderProblems",
    ):
        assert function in editor
    assert "function studioRenameStep" in history
    assert "function studioFocusStep" in view
    # an older dashboard serving newer files says what is missing
    assert '"studioRenameStep", "studioFocusStep"]' in editor
