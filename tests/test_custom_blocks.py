"""Global custom blocks validate, run, and retain immutable revisions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from miniflow import Scheduler, StateStore
from miniflow.custom_blocks import CustomBlockError, CustomBlockLibrary
from miniflow.flowfile import FlowFileError, load_json_flow
from miniflow.ui import DashboardServer


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CustomBlockLibrary:
    monkeypatch.setenv("MINIFLOW_CUSTOM_BLOCKS_DIR", str(tmp_path / "global"))
    return CustomBlockLibrary()


def _run(path: Path, document: dict) -> object:
    flow = load_json_flow(path, _source=document)
    with StateStore() as store:
        return Scheduler(flow.dag, store=store, flow=flow.name).run()


def test_visual_block_is_global_and_exposes_internal_steps(
    library: CustomBlockLibrary, tmp_path: Path
) -> None:
    saved = library.save(
        {
            "name": "keep_large",
            "kind": "visual",
            "description": "Keep rows over a threshold",
            "inputs": {"rows": {"type": "list"}, "minimum": {"type": "int", "default": 100}},
            "steps": {
                "filter": {
                    "block": "filter",
                    "settings": {
                        "rows": "{{ params.rows }}",
                        "where": "amount > {{ params.minimum }}",
                    },
                }
            },
            "outputs": {"rows": "{{ filter }}"},
        }
    )
    assert saved["revision"]
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    assert "keep_large" in {b["name"] for b in library.catalog()}
    document = {
        "steps": {
            "clean": {
                "custom_block": "keep_large",
                "settings": {
                    "rows": [{"amount": 150}, {"amount": 40}],
                },
            }
        }
    }
    for folder in (first, second):
        result = _run(folder / "flow.json", document)
        assert result.status == "SUCCESS", result.errors
        assert result["clean"] == {"rows": [{"amount": 150}]}
        assert "clean.filter" in result.states


def test_python_edits_affect_next_run_only(library: CustomBlockLibrary, tmp_path: Path) -> None:
    definition = {
        "name": "uppercase",
        "kind": "python",
        "description": "Uppercase text",
        "inputs": {"text": {"type": "str"}},
        "outputs": ["result"],
        "code": "return {'result': text.upper()}",
        "execution": "cpu",
    }
    first = library.save(definition)
    document = {"steps": {"change": {"custom_block": "uppercase", "settings": {"text": "hello"}}}}
    old_flow = load_json_flow(tmp_path / "old.json", _source=document)
    second = library.save(
        {**definition, "code": "return {'result': text.title()}"}, first["revision"]
    )
    assert first["revision"] != second["revision"]
    with StateStore() as store:
        old = Scheduler(old_flow.dag, store=store, flow="old").run()
    assert old.status == "SUCCESS", old.errors
    assert old["change"] == {"result": "HELLO"}
    new = _run(tmp_path / "new.json", document)
    assert new.status == "SUCCESS", new.errors
    assert new["change"] == {"result": "Hello"}


def test_invalid_code_and_conflicting_edit(library: CustomBlockLibrary) -> None:
    definition = {
        "name": "edit_me",
        "kind": "python",
        "inputs": {},
        "outputs": ["result"],
        "code": "return {'result': 1}",
    }
    with pytest.raises(CustomBlockError, match="syntax"):
        library.validate({**definition, "code": "return ("})
    first = library.save(definition)
    with pytest.raises(CustomBlockError, match="changed"):
        library.save({**definition, "code": "return {'result': 2}"})
    assert library.get("edit_me")["revision"] == first["revision"]


def test_custom_input_errors_are_reported_before_running(
    library: CustomBlockLibrary, tmp_path: Path
) -> None:
    library.save(
        {
            "name": "word_count",
            "kind": "python",
            "inputs": {"text": {"type": "str"}},
            "outputs": ["count"],
            "code": "return {'count': len(text.split())}",
        }
    )
    with pytest.raises(FlowFileError, match="setting 'text': should be text"):
        load_json_flow(
            tmp_path / "bad.json",
            _source={
                "steps": {"count": {"custom_block": "word_count", "settings": {"text": 42}}},
            },
        )
    with pytest.raises(FlowFileError, match="unknown setting 'extra'"):
        load_json_flow(
            tmp_path / "extra.json",
            _source={
                "steps": {
                    "count": {
                        "custom_block": "word_count",
                        "settings": {"text": "hello", "extra": 1},
                    }
                }
            },
        )


def test_custom_block_api(library: CustomBlockLibrary, tmp_path: Path) -> None:
    workspace = tmp_path / "flows"
    workspace.mkdir()
    definition = {
        "name": "greeting",
        "kind": "python",
        "inputs": {"name": {"type": "str"}},
        "outputs": ["result"],
        "code": "return {'result': 'Hello ' + name}",
    }
    with DashboardServer(tmp_path / "runs.db", port=0, flows=workspace) as server:
        import http.client
        from urllib.parse import urlsplit

        def request(method: str, endpoint: str, body: dict | None = None) -> tuple[int, object]:
            url = urlsplit(server.url)
            conn = http.client.HTTPConnection(url.hostname, url.port)
            data = json.dumps(body).encode() if body is not None else None
            conn.request(
                method, endpoint, data, {"Content-Type": "application/json"} if data else {}
            )
            response = conn.getresponse()
            result = response.status, json.loads(response.read())
            conn.close()
            return result

        assert request("POST", "/api/custom-blocks/validate", {"definition": definition})[0] == 200
        assert request("POST", "/api/custom-blocks/save", {"definition": definition})[0] == 201
        assert request("GET", "/api/custom-blocks")[1][0]["name"] == "greeting"
        assert request("GET", "/api/custom-blocks/greeting")[1]["code"] == definition["code"]
        assert (
            request(
                "POST",
                "/api/custom-blocks/test",
                {
                    "name": "greeting",
                    "settings": {"name": "Ada"},
                    "expected": {"result": "Hello Ada"},
                },
            )[1][0]["passed"]
            is True
        )
