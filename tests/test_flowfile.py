"""JSON flows: loading, checking, and running them through the scheduler."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from miniflow import Scheduler, State, StateStore
from miniflow.blocks import Setting, block
from miniflow.cli import main
from miniflow.flowfile import FlowFileError, load_json_flow


@block("leaky_test", settings=[Setting("token", str, secret=True)])
def leaky(token):
    """A block whose error message contains its secret."""
    raise PermissionError(f"token {token} was rejected")


SALES = "name,region,price\npen,North,120\ncup,South,40\nink,North,300\n"


def write(folder: Path, name: str, flow: dict) -> Path:
    path = folder / f"{name}.json"
    path.write_text(json.dumps(flow, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    (tmp_path / "sales.csv").write_text(SALES, encoding="utf-8")
    return tmp_path


def run(flow, tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        result = Scheduler(flow.dag, store=store, flow=flow.name).run()
    return result


REPORT = {
    "description": "North sales over a minimum price",
    "params": {"min": {"type": "float", "default": 100}},
    "steps": {
        "sales": {"block": "read_csv", "settings": {"path": "sales.csv"}},
        "big": {
            "block": "filter",
            "settings": {"rows": "{{ sales }}", "where": "price > {{ params.min }}"},
        },
        "save": {"block": "write_csv", "settings": {"rows": "{{ big }}", "path": "out/big.csv"}},
        "note": {
            "block": "log",
            "settings": {"message": "{{ big.count }} big sales, first: {{ big.first.name }}"},
        },
        "pause": {"block": "wait", "settings": {"seconds": 0.01}, "after": ["save"]},
    },
}


# ------------------------------------------------------------------ running
def test_runs_a_flow_end_to_end(folder, tmp_path):
    flow = load_json_flow(write(folder, "report", REPORT))
    assert flow.name == "report"
    assert flow.description == "North sales over a minimum price"
    result = run(flow, tmp_path)
    assert result.status == "SUCCESS", result.errors
    assert (folder / "out" / "big.csv").read_text(encoding="utf-8").splitlines() == [
        "name,region,price",
        "pen,North,120",
        "ink,North,300",
    ]
    assert result["note"] == {"level": "info", "message": "2 big sales, first: pen"}
    assert result["save"] == {"path": str(folder / "out" / "big.csv"), "rows": 2}


def test_dependencies_come_from_placeholders_and_after(folder):
    dag = load_json_flow(write(folder, "report", REPORT)).dag
    assert dag.upstream["big"] == ("sales",)
    assert set(dag.upstream["note"]) == {"big"}
    assert dag.upstream["pause"] == ("save",)
    assert dag.tasks["big"].kind == "cpu"  # filter's default
    assert dag.tasks["pause"].kind == "async"


def test_params_from_the_command_line(folder, tmp_path):
    flow = load_json_flow(write(folder, "report", REPORT), params={"min": "200"})
    assert flow.params == {"min": 200.0}
    assert run(flow, tmp_path)["save"]["rows"] == 1


def test_invalid_parameter_name_is_rejected(folder):
    bad = dict(REPORT, params={"has space": {"type": "int", "default": 1}})
    with pytest.raises(FlowFileError, match="name should start with a letter"):
        load_json_flow(write(folder, "bad_param", bad))


def test_environment_files(folder, tmp_path, monkeypatch):
    monkeypatch.delenv("SHOP_TOKEN", raising=False)
    env = folder / "environments"
    env.mkdir()
    (env / "prod.json").write_text('{"min": 250}', encoding="utf-8")
    (env / "prod.env").write_text("# secrets\nSHOP_TOKEN=abc123\n", encoding="utf-8")
    spec = dict(
        REPORT,
        steps={
            **REPORT["steps"],
            "tok": {"block": "log", "settings": {"message": "{{ env.SHOP_TOKEN }}"}},
        },
    )
    flow = load_json_flow(write(folder, "report", spec), environment="prod")
    assert flow.params == {"min": 250}
    result = run(flow, tmp_path)
    assert result["save"]["rows"] == 1
    assert result["tok"]["message"] == "abc123"


def test_step_settings_can_override_kind_retries_and_timeout(folder):
    spec = {
        "steps": {
            "s": {
                "block": "filter",
                "kind": "io",
                "retries": 2,
                "timeout": 5,
                "settings": {"rows": [], "where": "x > 1"},
            }
        }
    }
    t = load_json_flow(write(folder, "f", spec)).dag.tasks["s"]
    assert (t.kind, t.retries, t.timeout) == ("io", 2, 5)


def test_plugin_blocks_next_to_the_flow_run_in_processes(folder, tmp_path):
    blocks = folder / "blocks"
    blocks.mkdir()
    (blocks / "shout_plugin.py").write_text(
        dedent(
            """
            from miniflow.blocks import Setting, block

            @block("shout_test", settings=[Setting("text", str)], kind="cpu")
            def shout(text):
                \"\"\"Upper-case some text.\"\"\"
                return text.upper() + "!"
            """
        ),
        encoding="utf-8",
    )
    spec = {"steps": {"s": {"block": "shout_test", "settings": {"text": "hi"}}}}
    result = run(load_json_flow(write(folder, "plug", spec)), tmp_path)
    assert result.status == "SUCCESS", result.errors
    assert result["s"] == "HI!"


def test_runtime_errors_name_the_problem_and_hide_secrets(folder, tmp_path, monkeypatch):
    monkeypatch.setenv("SMTP_PASSWORD", "hunter2")
    spec = {
        "steps": {
            "sales": {"block": "read_csv", "settings": {"path": "sales.csv"}},
            "oops": {"block": "log", "settings": {"message": "{{ sales.0.colour }}"}},
            "mail": {
                "block": "email",
                "settings": {
                    "to": "a@b.c",
                    "subject": "x",
                    "host": "127.0.0.1",
                    "port": 9,
                    "username": "u hunter2",
                    "password": "{{ env.SMTP_PASSWORD }}",
                },
            },
        }
    }
    result = run(load_json_flow(write(folder, "bad", spec)), tmp_path)
    assert result.states["oops"] is State.FAILED
    assert (
        "setting 'message': no 'colour' in sales.0 (it has: name, region, price)"
        in result.errors["oops"]
    )
    assert result.states["mail"] is State.FAILED
    assert "hunter2" not in result.errors["mail"]


def test_secrets_are_masked_in_errors(folder, tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "tok-9f8e7d")
    spec = {
        "steps": {"call": {"block": "leaky_test", "settings": {"token": "{{ env.API_TOKEN }}"}}}
    }
    result = run(load_json_flow(write(folder, "leak", spec)), tmp_path)
    assert "PermissionError: token •••• was rejected" in result.errors["call"]
    assert "tok-9f8e7d" not in result.errors["call"]


def test_on_failure_gets_the_failure(folder, tmp_path):
    spec = {
        "steps": {"missing": {"block": "read_csv", "settings": {"path": "nope.csv"}}},
        "on_failure": {
            "block": "write_json",
            "settings": {
                "path": "failure.json",
                "data": {"step": "{{ failure.step }}", "error": "{{ failure.error }}"},
            },
        },
    }
    flow = load_json_flow(write(folder, "f", spec))
    result = run(flow, tmp_path)
    assert result.status == "FAILED"
    flow.run_on_failure(result)
    saved = json.loads((folder / "failure.json").read_text(encoding="utf-8"))
    assert saved["step"] == "missing"
    assert "no such file" in saved["error"]


def test_on_success_runs_after_a_successful_flow(folder, tmp_path, capsys):
    spec = {
        "steps": {"say": {"block": "log", "settings": {"message": "done"}}},
        "on_success": {
            "block": "write_json",
            "settings": {"path": "success.json", "data": {"message": "{{ say.message }}"}},
        },
    }
    path = write(folder, "success", spec)
    assert main(["run", str(path), "--db", str(tmp_path / "success.db"), "--quiet"]) == 0
    assert json.loads((folder / "success.json").read_text(encoding="utf-8")) == {"message": "done"}
    assert "on_success: ran write_json" in capsys.readouterr().err


# ----------------------------------------------------------------- checking
def problems(folder, spec, **kwargs) -> str:
    with pytest.raises(FlowFileError) as err:
        load_json_flow(write(folder, "f", spec), **kwargs)
    return str(err.value)


def test_all_problems_are_reported_together(folder, monkeypatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    spec = {
        "steps": {
            "sales": {"block": "read_cvs", "settings": {"path": "sales.csv"}},
            "big": {"block": "filter", "settings": {"rows": "{{ sale }}", "were": "x"}},
            "save": {"block": "write_csv", "settings": {"rows": "{{ big }}", "path": 5}},
            "mail": {
                "block": "email",
                "settings": {"to": "a", "subject": "s", "password": "plain"},
            },
            "tok": {
                "block": "log",
                "settings": {"message": "{{ env.NOT_SET_ANYWHERE }} {{ params.nope }}"},
            },
            "params": {"block": "log", "settings": {"message": "x"}},
        },
        "colour": "blue",
    }
    text = problems(folder, spec)
    for expected in [
        "f.json has 10 problems",
        "unknown top-level field 'colour'",
        "step 'sales': there is no block called 'read_cvs'. Did you mean 'read_csv'?",
        "step 'big', setting 'rows': {{ sale }} refers to a step called 'sale',"
        " which doesn't exist. Did you mean 'sales'?",
        "step 'big': unknown setting 'were'. Did you mean 'where'?",
        "step 'big': missing required setting 'where'",
        "step 'save', setting 'path': should be text, not a number",
        "step 'mail', setting 'password': secrets can't be written in the flow; use {{ env.NAME }}",
        "step 'tok', setting 'message': environment variable NOT_SET_ANYWHERE isn't set",
        "step 'tok', setting 'message': {{ params.nope }}: the flow has no parameter 'nope'",
        "'params' can't be used as a step name",
    ]:
        assert expected in text, (expected, text)


def test_dependency_loops_are_reported(folder):
    spec = {
        "steps": {
            "a": {"block": "log", "settings": {"message": "{{ b }}"}},
            "b": {"block": "log", "settings": {"message": "{{ a }}"}},
        }
    }
    assert "depend on each other: a -> b -> a" in problems(folder, spec)


def test_bad_json_points_at_the_line(folder):
    path = folder / "broken.json"
    path.write_text('{\n  "steps": {\n    "a": {"block": "log",}\n  }\n}\n', encoding="utf-8")
    with pytest.raises(FlowFileError, match="line 3"):
        load_json_flow(path)


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"steps": {}}, "has no steps"),
        ({"steps": {"bad name": {"block": "log", "settings": {"message": "x"}}}}, "step names"),
        ({"steps": {"a": {"settings": {}}}}, "step 'a': missing 'block'"),
        (
            {"steps": {"a": {"block": "log", "settings": {"message": "x"}, "after": ["zz"]}}},
            "'after' names 'zz'",
        ),
        (
            {"steps": {"a": {"block": "log", "settings": {"message": "x"}, "retries": -1}}},
            "retries",
        ),
        ({"steps": {"a": {"block": "log", "settings": {"message": "x"}, "kind": "async"}}}, "kind"),
        (
            {"steps": {"a": {"block": "log", "settings": {"message": "x"}, "colour": 1}}},
            "unknown field 'colour'",
        ),
        (
            {
                "params": {"n": {"type": "decimal"}},
                "steps": {"a": {"block": "log", "settings": {"message": "x"}}},
            },
            "type",
        ),
        (
            {"steps": {"a": {"block": "log", "settings": {"message": "{{ failure.step }}"}}}},
            "only in on_failure",
        ),
        (
            {"steps": {"a": {"block": "log", "settings": {"message": "{{ env }}"}}}},
            "{{ env.NAME }}",
        ),
    ],
)
def test_single_problems(folder, spec, message):
    assert message in problems(folder, spec)


def test_parameter_problems(folder):
    spec = {
        "params": {"n": {"type": "int"}, "on": {"type": "bool", "default": False}},
        "steps": {"a": {"block": "log", "settings": {"message": "{{ params.n }}"}}},
    }
    assert "parameter 'n' needs a value" in problems(folder, spec)
    assert "parameter 'n': 'ten' isn't a whole number" in problems(
        folder, spec, params={"n": "ten"}
    )
    assert "no parameter called 'm'" in problems(folder, spec, params={"n": "1", "m": "2"})
    flow = load_json_flow(write(folder, "f", spec), params={"n": "3", "on": "yes"})
    assert flow.params == {"n": 3, "on": True}


def test_missing_environment(folder):
    spec = {"steps": {"a": {"block": "log", "settings": {"message": "x"}}}}
    assert "no environment called 'prod'" in problems(folder, spec, environment="prod")
