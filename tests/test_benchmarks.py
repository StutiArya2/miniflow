"""Smoke tests for the evaluation harness: tiny sizes, no Dask or plots needed."""

from __future__ import annotations

import csv
import json

import pytest

from benchmarks import experiments as X
from benchmarks import workloads as W
from benchmarks.run import main
from miniflow import DAG


@pytest.mark.parametrize(
    ("edges", "tasks", "depth"),
    [
        (W.chain(5), 5, 5),
        (W.fan_out(4), 6, 3),
        (W.diamond(2, 3), 9, 5),
        (W.layered(4, 3, seed=1), 12, 4),
    ],
)
def test_shapes_are_valid_dags(edges, tasks, depth):
    DAG.from_tasks(W.build(edges, W.noop))
    assert len(edges) == tasks
    assert W.depth(edges) == depth


def test_dask_graph_mirrors_edges():
    graph = W.dask_graph(W.fan_out(2), W.sleep_io, seconds=0)
    assert graph["join"][1:] == ("t0", "t1")
    assert W.sinks(W.fan_out(2)) == ["join"]


def test_flaky_io_is_deterministic():
    W.reset_fault_counters()
    outcomes = []
    for _ in range(5):
        try:
            W.flaky_io(p=0.5, seed=3, name="x", seconds=0)
            outcomes.append(True)
        except ConnectionError:
            outcomes.append(False)
    W.reset_fault_counters()
    again = []
    for _ in range(5):
        try:
            W.flaky_io(p=0.5, seed=3, name="x", seconds=0)
            again.append(True)
        except ConnectionError:
            again.append(False)
    assert outcomes == again


def test_experiments_run_and_stay_correct():
    cfg = X.Config.quick()
    cfg.use_dask = False
    checker = X.Correctness()
    e2 = X.worker_choice(cfg, checker)
    e3 = X.overhead(cfg, checker)
    e4 = X.fault_tolerance(cfg, checker)
    assert [r["config"] for r in e2] == ["all threads", "all processes", "MiniFlow routing"]
    assert all(r["miniflow_memory_us_per_task"] > 0 for r in e3)
    zero_p = [r for r in e4 if r["failure_rate"] == 0.0]
    assert all(r["run_success_rate"] == 1.0 for r in zero_p)
    assert checker.runs > 0
    assert checker.violations == []


def test_run_writes_csvs_and_summary(tmp_path):
    out = tmp_path / "results"
    code = main(["--quick", "--no-dask", "--no-plots", "--only", "e3,e4", "--out", str(out)])
    assert code == 0
    for name in ("e3_overhead.csv", "e4_fault_tolerance.csv", "e5_correctness.csv"):
        assert (out / name).exists()
    with (out / "e5_correctness.csv").open() as f:
        (row,) = list(csv.DictReader(f))
    assert row["violations"] == "0"
    env = json.loads((out / "environment.json").read_text())
    assert env["logical_cpus"] >= 1
    text = (out / "results.md").read_text(encoding="utf-8")
    assert "## 5. Correctness" in text
    assert "0 violations" in text


def test_llm_reuse_experiment_runs_quickly():
    from benchmarks import llm_reuse

    rows = llm_reuse.tolerant_reuse(X.Config.quick(), X.Correctness())
    assert {r["policy"] for r in rows} == set(llm_reuse.POLICIES)
    assert {r["workload"] for r in rows} == set(llm_reuse.WORKLOADS)
    no_reuse = [r for r in rows if r["policy"] == "no reuse"]
    assert all(r["calls_vs_no_reuse"] == 1.0 for r in no_reuse)
    exact = [r for r in rows if r["policy"] == "exact"]
    assert all(r["calls_vs_no_reuse"] < 1.0 for r in exact)
