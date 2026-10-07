import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from textwrap import dedent

import pytest

from miniflow import State, StateStore
from miniflow.cli import load_flow, main

EXAMPLE = Path(__file__).parent.parent / "examples" / "pipeline.py"


def write_flow(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / f"{name}.py"
    path.write_text("from miniflow import task\n" + dedent(body), encoding="utf-8")
    return path


FIXABLE = """
import os

@task
def first():
    return 1

@task(after=[first])
def second(x):
    if not os.environ.get("MINIFLOW_FIXED"):
        raise RuntimeError("not fixed yet")
    return x + 1

@task(after=[second])
def third(x):
    return x * 10
"""


# ------------------------------------------------------------------ validate/graph
def test_load_flow():
    _, dag = load_flow(EXAMPLE)
    assert dag.topological_order() == ["fetch", "analyse", "publish"]


def test_validate(capsys):
    assert main(["validate", str(EXAMPLE)]) == 0
    assert "fetch -> analyse -> publish" in capsys.readouterr().out


def test_graph_to_file(tmp_path):
    out = tmp_path / "g.dot"
    assert main(["graph", str(EXAMPLE), "-o", str(out), "--db", str(tmp_path / "none.db")]) == 0
    assert '"fetch" -> "analyse";' in out.read_text()


def test_graph_stdout(capsys, tmp_path):
    assert main(["graph", str(EXAMPLE), "--db", str(tmp_path / "none.db")]) == 0
    assert "digraph" in capsys.readouterr().out


def test_cycle_exits_with_error(tmp_path, capsys):
    flow = write_flow(
        tmp_path,
        "cyclic",
        """
        @task(after=['b'])
        def a(): pass
        @task(after=['a'])
        def b(): pass
        """,
    )
    assert main(["validate", str(flow)]) == 2
    assert "cycle" in capsys.readouterr().err


def test_missing_file():
    with pytest.raises(FileNotFoundError):
        load_flow("nope.py")


# --------------------------------------------------------------- run/resume/status
def test_run_example_pipeline(tmp_path, capsys):
    db = tmp_path / "state.db"
    assert main(["run", str(EXAMPLE), "--db", str(db), "--plain"]) == 0
    out = capsys.readouterr().out
    assert "SUCCESS" in out
    assert "pid-" in out  # analyse ran in a worker process
    with StateStore(db) as store:
        run_id = store.latest_run("pipeline")
        assert run_id is not None
        assert set(store.get_states(run_id).values()) == {State.SUCCESS}


def test_quiet_run(tmp_path, capsys):
    assert main(["run", str(EXAMPLE), "--db", str(tmp_path / "s.db"), "--quiet"]) == 0
    assert capsys.readouterr().out == ""


def test_failed_run_then_resume(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("MINIFLOW_FIXED", raising=False)
    flow = write_flow(tmp_path, "fixable", FIXABLE)
    db = str(tmp_path / "state.db")

    assert main(["run", str(flow), "--db", db, "--plain"]) == 1
    out = capsys.readouterr().out
    assert "RuntimeError: not fixed yet" in out
    assert "resume with" in out

    monkeypatch.setenv("MINIFLOW_FIXED", "1")
    assert main(["resume", str(flow), "--db", db, "--plain"]) == 0
    out = capsys.readouterr().out
    assert "reused from previous run" in out
    assert "1 reused" in out

    assert main(["status", "--db", db]) == 0
    status = capsys.readouterr().out
    assert "reused" in status
    assert "third" in status

    assert main(["status", "--db", db, "--all"]) == 0
    runs = capsys.readouterr().out
    assert runs.count("fixable") == 2
    assert "FAILED" in runs
    assert "SUCCESS" in runs


def test_retry_options(tmp_path, capsys):
    flow = write_flow(
        tmp_path,
        "flaky_cli",
        """
        calls = []

        @task(retries=2)
        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("try again")
            return len(calls)
        """,
    )
    args = ["run", str(flow), "--db", str(tmp_path / "s.db"), "--plain", "--retry-base", "0.01"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert out.count("RETRYING") == 2
    assert "retry in" in out


def test_resume_without_runs(tmp_path, capsys):
    flow = write_flow(tmp_path, "never_ran", "@task\ndef a():\n    return 1\n")
    assert main(["resume", str(flow), "--db", str(tmp_path / "s.db")]) == 2
    assert "no earlier run" in capsys.readouterr().err


def test_status_without_database(tmp_path, capsys):
    assert main(["status", "--db", str(tmp_path / "missing.db")]) == 2


def test_graph_coloured_by_latest_run(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("MINIFLOW_FIXED", raising=False)
    flow = write_flow(tmp_path, "coloured", FIXABLE)
    db = str(tmp_path / "state.db")
    main(["run", str(flow), "--db", db, "--quiet"])
    assert main(["graph", str(flow), "--db", db]) == 0
    dot = capsys.readouterr().out
    assert "palegreen" in dot  # first
    assert "salmon" in dot  # second
    assert "lightgrey" in dot  # third skipped


@pytest.mark.skipif(shutil.which("dot") is None, reason="Graphviz dot binary not installed")
def test_graph_render_svg(tmp_path):
    out = tmp_path / "flow.svg"
    assert main(["graph", str(EXAMPLE), "--render", "svg", "-o", str(out), "--db", "x.db"]) == 0
    assert out.read_text(encoding="utf-8").lstrip().startswith(("<?xml", "<svg", "<!--"))


# --------------------------------------------------------------- kill and resume
def _wait_for_exit(pid: int, timeout: float) -> None:
    """Block until process ``pid`` (not necessarily our child) has exited."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if handle:  # 0 means it is already gone
            kernel32.WaitForSingleObject(handle, int(timeout * 1000))
            kernel32.CloseHandle(handle)
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)


def _kill_tree(proc: subprocess.Popen, real_pid: int | None) -> None:
    # On Windows a venv's python.exe is a launcher that runs the real interpreter as a
    # child, so kill the whole tree and wait for the process that holds the database.
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()
    proc.wait(timeout=10)
    if real_pid is not None:
        _wait_for_exit(real_pid, timeout=10)


def test_killed_process_resumes_without_redoing_work(tmp_path, capsys, monkeypatch):
    """Spec §3.6 Resume: kill a real run mid-way, then resume it."""
    pid_file = tmp_path / "worker.pid"
    flow = write_flow(
        tmp_path,
        "killable",
        f"""
        import os, time

        @task
        def fast():
            return 21

        @task(after=[fast])
        def slow(x):
            if not os.environ.get("MINIFLOW_FAST"):
                with open({str(pid_file)!r}, "w") as f:
                    f.write(str(os.getpid()))
                time.sleep(120)
            return x * 2
        """,
    )
    db = tmp_path / "state.db"
    env = {k: v for k, v in os.environ.items() if k != "MINIFLOW_FAST"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "miniflow", "run", str(flow), "--db", str(db), "--quiet"],
        env=env,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            assert time.monotonic() < deadline, "run never got going"
            assert proc.poll() is None, "run exited early"
            if db.exists():
                with StateStore(db) as store:
                    run_id = store.latest_run("killable")
                    states = store.get_states(run_id) if run_id else {}
                if (
                    states.get("fast") is State.SUCCESS
                    and states.get("slow") is State.RUNNING
                    and pid_file.exists()
                    and pid_file.read_text()
                ):
                    break
            time.sleep(0.1)
    finally:
        real_pid = int(pid_file.read_text()) if pid_file.exists() and pid_file.read_text() else None
        _kill_tree(proc, real_pid)

    monkeypatch.setenv("MINIFLOW_FAST", "1")
    assert main(["resume", str(flow), "--db", str(db), "--plain"]) == 0
    with StateStore(db) as store:
        new_run = store.latest_run("killable")
        assert new_run != run_id
        records = {r.task: r for r in store.changes_since(new_run, 0.0)}
        assert records["fast"].from_cache
        assert records["slow"].state is State.SUCCESS
        assert store.load_completed(new_run) == {"fast": 21, "slow": 42}


def test_cli_exits_promptly_after_abandoning_a_timed_out_task(tmp_path):
    flow = write_flow(
        tmp_path,
        "hangs",
        """
        import time

        @task(timeout=0.3)
        def stuck():
            time.sleep(60)
        """,
    )
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "miniflow", "run", str(flow), "--db", str(tmp_path / "s.db")],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert proc.returncode == 1
    assert time.monotonic() - start < 20
    assert "TaskTimeoutError" in proc.stdout
    assert "abandoned" in proc.stderr


def test_rich_reporter_renders_live_table():
    pytest.importorskip("rich")
    from miniflow import Event
    from miniflow.cli import RichReporter

    _, dag = load_flow(EXAMPLE)
    reporter = RichReporter(dag)
    with reporter:
        reporter(Event("r", "fetch", State.RUNNING, 1, time.time()))
        reporter(Event("r", "fetch", State.RETRYING, 1, time.time(), "thread-1", "Boom", 0.5))
        reporter(Event("r", "fetch", State.SUCCESS, 2, time.time(), "thread-1"))
        reporter(Event("r", "analyse", State.SUCCESS, 0, time.time(), from_cache=True))
    assert reporter.rows["fetch"]["state"] is State.SUCCESS
    assert reporter.rows["fetch"]["attempt"] == 2
    assert reporter.rows["analyse"]["note"] == "reused from previous run"
