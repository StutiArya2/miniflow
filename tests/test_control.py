from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from textwrap import dedent

import pytest

from miniflow import DAG, Scheduler, State, StateStore, task
from miniflow.ui.control import ControlError, RunController
from tests import _sched_flows as F

FLOWS = {
    # Returns the fault rate the dashboard passed in, so tests can see it arrive.
    "quick": """
        import os

        @task
        def rate():
            return os.environ.get("MINIFLOW_FAIL_RATE")
        """,
    # Hangs in ``slow`` unless the fault rate is 0, which is what a resume sends.
    "sleepy": """
        import os, time

        @task
        def fast():
            return 21

        @task(after=[fast])
        def slow(x):
            if os.environ.get("MINIFLOW_FAIL_RATE") != "0":
                time.sleep(60)
            return x * 2
        """,
    # Ignores the interrupt, so cancelling has to fall back to killing it.
    "stubborn": """
        import signal, sys, time

        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if sys.platform == "win32":
            signal.signal(signal.SIGBREAK, signal.SIG_IGN)

        @task
        def forever():
            time.sleep(60)
        """,
    # Takes the whole process down mid-run, leaving the run RUNNING in the store.
    "crashy": """
        import os

        @task
        def crash():
            os._exit(3)
        """,
    "broken": """
        raise RuntimeError("boom at import")
        """,
    "_helper": """
        HELPER = 1
        """,
}


FLOW_NAMES = ["broken", "crashy", "quick", "sleepy", "stubborn"]


def wait_until(cond: Callable[[], object], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


@pytest.fixture
def flows_dir(tmp_path: Path) -> Path:
    d = tmp_path / "flows"
    d.mkdir()
    for name, body in FLOWS.items():
        (d / f"{name}.py").write_text("from miniflow import task\n" + dedent(body), "utf-8")
    (d / "notes.txt").write_text("not a flow", "utf-8")
    return d


@pytest.fixture
def store(tmp_path: Path):
    with StateStore(tmp_path / "state.db") as s:
        yield s


@pytest.fixture
def controller(flows_dir: Path, store: StateStore):
    c = RunController(flows_dir, store, grace=0.5)
    yield c
    c.shutdown()


def idle(c: RunController) -> bool:
    return c.status()["active"] is None


def run_of(store: StateStore, flow: str) -> str:
    run_id = store.latest_run(flow)
    assert run_id is not None
    return run_id


def status_of(store: StateStore, run_id: str) -> str:
    return next(r.status for r in store.list_runs() if r.run_id == run_id)


def task_running(store: StateStore, flow: str, name: str) -> bool:
    run_id = store.latest_run(flow)
    return run_id is not None and store.get_states(run_id).get(name) is State.RUNNING


# ------------------------------------------------------------------ flows
def test_lists_only_public_python_files(controller):
    assert controller.status() == {
        "flows": FLOW_NAMES,
        "active": None,
        "running": [],
        "error": None,
        "queued": [],
    }


def test_missing_flows_folder_is_rejected(tmp_path, store):
    with pytest.raises(NotADirectoryError):
        RunController(tmp_path / "nope", store)


# -------------------------------------------------------------------- run
def test_run_passes_the_fault_rate_to_the_flow(controller, store):
    active = controller.start("quick", 0.25)
    assert active["flow"] == "quick"
    assert active["action"] == "run"
    wait_until(lambda: idle(controller))
    run_id = run_of(store, "quick")
    assert status_of(store, run_id) == "SUCCESS"
    assert store.load_completed(run_id) == {"rate": "0.25"}
    assert controller.status()["error"] is None


def test_unknown_flow_is_rejected(controller):
    for name in ["nope", "_helper", "notes", "../quick"]:
        with pytest.raises(ControlError) as err:
            controller.start(name, 0.0)
        assert err.value.status == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize("rate", [-0.1, 1.5, math.nan, "0.5", True, None])
def test_fault_rate_must_be_a_number_from_0_to_1(controller, rate):
    with pytest.raises(ControlError) as err:
        controller.start("quick", rate)
    assert err.value.status == HTTPStatus.BAD_REQUEST
    assert idle(controller)


def test_manual_run_waits_for_the_active_run(controller, store):
    controller.start("sleepy", 0.4)
    assert controller.start("quick", 0.0)["queued"] is True
    assert [item["flow"] for item in controller.status()["queued"]] == ["quick"]
    wait_until(lambda: task_running(store, "sleepy", "slow"))
    controller.cancel()
    wait_until(lambda: idle(controller))
    assert status_of(store, run_of(store, "quick")) == "SUCCESS"


def test_partial_json_run_uses_same_history(controller, store, flows_dir):
    path = flows_dir / "small.json"
    path.write_text(
        '{"steps":{"first":{"block":"log","settings":{"message":"hello"}},'
        '"second":{"block":"log","settings":{"message":"done"},"after":["first"]}}}',
        encoding="utf-8",
    )
    controller.add_flow("small", path)
    controller.start("small", 0)
    wait_until(lambda: idle(controller))
    first = store.latest_run("small")
    assert first is not None
    controller.partial("small", "first")
    wait_until(lambda: idle(controller))
    second = store.latest_run("small")
    assert second is not None
    assert second != first
    assert set(store.get_states(second)) == {"first"}
    assert store.changes_since(second, 0)[0].from_cache


def test_browser_run_selects_environment(controller, store, flows_dir):
    environment = flows_dir / "environments"
    environment.mkdir()
    (environment / "dev.json").write_text('{"message": "from dev"}', encoding="utf-8")
    path = flows_dir / "greeting.json"
    path.write_text(
        '{"params":{"message":{"type":"str","default":"default"}},'
        '"steps":{"say":{"block":"log","settings":{"message":"{{ params.message }}"}}}}',
        encoding="utf-8",
    )
    controller.add_flow("greeting", path)
    controller.start("greeting", 0.0, "dev")
    wait_until(lambda: idle(controller))
    run_id = run_of(store, "greeting")
    assert store.load_completed(run_id)["say"]["message"] == "from dev"
    with pytest.raises(ControlError) as error:
        controller.start("greeting", 0.0, "../secret")
    assert error.value.status == HTTPStatus.BAD_REQUEST
    assert store.latest_run("quick") is None


def test_active_run_is_the_new_run_not_an_older_one_of_the_same_flow(controller, store):
    controller.start("sleepy", 0.0)
    wait_until(lambda: idle(controller))
    older = run_of(store, "sleepy")
    controller.start("sleepy", 0.4)
    assert controller.status()["active"]["run_id"] in (None, run_of(store, "sleepy"))
    wait_until(lambda: task_running(store, "sleepy", "slow"))
    newer = run_of(store, "sleepy")
    assert newer != older
    assert controller.status()["active"]["run_id"] == newer
    controller.cancel()
    assert status_of(store, older) == "SUCCESS"


# ----------------------------------------------------------------- cancel
def test_cancel_interrupts_and_resume_reuses_finished_tasks(flows_dir, store):
    # A long grace period: only the interrupt, not the fallback kill, can stop it in time.
    controller = RunController(flows_dir, store, grace=30)
    controller.start("sleepy", 0.4)
    wait_until(lambda: task_running(store, "sleepy", "slow"))
    assert controller.status()["active"]["run_id"] == run_of(store, "sleepy")

    started = time.monotonic()
    controller.cancel()
    assert time.monotonic() - started < 10
    first = run_of(store, "sleepy")
    assert status_of(store, first) == "INTERRUPTED"
    assert controller.status() == {
        "flows": FLOW_NAMES,
        "active": None,
        "running": [],
        "error": None,
        "queued": [],
    }

    active = controller.resume(first, 0.0)
    assert active["action"] == "resume"
    assert active["flow"] == "sleepy"
    wait_until(lambda: idle(controller))
    second = run_of(store, "sleepy")
    assert second != first
    assert status_of(store, second) == "SUCCESS"
    records = {r.task: r for r in store.changes_since(second, 0.0)}
    assert records["fast"].from_cache
    assert not records["slow"].from_cache
    assert store.load_completed(second) == {"fast": 21, "slow": 42}


def test_cancel_kills_a_run_that_ignores_the_interrupt(controller, store):
    controller.start("stubborn", 0.0)
    wait_until(lambda: task_running(store, "stubborn", "forever"))
    started = time.monotonic()
    controller.cancel()
    assert time.monotonic() - started < 10
    assert status_of(store, run_of(store, "stubborn")) == "INTERRUPTED"
    assert controller.status()["active"] is None
    assert controller.status()["error"] is None  # we asked for it, so it isn't an error


def test_cancel_with_nothing_running_is_refused(controller):
    with pytest.raises(ControlError) as err:
        controller.cancel()
    assert err.value.status == HTTPStatus.CONFLICT


def test_shutdown_cancels_the_active_run(flows_dir, store):
    c = RunController(flows_dir, store, grace=0.5)
    c.start("sleepy", 0.4)
    wait_until(lambda: task_running(store, "sleepy", "slow"))
    c.shutdown()
    assert status_of(store, run_of(store, "sleepy")) == "INTERRUPTED"


# ----------------------------------------------------------------- errors
def test_flow_that_fails_to_load_reports_its_error(controller, store):
    controller.start("broken", 0.0)
    wait_until(lambda: idle(controller))
    error = controller.status()["error"]
    assert "RuntimeError: boom at import" in error
    assert store.latest_run("broken") is None


def test_crashed_run_is_marked_interrupted(controller, store):
    controller.start("crashy", 0.0)
    wait_until(lambda: idle(controller))
    run_id = run_of(store, "crashy")
    assert status_of(store, run_id) == "INTERRUPTED"
    assert run_id in controller.status()["error"]


def test_error_is_cleared_by_the_next_run(controller):
    controller.start("broken", 0.0)
    wait_until(lambda: idle(controller))
    controller.start("quick", 0.0)
    assert controller.status()["error"] is None


# ----------------------------------------------------------------- resume
def test_resume_unknown_run_is_rejected(controller):
    with pytest.raises(ControlError) as err:
        controller.resume("doesnotexist", 0.0)
    assert err.value.status == HTTPStatus.NOT_FOUND


def test_resume_of_a_flow_outside_the_folder_is_rejected(controller, store):
    dag = DAG.from_tasks([task(F.const, name="a", args=(1,))])
    run_id = Scheduler(dag, store=store, flow="elsewhere").run().run_id
    with pytest.raises(ControlError) as err:
        controller.resume(run_id, 0.0)
    assert err.value.status == HTTPStatus.NOT_FOUND
    assert "elsewhere" in str(err.value)


def test_json_flows_are_listed_and_run(tmp_path, store):
    folder = tmp_path / "jflows"
    folder.mkdir()
    (folder / "hello.json").write_text(
        '{"steps": {"say": {"block": "log",'
        ' "settings": {"message": "rate {{ env.MINIFLOW_FAIL_RATE }}"}}}}',
        encoding="utf-8",
    )
    (folder / "old.py").write_text(
        "from miniflow import task\n\n@task\ndef t():\n    return 1\n", "utf-8"
    )
    c = RunController(folder, store, grace=0.5)
    try:
        assert c.status()["flows"] == ["hello", "old"]
        c.start("hello", 0.5)
        wait_until(lambda: idle(c))
        run_id = run_of(store, "hello")
        assert status_of(store, run_id) == "SUCCESS"
        assert store.load_completed(run_id) == {"say": {"level": "info", "message": "rate 0.5"}}
    finally:
        c.shutdown()


# ------------------------------------------------------ flows saved later
LATER = {"steps": {"hello": {"block": "log", "settings": {"message": "hi"}}}}


def test_flow_saved_after_startup_can_be_run(controller, store, flows_dir):
    (flows_dir / "later.json").write_text(json.dumps(LATER), "utf-8")
    controller.start("later", 0)
    wait_until(lambda: idle(controller))
    assert status_of(store, run_of(store, "later")) == "SUCCESS"
    assert "later" in controller.flows


@pytest.mark.parametrize("name", ["../outside", "_private", "a/b", "", "later.json"])
def test_names_that_are_not_flows_in_the_folder_are_still_refused(controller, flows_dir, name):
    (flows_dir.parent / "outside.json").write_text(json.dumps(LATER), "utf-8")
    (flows_dir / "_private.json").write_text(json.dumps(LATER), "utf-8")
    with pytest.raises(ControlError):
        controller.start(name, 0)


def test_a_symlinked_flow_saved_later_is_refused(controller, flows_dir, tmp_path):
    target = tmp_path / "real.json"
    target.write_text(json.dumps(LATER), "utf-8")
    try:
        (flows_dir / "linked.json").symlink_to(target)
    except OSError:
        pytest.skip("symlinks need extra rights on this system")
    with pytest.raises(ControlError):
        controller.start("linked", 0)


def test_cached_flow_replaced_with_symlink_is_refused(controller, flows_dir, tmp_path):
    (flows_dir / "later.json").write_text(json.dumps(LATER), "utf-8")
    assert controller._flow_path("later") is not None
    target = tmp_path / "external.json"
    target.write_text(json.dumps(LATER), "utf-8")
    (flows_dir / "later.json").unlink()
    try:
        (flows_dir / "later.json").symlink_to(target)
    except OSError:
        pytest.skip("symlinks need extra rights")
    with pytest.raises(ControlError):
        controller.start("later", 0)


def test_flow_saved_later_can_be_enqueued_and_resumed(controller, store, flows_dir):
    (flows_dir / "later.json").write_text(json.dumps(LATER), "utf-8")
    assert controller.enqueue("later", source="schedule:probe") == "started"
    wait_until(lambda: idle(controller))
    run_id = run_of(store, "later")
    assert status_of(store, run_id) == "SUCCESS"
    second = RunController(flows_dir, store, grace=0.5)
    try:
        second.flows.pop("later")  # recreate the unknown-name lookup on the resume path
        second.resume(run_id, 0)
        wait_until(lambda: idle(second))
        assert status_of(store, run_of(store, "later")) == "SUCCESS"
    finally:
        second.shutdown()
