"""Sample-input flow tests execute only the branch being asserted."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from miniflow.cli import main
from miniflow.flowfile import FlowFileError, load_json_flow
from miniflow.flowtest import run_flow_tests


def example(tmp_path: Path) -> Path:
    path = tmp_path / "checked.json"
    path.write_text(
        json.dumps(
            {
                "params": {"label": {"type": "str", "default": "rows"}},
                "steps": {
                    "source": {"block": "read_json", "settings": {"path": "missing.json"}},
                    "notice": {
                        "block": "log",
                        "settings": {"message": "{{ source.count }} {{ params.label }}"},
                    },
                    "write": {
                        "block": "write_json",
                        "settings": {"data": "{{ source }}", "path": "unexpected.json"},
                    },
                },
                "tests": [
                    {
                        "name": "two rows",
                        "inputs": {"source": [1, 2]},
                        "expect": {"notice": {"level": "info", "message": "2 rows"}},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_sample_input_replaces_step_and_does_not_run_other_branches(tmp_path: Path, capsys):
    path = example(tmp_path)
    assert run_flow_tests(path) == [{"name": "two rows", "passed": True, "problems": []}]
    assert not (tmp_path / "unexpected.json").exists()
    assert main(["test", str(path)]) == 0
    assert "1/1 flow tests passed" in capsys.readouterr().out


def test_wrong_expected_result_fails_without_echoing_values(tmp_path: Path, capsys):
    path = example(tmp_path)
    spec = json.loads(path.read_text(encoding="utf-8"))
    spec["tests"][0]["expect"]["notice"]["message"] = "wrong"
    path.write_text(json.dumps(spec), encoding="utf-8")
    assert main(["test", str(path)]) == 1
    output = capsys.readouterr().out
    assert "FAIL two rows" in output
    assert "did not match" in output
    assert "[1, 2]" not in output


def test_invalid_target_is_rejected_before_execution(tmp_path: Path):
    path = example(tmp_path)
    spec = json.loads(path.read_text(encoding="utf-8"))
    spec["tests"][0]["expect"] = {"not_a_step": None}
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(FlowFileError, match="unknown 'not_a_step'"):
        load_json_flow(path)
