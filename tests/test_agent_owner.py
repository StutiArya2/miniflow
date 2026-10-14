"""DashboardLink: runs from an agent go to the one dashboard that owns the database."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from miniflow import StateStore
from miniflow.agent.owner import DashboardLink, LinkError
from miniflow.agent.tools import Toolbox, call
from miniflow.ui import DashboardServer
from miniflow.ui.owner import is_live, owner_path, read_owner

HELLO = {"steps": {"hello": {"block": "log", "settings": {"message": "hi"}}}}
SLOW = {"steps": {"pause": {"block": "wait", "settings": {"seconds": 3}}}}


def wait_until(cond, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


@pytest.fixture
def flows(tmp_path: Path) -> Path:
    d = tmp_path / "flows"
    d.mkdir()
    (d / "hello.json").write_text(json.dumps(HELLO), "utf-8")
    (d / "slow.json").write_text(json.dumps(SLOW), "utf-8")
    return d


def statuses(db: Path, flow: str) -> list[str]:
    with StateStore(db) as store:
        return [r.status for r in store.list_runs() if r.flow == flow]


def test_without_a_dashboard_the_link_starts_one_and_stops_it(tmp_path, flows):
    db = tmp_path / "s.db"
    link = DashboardLink(flows, db)
    try:
        link.run("hello", None)
        wait_until(lambda: statuses(db, "hello") == ["SUCCESS"])
        owner = read_owner(db)
        assert owner is not None
        assert owner.url == link.url()
    finally:
        link.close()
    wait_until(lambda: not owner_path(db).exists())


def test_an_existing_dashboard_is_used(tmp_path, flows):
    db = tmp_path / "s.db"
    with DashboardServer(db, port=0, flows=flows) as server:
        link = DashboardLink(flows, db, start=False)
        assert link.url() == server.url
        link.run("hello", None)
        wait_until(lambda: statuses(db, "hello") == ["SUCCESS"])
        link.close()  # it didn't start this dashboard, so it leaves it alone
        assert is_live(read_owner(db))


def test_agent_runs_share_the_dashboards_queue(tmp_path, flows):
    db = tmp_path / "s.db"
    with DashboardServer(db, port=0, flows=flows) as server:
        server.controller.start("slow", 0)
        link = DashboardLink(flows, db, start=False)
        assert link.run("slow", None).get("queued") is True
        assert [q["flow"] for q in link.control()["queued"]] == ["slow"]


def test_a_dashboard_for_another_folder_is_an_error(tmp_path, flows):
    other = tmp_path / "other"
    other.mkdir()
    db = tmp_path / "s.db"
    with (
        DashboardServer(db, port=0, flows=other),
        pytest.raises(LinkError, match="runs flows from"),
    ):
        DashboardLink(flows, db, start=False).url()


def test_no_dashboard_and_no_starting(tmp_path, flows):
    with pytest.raises(LinkError, match="no dashboard"):
        DashboardLink(flows, tmp_path / "s.db", start=False).url()
    assert DashboardLink(flows, tmp_path / "s.db", start=False).control(start=False) == {
        "running": [],
        "queued": [],
    }


def test_two_links_starting_together_share_one_dashboard(tmp_path, flows):
    db = tmp_path / "s.db"
    links = [DashboardLink(flows, db), DashboardLink(flows, db)]
    urls: list[str] = []
    threads = [
        threading.Thread(target=lambda current=link: urls.append(current.url())) for link in links
    ]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(40)
        assert len(urls) == 2
        assert urls[0] == urls[1]
    finally:
        for link in links:
            link.close()


def test_a_dashboard_left_running_is_reused(tmp_path, flows):
    db = tmp_path / "s.db"
    first = DashboardLink(flows, db)
    try:
        url = first.url()  # the first MCP server "crashes" here: close() never runs
        second = DashboardLink(flows, db)
        assert second.url() == url
        second.close()
        assert is_live(read_owner(db))
    finally:
        first.close()


def test_closing_owned_dashboard_interrupts_an_active_run(tmp_path, flows):
    db = tmp_path / "s.db"
    link = DashboardLink(flows, db)
    try:
        link.run("slow", None)
        wait_until(lambda: bool(statuses(db, "slow")))
    finally:
        link.close()
    assert statuses(db, "slow") == ["INTERRUPTED"]
    assert not owner_path(db).exists()


def test_relative_database_reports_its_absolute_path(tmp_path, flows, monkeypatch):
    monkeypatch.chdir(tmp_path)
    link = DashboardLink(flows, "relative.db")
    try:
        result = call(Toolbox(flows, "relative.db", runs=link), "run_flow", {"name": "hello"})
        assert result["database"] == str(tmp_path / "relative.db")
        assert result["dashboard"] == link.url()
        wait_until(lambda: statuses(tmp_path / "relative.db", "hello") == ["SUCCESS"])
    finally:
        link.close()
