"""examples/sales_report.json, run on a copy of the examples folder."""

from __future__ import annotations

import csv
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from miniflow import StateStore
from miniflow.cli import main

EXAMPLES = Path(__file__).parent.parent / "examples"


@pytest.fixture
def copy(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    for name in ("data", "blocks", "environments"):
        shutil.copytree(EXAMPLES / name, tmp_path / name)
    shutil.copy(EXAMPLES / "sales_report.json", tmp_path)
    return tmp_path


def orders_over(folder: Path, minimum: float) -> list[dict]:
    rates = {"INR": 1, "USD": 83, "EUR": 90}
    with (folder / "data" / "sales.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if float(r["amount"]) * rates[r["currency"]] >= minimum]


def test_sales_report(copy):
    db = copy / "s.db"
    assert main(["run", str(copy / "sales_report.json"), "--db", str(db), "-q"]) == 0
    expected = orders_over(copy, 20000)
    with (copy / "out" / "revenue_by_region.csv").open(encoding="utf-8") as f:
        regions = list(csv.DictReader(f))
    assert sum(int(r["orders"]) for r in regions) == len(expected)
    revenues = [float(r["revenue_inr"]) for r in regions]
    assert revenues == sorted(revenues, reverse=True)
    with sqlite3.connect(copy / "out" / "sales.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM big_orders").fetchone() == (len(expected),)
    with StateStore(db) as store:
        summary = store.load_completed(store.latest_run("sales_report"))["summary"]
    assert summary["message"].startswith(f"{len(expected)} orders of at least 20000 INR")


def test_sales_report_with_prod_environment(copy):
    db = copy / "s.db"
    args = ["run", str(copy / "sales_report.json"), "--db", str(db), "-q", "--environment", "prod"]
    assert main(args) == 0
    with StateStore(db) as store:
        summary = store.load_completed(store.latest_run("sales_report"))["summary"]
    assert summary["message"].startswith(
        f"{len(orders_over(copy, 100000))} orders of at least 100000"
    )
