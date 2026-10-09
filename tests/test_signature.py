"""Reuse identities should change with inputs but ignore unrelated parameters."""

import json

from miniflow.flowfile import load_json_flow
from miniflow.signature import step_signature


def test_environment_reference_changes_signature(tmp_path, monkeypatch):
    path = tmp_path / "env.json"
    path.write_text(
        json.dumps(
            {
                "steps": {
                    "say": {
                        "block": "log",
                        "settings": {"message": "{{ env.MINIFLOW_TEST_VALUE }}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MINIFLOW_TEST_VALUE", "first")
    first = step_signature(load_json_flow(path).dag.tasks["say"])
    monkeypatch.setenv("MINIFLOW_TEST_VALUE", "second")
    second = step_signature(load_json_flow(path).dag.tasks["say"])
    assert first != second


def test_dynamic_file_path_disables_reuse(tmp_path):
    path = tmp_path / "file.json"
    path.write_text(
        json.dumps(
            {
                "params": {"file": {"type": "str", "default": "data.csv"}},
                "steps": {"read": {"block": "read_csv", "settings": {"path": "{{ params.file }}"}}},
            }
        ),
        encoding="utf-8",
    )
    assert step_signature(load_json_flow(path).dag.tasks["read"]) is None
