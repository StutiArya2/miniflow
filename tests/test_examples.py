"""The real-world example pipelines, run offline from their saved real-data snapshots."""

from __future__ import annotations

import gzip
import json

import pytest

from miniflow import DAG, Scheduler, State, StateStore, task
from miniflow.cli import main
from miniflow.examples import NAMES, catalog, load
from miniflow.examples._common import SNAPSHOTS, fetch_json, fetch_text, inr
from miniflow.graph import describe_module
from miniflow.preview import summarize


@pytest.fixture
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("MINIFLOW_OFFLINE", "1")
    monkeypatch.setenv("MINIFLOW_REPORTS", str(tmp_path / "reports"))
    return tmp_path


# ------------------------------------------------------------------- catalog
def test_catalog_describes_every_example():
    entries = {e["name"]: e for e in catalog()}
    assert set(entries) == set(NAMES)
    for e in entries.values():
        assert e["title"]
        assert len(e["description"]) > 80
        assert e["watch"]
        assert sum(e["kinds"].values()) == e["tasks"]
        assert all(e["kinds"][k] > 0 for k in ("io", "cpu", "async"))


@pytest.mark.parametrize("name", NAMES)
def test_every_step_explains_itself(name):
    _, dag = load(name)
    for t in dag.tasks.values():
        assert len(t.description) > 15, f"{name}.{t.name} has no description"
    for h in dag.about.get("highlights", []):
        assert h in dag.tasks


def test_unknown_example():
    with pytest.raises(KeyError, match="choose from"):
        load("nope")


# ------------------------------------------------------------- offline runs
@pytest.mark.parametrize("name", NAMES)
def test_example_runs_offline(name, offline):
    _, dag = load(name)
    with StateStore(offline / "state.db") as store:
        result = Scheduler(dag, store=store, flow=name).run()
        records = {r.task: r for r in store.changes_since(result.run_id, 0.0)}
    assert result.ok, result.errors
    # The only failures allowed are optional steps (a website that never answered live).
    for n, state in result.states.items():
        assert state is State.SUCCESS or dag.tasks[n].optional, (n, state)
    for h in dag.about.get("highlights", []):
        assert records[h].preview, f"{h} has no result text"
    assert any((offline / "reports").iterdir())


def test_offline_replays_a_recorded_failure(offline):
    failures = sorted(SNAPSHOTS.glob("*.err.gz"))
    if not failures:
        pytest.skip("no recorded failures in the snapshots")
    key = failures[0].name.removesuffix(".err.gz")
    with pytest.raises(ConnectionError, match="replayed"):
        fetch_text("https://example.invalid", snapshot=key)


def test_offline_missing_snapshot(offline):
    with pytest.raises(FileNotFoundError, match="no saved snapshot"):
        fetch_json("https://example.invalid", snapshot="does_not_exist")


def test_snapshots_are_valid_json():
    for path in SNAPSHOTS.glob("*.json.gz"):
        json.loads(gzip.decompress(path.read_bytes()))


def test_inr_uses_indian_grouping():
    assert inr(0) == "₹0"
    assert inr(999) == "₹999"
    assert inr(1234567.8) == "₹12,34,568"
    assert inr(-100000) == "-₹1,00,000"


# --------------------------------------------------------- descriptions etc.
def documented():
    """Fetch the morning data.

    More detail that is not part of the description.
    """


def test_description_from_docstring_and_override():
    assert task(documented).description == "Fetch the morning data."
    assert task(documented, description="Custom").description == "Custom"
    assert task(lambda: None, name="anon").description == ""


def test_describe_module_reads_docstring_watch_and_highlights():
    import types

    mod = types.ModuleType("flow")
    mod.__doc__ = "My flow\n\nIt does useful\nthings.\n\nRun it: ..."
    mod.WATCH = "Watch   this."
    mod.HIGHLIGHTS = ["a"]
    assert describe_module(mod) == {
        "title": "My flow",
        "description": "It does useful things.",
        "watch": "Watch this.",
        "highlights": ["a"],
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ({"summary": "  12 quakes  ", "data": [1, 2]}, "12 quakes"),
        ("short text", "short text"),
        ([1, 2, 3], "[1, 2, 3]"),
    ],
)
def test_summarize(value, expected):
    assert summarize(value) == expected


def test_summarize_trims_long_values():
    assert len(summarize("x" * 1000)) == 300
    assert len(summarize(list(range(1000)))) <= 300


def test_cli_lists_and_runs_examples(offline, capsys, monkeypatch):
    assert main(["examples"]) == 0
    listing = capsys.readouterr().out
    assert all(n in listing for n in NAMES)
    monkeypatch.setenv("MINIFLOW_OFFLINE", "0")  # restored afterwards; --offline sets it
    db = str(offline / "cli.db")
    assert main(["example", "earthquakes", "--offline", "--db", db, "--quiet"]) == 0
    assert main(["example", "earthquakes", "--offline", "--db", db, "--quiet", "--resume"]) == 0
    with StateStore(db) as store:
        latest = store.latest_run("earthquakes")
        assert all(r.from_cache for r in store.changes_since(latest, 0.0))


# ---------------------------------------------------------------- optional
def ok(*_up):
    return "fine"


def broken(*_up):
    raise ValueError("site down")


def receives(*values):
    return {"summary": f"got {values!r}"}


def test_optional_failure_does_not_skip_downstream(tmp_path):
    tasks = [
        task(ok, name="a"),
        task(broken, name="flaky", after=["a"], optional=True),
        task(receives, name="digest", after=["a", "flaky"]),
    ]
    with StateStore(tmp_path / "s.db") as store:
        result = Scheduler(DAG.from_tasks(tasks), store=store).run()
        graph = store.get_graph(result.run_id)
    assert result.ok
    assert result.states["flaky"] is State.FAILED
    assert result.states["digest"] is State.SUCCESS
    assert result["digest"] == {"summary": "got ('fine', None)"}
    assert graph["tasks"]["flaky"]["optional"] is True


def test_required_failure_still_fails_the_run(tmp_path):
    tasks = [task(broken, name="flaky"), task(receives, name="digest", after=["flaky"])]
    with StateStore(tmp_path / "s.db") as store:
        result = Scheduler(DAG.from_tasks(tasks), store=store).run()
    assert result.status == "FAILED"
    assert result.states["digest"] is State.SKIPPED


def test_store_adds_preview_column_to_old_databases(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE task_states (run_id TEXT NOT NULL, task TEXT NOT NULL, state TEXT NOT NULL,"
        " attempts INTEGER NOT NULL DEFAULT 0, result BLOB, error TEXT,"
        " from_cache INTEGER NOT NULL DEFAULT 0, started_at REAL, finished_at REAL,"
        " updated_at REAL NOT NULL, PRIMARY KEY (run_id, task));"
    )
    con.close()
    with StateStore(db) as store:
        cols = {r[1] for r in store._conn.execute("PRAGMA table_info(task_states)")}
    assert "preview" in cols
    assert {"sample", "item_count"} <= cols
