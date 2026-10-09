"""Sub-flows: a JSON flow used as one step of another (milestone 2)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from miniflow import Scheduler, StateStore
from miniflow.flowfile import FlowFileError, load_json_flow

CLEAN = {
    "description": "Keep rows over a minimum and sort them",
    "params": {"rows": {"type": "list"}, "min": {"type": "float", "default": 0}},
    "steps": {
        "big": {
            "block": "filter",
            "settings": {"rows": "{{ params.rows }}", "where": "amount > {{ params.min }}"},
        },
        "ranked": {
            "block": "sort",
            "settings": {"rows": "{{ big }}", "by": "amount", "descending": True},
        },
    },
    "outputs": {"rows": "{{ ranked }}", "count": "{{ big.count }}"},
}


def write(path: Path, flow: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(flow), encoding="utf-8")
    return path


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    (tmp_path / "sales.csv").write_text("id,amount\n1,30\n2,80\n3,60\n", encoding="utf-8")
    write(tmp_path / "parts" / "clean.json", CLEAN)
    return tmp_path


def parent(**clean_step) -> dict:
    return {
        "steps": {
            "sales": {"block": "read_csv", "settings": {"path": "sales.csv"}},
            "clean": {
                "flow": "parts/clean.json",
                "settings": {"rows": "{{ sales }}", "min": 50},
                **clean_step,
            },
            "save": {
                "block": "write_json",
                "settings": {"data": "{{ clean.rows }}", "path": "out.json"},
            },
            "note": {"block": "log", "settings": {"message": "{{ clean.count }} kept"}},
        }
    }


def run(flow, tmp_path):
    with StateStore(tmp_path / "s.db") as store:
        return Scheduler(flow.dag, store=store, flow=flow.name).run()


def test_sub_flow_runs_inside_the_parent(folder, tmp_path):
    flow = load_json_flow(write(folder / "main.json", parent()))
    result = run(flow, tmp_path)
    assert result.status == "SUCCESS", result.errors
    assert result["clean"] == {
        "rows": [{"id": 2, "amount": 80}, {"id": 3, "amount": 60}],
        "count": 2,
    }
    assert result["note"]["message"] == "2 kept"
    assert json.loads((folder / "out.json").read_text(encoding="utf-8"))[0]["id"] == 2
    assert {"clean.params", "clean.big", "clean.ranked", "clean"} <= set(result.states)


def test_sub_flow_wiring(folder):
    up = load_json_flow(write(folder / "main.json", parent(after=["sales"]))).dag.upstream
    assert up["clean.params"] == ("sales",)
    assert up["clean.big"] == ("clean.params",)
    assert set(up["clean.ranked"]) == {"clean.big", "clean.params"}
    assert set(up["clean"]) == {"clean.ranked", "clean.big"}
    assert up["save"] == ("clean",)


def test_sub_flow_without_outputs_returns_every_step(folder, tmp_path):
    write(folder / "parts" / "clean.json", {k: v for k, v in CLEAN.items() if k != "outputs"})
    result = run(
        load_json_flow(
            write(
                folder / "main.json",
                {
                    "steps": {
                        "sales": {"block": "read_csv", "settings": {"path": "sales.csv"}},
                        "clean": {"flow": "parts/clean.json", "settings": {"rows": "{{ sales }}"}},
                    }
                },
            )
        ),
        tmp_path,
    )
    assert set(result["clean"]) == {"big", "ranked"}
    assert len(result["clean"]["ranked"]) == 3


def test_sub_flow_paths_are_relative_to_the_sub_flow(folder, tmp_path):
    (folder / "parts" / "local.csv").write_text("x\n1\n2\n", encoding="utf-8")
    write(
        folder / "parts" / "reader.json",
        {
            "steps": {"r": {"block": "read_csv", "settings": {"path": "local.csv"}}},
            "outputs": {"n": "{{ r.count }}"},
        },
    )
    flow = load_json_flow(
        write(folder / "main.json", {"steps": {"read": {"flow": "parts/reader.json"}}})
    )
    assert run(flow, tmp_path)["read"] == {"n": 2}


def test_nested_sub_flows(folder, tmp_path):
    write(
        folder / "parts" / "outer.json",
        {
            "params": {"rows": {"type": "list"}},
            "steps": {
                "inner": {
                    "flow": "clean.json",
                    "settings": {"rows": "{{ params.rows }}", "min": 70},
                }
            },
            "outputs": {"top": "{{ inner.rows.first.id }}"},
        },
    )
    flow = load_json_flow(
        write(
            folder / "main.json",
            {
                "steps": {
                    "sales": {"block": "read_csv", "settings": {"path": "sales.csv"}},
                    "o": {"flow": "parts/outer.json", "settings": {"rows": "{{ sales }}"}},
                }
            },
        )
    )
    result = run(flow, tmp_path)
    assert result["o"] == {"top": 2}
    assert "o.inner.big" in result.states


def problems(folder, spec) -> str:
    with pytest.raises(FlowFileError) as err:
        load_json_flow(write(folder / "main.json", spec))
    return str(err.value)


def test_sub_flow_problems(folder):
    text = problems(
        folder,
        {
            "steps": {
                "sales": {"block": "read_csv", "settings": {"path": "sales.csv"}},
                "a": {"flow": "parts/clean.json", "settings": {"rowz": "{{ sales }}"}},
                "b": {"flow": "parts/nope.json"},
                "c": {"flow": "parts/clean.json", "block": "log", "settings": {"rows": []}},
                "d": {"flow": "parts/clean.json", "settings": {"rows": [], "min": "lots"}},
            }
        },
    )
    for expected in [
        "step 'a': unknown setting 'rowz'. Did you mean 'rows'?",
        "step 'a': missing required setting 'rows'",
        "step 'b': there is no flow file",
        "step 'c': use either 'block' or 'flow', not both",
        "step 'd', setting 'min': should be a number, not a piece of text",
    ]:
        assert expected in text, (expected, text)


def test_problems_inside_a_sub_flow_say_where(folder):
    bad = dict(CLEAN, steps={**CLEAN["steps"], "oops": {"block": "lgo", "settings": {}}})
    write(folder / "parts" / "clean.json", bad)
    text = problems(folder, parent())
    assert "step 'clean' (parts/clean.json): step 'oops': there is no block called 'lgo'" in text


def test_a_flow_cannot_include_itself(folder):
    write(folder / "parts" / "loop.json", {"steps": {"again": {"flow": "loop.json"}}})
    assert "includes itself" in problems(folder, {"steps": {"l": {"flow": "parts/loop.json"}}})


def test_outputs_are_checked(folder):
    write(folder / "parts" / "clean.json", dict(CLEAN, outputs={"rows": "{{ rankd }}"}))
    assert "Did you mean 'ranked'" in problems(folder, parent())


def test_dynamic_sub_flow_parameter_is_checked_when_it_runs(folder, tmp_path):
    spec = parent()
    spec["steps"]["clean"]["settings"]["min"] = "{{ sales.first }}"
    flow = load_json_flow(write(folder / "main.json", spec))
    result = run(flow, tmp_path)
    assert result.status == "FAILED"
    assert "setting 'min': should be a number" in result.errors["clean.params"]
