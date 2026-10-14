"""The agent tool layer: reading, writing and validating flows, block drafts."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from miniflow import DAG, StateStore, task
from miniflow.agent.tools import TOOLS, Toolbox, ToolError, UnknownTool, available, call
from tests import _sched_flows as F

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
        assert tool.schema["type"] == "object"
        assert tool.description
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
    assert only
    assert {b["group"] for b in only} == {"Transform"}
    log = call(box, "describe_block", {"name": "log"})
    assert log["example_step"]["block"] == "log"
    assert "message" in log["example_step"]["settings"]
    with pytest.raises(ToolError, match="no block called"):
        call(box, "describe_block", {"name": "lgo"})


def test_get_and_list_flows(box):
    flow = call(box, "get_flow", {"name": "hello"})
    assert flow["document"] == HELLO
    assert flow["version"] == box.workspace.file_version("hello")
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
    assert result["valid"] is False
    assert any("lgo" in p for p in result["problems"])


def test_save_new_then_update_with_its_version(box):
    created = call(box, "save_flow", {"name": "bye", "document": BYE, "message": "first"})
    assert created["saved"]
    assert created["version"] == box.workspace.file_version("bye")
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
    assert restored["document"] == HELLO
    assert box.workspace.read("hello") == HELLO


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
    return (
        "from miniflow.blocks import Setting, block\n\n"
        f'@block("{name}", settings=[Setting("text", str)])\n'
        'def run(text):\n    """Echo."""\n    return text\n'
    )


def test_draft_tools(box):
    name = "blk_" + uuid.uuid4().hex[:10]
    result = call(box, "draft_block", {"name": name, "source": good_block(name)})
    assert result["drafted"]
    assert "Pending blocks" in result["next"]
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


def test_read_only_hides_sample_tests(flows, tmp_path):
    box = Toolbox(flows, tmp_path / "s.db", read_only=True)
    with pytest.raises(UnknownTool):
        call(box, "test_flow", {"name": "hello"})


def test_template_creation_is_labelled_agent(box):
    created = call(box, "create_flow_from_template", {"template": "hello_world", "name": "starter"})
    box.workspace.save("starter", BYE)
    assert box.workspace.versions("starter")[0]["message"].endswith("(agent)")
    assert created["version"]


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
    assert result["state"] == "started"
    assert result["run_id"]
    assert result["database"] == str(db.resolve())
    assert result["dashboard"].startswith("http://")
    run = call(box, "get_run", {"run_id": result["run_id"]})
    assert run["status"] == "SUCCESS"
    assert run["finished"]
    assert run["steps"][0]["name"] == "a"
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


def test_save_returns_its_own_version_when_another_writer_saves_next(box, monkeypatch):
    real_save = box.workspace.save

    def interleave(name, document, *args, **kwargs):
        result = real_save(name, document, *args, **kwargs)
        real_save(name, HELLO)
        return result

    monkeypatch.setattr(box.workspace, "save", interleave)
    saved = call(box, "save_flow", {"name": "interleaved", "document": BYE, "message": "first"})
    assert saved["version"] != box.workspace.file_version("interleaved")
    with pytest.raises(ToolError, match="changed since"):
        call(
            box,
            "save_flow",
            {
                "name": "interleaved",
                "document": BYE,
                "message": "retry",
                "base_version": saved["version"],
            },
        )


def test_run_tool_refuses_python_flows(box):
    (box.folder / "python_only.py").write_text('raise RuntimeError("must not run")\n')
    with pytest.raises(ToolError, match="no flow"):
        call(box, "run_flow", {"name": "python_only"})
    assert box.runs.calls == []


def test_invalid_json_flows_can_be_repaired_with_their_version(box):
    (box.folder / "broken.json").write_text("{not json")
    broken = next(f for f in call(box, "list_flows", {})["flows"] if f["name"] == "broken")
    assert broken["valid"] is False
    assert broken["version"] == box.workspace.file_version("broken")
    with pytest.raises(ToolError, match="already exists") as info:
        call(box, "save_flow", {"name": "broken", "document": BYE, "message": "repair"})
    assert info.value.data["current_version"] == broken["version"]
    result = call(
        box,
        "save_flow",
        {"name": "broken", "document": BYE, "message": "repair", "base_version": broken["version"]},
    )
    assert result["saved"]
    assert box.workspace.read("broken") == BYE
