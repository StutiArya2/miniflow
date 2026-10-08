"""The command line with JSON (no-code) flows."""

from __future__ import annotations

import json
import sys

import pytest

from miniflow import State, StateStore
from miniflow.cli import main


@pytest.fixture
def flows(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    (tmp_path / "sales.csv").write_text("name,price\npen,120\ncup,40\n", encoding="utf-8")
    flow = {
        "params": {"min": {"type": "int", "default": 100}},
        "steps": {
            "sales": {"block": "read_csv", "settings": {"path": "sales.csv"}},
            "big": {
                "block": "filter",
                "settings": {"rows": "{{ sales }}", "where": "price > {{ params.min }}"},
            },
            "save": {"block": "write_json", "settings": {"data": "{{ big }}", "path": "big.json"}},
        },
    }
    (tmp_path / "report.json").write_text(json.dumps(flow), encoding="utf-8")
    failing = {
        "steps": {"read": {"block": "read_csv", "settings": {"path": "missing.csv"}}},
        "on_failure": {
            "block": "write_json",
            "settings": {"path": "alert.json", "data": "{{ failure }}"},
        },
    }
    (tmp_path / "failing.json").write_text(json.dumps(failing), encoding="utf-8")
    return tmp_path


def test_run_a_json_flow(flows, capsys):
    db = flows / "s.db"
    assert main(["run", str(flows / "report.json"), "--db", str(db), "--plain"]) == 0
    assert json.loads((flows / "big.json").read_text(encoding="utf-8")) == [
        {"name": "pen", "price": 120}
    ]
    assert "SUCCESS" in capsys.readouterr().out
    with StateStore(db) as store:
        run_id = store.latest_run("report")
        assert set(store.get_states(run_id).values()) == {State.SUCCESS}


def test_params_on_the_command_line(flows):
    args = [
        "run",
        str(flows / "report.json"),
        "--db",
        str(flows / "s.db"),
        "-q",
        "--param",
        "min=10",
    ]
    assert main(args) == 0
    assert len(json.loads((flows / "big.json").read_text(encoding="utf-8"))) == 2


def test_resume_a_json_flow(flows):
    db = str(flows / "s.db")
    assert main(["run", str(flows / "failing.json"), "--db", db, "-q"]) == 1
    (flows / "missing.csv").write_text("a\n1\n", encoding="utf-8")
    assert main(["resume", str(flows / "failing.json"), "--db", db, "-q"]) == 0


def test_on_failure_runs_after_a_failed_run(flows, capsys):
    assert main(["run", str(flows / "failing.json"), "--db", str(flows / "s.db"), "-q"]) == 1
    alert = json.loads((flows / "alert.json").read_text(encoding="utf-8"))
    assert alert["step"] == "read"
    assert alert["failed"] == ["read"]
    assert "on_failure: ran write_json" in capsys.readouterr().err


def test_problems_are_printed_and_exit_2(flows, capsys):
    (flows / "bad.json").write_text('{"steps": {"a": {"block": "nope"}}}', encoding="utf-8")
    assert main(["validate", str(flows / "bad.json")]) == 2
    err = capsys.readouterr().err
    assert "bad.json has 1 problem" in err
    assert "there is no block called 'nope'" in err


def test_validate_and_graph(flows, capsys):
    assert main(["validate", str(flows / "report.json")]) == 0
    assert capsys.readouterr().out.startswith("OK: 3 steps, order: sales -> big -> save")
    assert main(["graph", str(flows / "report.json"), "--db", ""]) == 0
    assert '"sales" -> "big"' in capsys.readouterr().out


def test_unknown_param_exits_2(flows, capsys):
    assert main(["validate", str(flows / "report.json"), "--param", "max=3"]) == 2
    assert "no parameter called 'max'" in capsys.readouterr().err
    assert main(["validate", str(flows / "report.json"), "--param", "oops"]) == 2
    assert "NAME=VALUE" in capsys.readouterr().err


def test_blocks_command_lists_and_describes(capsys):
    assert main(["blocks"]) == 0
    out = capsys.readouterr().out
    assert "read_csv" in out
    assert "Read a CSV file into a list of rows." in out
    assert main(["blocks", "filter"]) == 0
    detail = capsys.readouterr().out
    assert "filter (cpu)" in detail
    assert "where" in detail
    assert "required" in detail
    assert main(["blocks", "filtr"]) == 2
    assert "Did you mean 'filter'" in capsys.readouterr().err


def test_blocks_command_loads_plugins(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    (tmp_path / "hello_plugin.py").write_text(
        "from miniflow.blocks import block\n\n"
        "@block('hello_cli_test')\n"
        "def hello():\n"
        '    """Say hello."""\n'
        "    return 'hi'\n",
        encoding="utf-8",
    )
    assert main(["blocks", "--blocks", str(tmp_path)]) == 0
    assert "hello_cli_test" in capsys.readouterr().out
