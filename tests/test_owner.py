"""One dashboard runs flows per state database: the owner file."""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from miniflow.cli import main
from miniflow.ui import DashboardServer
from miniflow.ui.owner import Owner, OwnerError, claim, is_live, owner_path, read_owner, release
from tests.test_author import request

HELLO = {"steps": {"hello": {"block": "log", "settings": {"message": "hi"}}}}


@pytest.fixture
def flows(tmp_path: Path) -> Path:
    d = tmp_path / "flows"
    d.mkdir()
    (d / "hello.json").write_text(json.dumps(HELLO), "utf-8")
    return d


def write_owner(db: Path, url: str, token: str = "old", flows: str = "/x") -> None:
    owner = Owner(pid=1, url=url, flows=flows, token=token, started_at=time.time() - 60)
    owner_path(db).write_text(json.dumps(owner.__dict__), "utf-8")
    old = time.time() - 60
    os.utime(owner_path(db), (old, old))


def test_a_dashboard_with_flows_claims_the_database(tmp_path, flows):
    db = tmp_path / "s.db"
    with DashboardServer(db, port=0, flows=flows) as server:
        owner = read_owner(db)
        assert owner is not None
        assert owner.url == server.url
        assert owner.flows == str(flows.resolve())
        assert is_live(owner)
        assert request(server, "GET", "/api/owner") == (200, {"token": owner.token})
    assert not owner_path(db).exists()  # released on stop


def test_a_read_only_dashboard_claims_nothing(tmp_path):
    db = tmp_path / "s.db"
    with DashboardServer(db, port=0) as server:
        assert not owner_path(db).exists()
        assert request(server, "GET", "/api/owner") == (200, {"token": None})


def test_a_second_dashboard_on_the_same_database_is_refused(tmp_path, flows):
    db = tmp_path / "s.db"
    with DashboardServer(db, port=0, flows=flows) as first:
        second = DashboardServer(db, port=0, flows=flows)
        with pytest.raises(OwnerError, match="already running at " + first.url):
            second.start()
        assert read_owner(db).url == first.url  # the first keeps its claim


def test_the_cli_reports_the_running_dashboard(tmp_path, flows, capsys):
    db = tmp_path / "s.db"
    with DashboardServer(db, port=0, flows=flows) as first:
        code = main(["ui", "--db", str(db), "--flows", str(flows), "--port", "0", "--no-browser"])
        assert code == 2
        assert first.url in capsys.readouterr().err


def test_a_file_left_by_a_crashed_dashboard_is_replaced(tmp_path, flows):
    db = tmp_path / "s.db"
    db.touch()
    write_owner(db, "http://127.0.0.1:9")  # nothing listens on the discard port
    with DashboardServer(db, port=0, flows=flows) as server:
        assert read_owner(db).url == server.url


def test_a_port_reused_by_another_program_is_not_live(tmp_path, flows):
    class Other(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"token": "someone-else"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    other = ThreadingHTTPServer(("127.0.0.1", 0), Other)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        db = tmp_path / "s.db"
        db.touch()
        write_owner(db, f"http://127.0.0.1:{other.server_address[1]}", token="mine")
        assert not is_live(read_owner(db))
        with DashboardServer(db, port=0, flows=flows) as server:
            assert read_owner(db).url == server.url
    finally:
        other.shutdown()
        other.server_close()


def test_a_half_written_new_file_means_another_dashboard_is_starting(tmp_path, flows):
    db = tmp_path / "s.db"
    db.touch()
    owner_path(db).write_text("{", "utf-8")  # fresh and unreadable
    with pytest.raises(OwnerError, match="starting"):
        claim(db, "http://127.0.0.1:1", flows)


def test_release_leaves_someone_elses_claim(tmp_path, flows):
    db = tmp_path / "s.db"
    db.touch()
    mine = claim(db, "http://127.0.0.1:1", flows)
    owner_path(db).unlink()
    theirs = claim(db, "http://127.0.0.1:2", flows)
    release(db, mine)
    assert read_owner(db) == theirs


@pytest.mark.parametrize("url", ["https://127.0.0.1:1", "http://example.com:80", "nonsense"])
def test_only_local_http_addresses_are_probed(url):
    assert not is_live(Owner(pid=1, url=url, flows="/x", token="t", started_at=0.0))


def test_an_old_unreadable_file_is_replaced(tmp_path, flows):
    db = tmp_path / "s.db"
    db.touch()
    owner_path(db).write_text("{", "utf-8")
    old = time.time() - 60
    os.utime(owner_path(db), (old, old))
    with DashboardServer(db, port=0, flows=flows) as server:
        assert read_owner(db).url == server.url


def test_a_file_that_changed_since_it_was_judged_dead_is_kept(tmp_path, flows, monkeypatch):
    import miniflow.ui.owner as owner_module

    db = tmp_path / "s.db"
    db.touch()
    write_owner(db, "http://127.0.0.1:9", token="dead")
    calls: list[int] = []

    def fake_is_live(owner: Owner, timeout: float = 2.0) -> bool:
        calls.append(1)
        if len(calls) == 1:
            write_owner(db, "http://127.0.0.1:8", token="new")  # another dashboard won
            return False
        return True

    monkeypatch.setattr(owner_module, "is_live", fake_is_live)
    with pytest.raises(OwnerError, match=r"already running at http://127\.0\.0\.1:8"):
        claim(db, "http://127.0.0.1:1", flows)
    assert read_owner(db).token == "new"
