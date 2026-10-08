"""The built-in blocks, called directly with their settings."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from miniflow.blocks import BlockError, block, get_block

ROWS = [
    {"name": "pen", "region": "North", "price": 120},
    {"name": "cup", "region": "South", "price": 40},
    {"name": "ink", "region": "North", "price": 300},
]


def call(name: str, **settings):
    return get_block(name).fn(**settings)


# -------------------------------------------------------------------- http
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/missing"):
            self._reply(404, b"nope", "text/plain")
        elif self.path.startswith("/text"):
            self._reply(200, b"hello", "text/plain")
        else:
            payload = {"path": self.path, "auth": self.headers.get("Authorization")}
            self._reply(200, json.dumps(payload).encode(), "application/json")

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        echo = {"got": json.loads(body), "type": self.headers["Content-Type"]}
        self._reply(201, json.dumps(echo).encode(), "application/json")


@pytest.fixture(scope="module")
def http_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_http_get_json_with_query_and_headers(http_url):
    out = call(
        "http_request",
        url=f"{http_url}/items",
        query={"page": 2},
        headers={"Authorization": "Bearer t"},
    )
    assert out["status"] == 200
    assert out["json"] == {"path": "/items?page=2", "auth": "Bearer t"}


def test_http_text_response(http_url):
    out = call("http_request", url=f"{http_url}/text")
    assert out["text"] == "hello"
    assert out["json"] is None


def test_http_post_sends_json(http_url):
    out = call("http_request", url=http_url, method="POST", body={"a": 1})
    assert out["status"] == 201
    assert out["json"] == {"got": {"a": 1}, "type": "application/json"}


def test_http_error_status_fails_unless_allowed(http_url):
    with pytest.raises(RuntimeError, match="404"):
        call("http_request", url=f"{http_url}/missing")
    out = call("http_request", url=f"{http_url}/missing", fail_on_error=False)
    assert out["status"] == 404
    assert out["text"] == "nope"


# -------------------------------------------------------------------- files
def test_csv_round_trip_converts_numbers(tmp_path):
    path = tmp_path / "out" / "rows.csv"
    written = call("write_csv", path=str(path), rows=ROWS)
    assert written == {"path": str(path), "rows": 3}
    assert path.read_text(encoding="utf-8").splitlines()[0] == "name,region,price"
    assert call("read_csv", path=str(path)) == ROWS
    raw = call("read_csv", path=str(path), convert_numbers=False)
    assert raw[0]["price"] == "120"


def test_write_csv_column_order_and_missing_values(tmp_path):
    path = tmp_path / "rows.csv"
    call("write_csv", path=str(path), rows=[{"a": 1}, {"b": 2.5}], columns=["b", "a"])
    assert path.read_text(encoding="utf-8").splitlines() == ["b,a", ",1", "2.5,"]


def test_json_round_trip(tmp_path):
    path = tmp_path / "data.json"
    out = call("write_json", path=str(path), data={"rows": ROWS})
    assert out["path"] == str(path)
    assert out["bytes"] == path.stat().st_size
    assert call("read_json", path=str(path)) == {"rows": ROWS}


def test_missing_file_is_explained(tmp_path):
    with pytest.raises(FileNotFoundError, match="no such file"):
        call("read_csv", path=str(tmp_path / "nope.csv"))


# ---------------------------------------------------------------- transform
def test_filter():
    assert call("filter", rows=ROWS, where="price > 100 and region == 'North'") == [
        ROWS[0],
        ROWS[2],
    ]


def test_filter_names_the_row_that_fails():
    with pytest.raises(ValueError, match="row 2"):
        call("filter", rows=[{"price": 1}, {"cost": 2}], where="price > 0")


def test_select_keeps_and_renames():
    assert call("select", rows=ROWS[:1], fields=["name", "price"]) == [
        {"name": "pen", "price": 120}
    ]
    assert call("select", rows=ROWS[:1], fields={"item": "name"}) == [{"item": "pen"}]
    with pytest.raises(ValueError, match="Did you mean 'price'"):
        call("select", rows=ROWS, fields=["prize"])


def test_aggregate_with_and_without_groups():
    totals = call(
        "aggregate",
        rows=ROWS,
        group_by=["region"],
        metrics={
            "items": "count",
            "total": "sum(price)",
            "average": "avg(price)",
            "top": "max(price)",
        },
    )
    assert totals == [
        {"region": "North", "items": 2, "total": 420, "average": 210.0, "top": 300},
        {"region": "South", "items": 1, "total": 40, "average": 40.0, "top": 40},
    ]
    assert call("aggregate", rows=ROWS, metrics={"cheapest": "min(price)"}) == [{"cheapest": 40}]
    with pytest.raises(ValueError, match="median"):
        call("aggregate", rows=ROWS, metrics={"m": "median(price)"})


def test_aggregate_rounds_floats():
    rows = [{"x": 0.1}, {"x": 0.2}, {"x": 0.4}]
    assert call("aggregate", rows=rows, metrics={"s": "sum(x)", "a": "avg(x)"}) == [
        {"s": 0.7, "a": 0.23}
    ]
    assert call("aggregate", rows=rows, metrics={"a": "avg(x)"}, decimals=4) == [{"a": 0.2333}]
    exact = call("aggregate", rows=rows, metrics={"s": "sum(x)"}, decimals=None)
    assert exact == [{"s": 0.1 + 0.2 + 0.4}]


def test_sort():
    assert [r["name"] for r in call("sort", rows=ROWS, by="price")] == ["cup", "pen", "ink"]
    by_region = call("sort", rows=ROWS, by=["region", "price"], descending=True)
    assert [r["name"] for r in by_region] == ["cup", "ink", "pen"]
    with_none = call("sort", rows=[{"p": 2}, {"p": None}, {"p": 1}], by="p")
    assert [r["p"] for r in with_none] == [1, 2, None]


# ------------------------------------------------------------------ systems
def test_shell_captures_output(tmp_path):
    out = call("shell", command=[sys.executable, "-c", "print('hi')"], cwd=str(tmp_path))
    assert out["code"] == 0
    assert out["stdout"].strip() == "hi"


def test_shell_failure_raises_unless_allowed():
    command = [sys.executable, "-c", "import sys; sys.exit(3)"]
    with pytest.raises(RuntimeError, match="exit code 3"):
        call("shell", command=command)
    assert call("shell", command=command, check=False)["code"] == 3


def test_sql_select_insert_many(tmp_path):
    db = str(tmp_path / "shop.db")
    call("sql", database=db, query="CREATE TABLE items (name TEXT, price REAL)")
    out = call("sql", database=db, query="INSERT INTO items VALUES (:name, :price)", many=ROWS)
    assert out == {"changed": 3}
    rows = call(
        "sql",
        database=db,
        query="SELECT name FROM items WHERE price > ? ORDER BY name",
        parameters=[100],
    )
    assert rows == [{"name": "ink"}, {"name": "pen"}]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone() == (3,)


# ------------------------------------------------------------------- notify
class FakeSMTP:
    sent: ClassVar[list] = []

    def __init__(self, host, port, timeout):
        self.calls = [("connect", host, port)]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        FakeSMTP.sent.append(self.calls)

    def starttls(self):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.calls.append(("send", msg["To"], msg["Subject"], msg.get_content().strip()))


def test_email(monkeypatch):
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    FakeSMTP.sent.clear()
    out = call(
        "email", to=["a@x.com", "b@x.com"], subject="Hi", body="Report", host="mail.x.com",
        port=587, username="bot", password="pw", tls=True,
    )  # fmt: skip
    assert out == {"sent_to": ["a@x.com", "b@x.com"], "subject": "Hi"}
    assert FakeSMTP.sent == [
        [
            ("connect", "mail.x.com", 587),
            ("starttls",),
            ("login", "bot", "pw"),
            ("send", "a@x.com, b@x.com", "Hi", "Report"),
        ]
    ]


def test_log(caplog):
    with caplog.at_level("INFO", logger="miniflow.flow"):
        out = call("log", message="3 expensive items", level="warning")
    assert out == {"level": "warning", "message": "3 expensive items"}
    assert caplog.records[-1].levelname == "WARNING"
    assert caplog.records[-1].getMessage() == "3 expensive items"


def test_wait_is_async():
    spec = get_block("wait")
    assert spec.kind == "async"
    assert asyncio.run(spec.fn(seconds=0.01)) == {"waited": 0.01}


def test_async_blocks_must_be_kind_async():
    with pytest.raises(BlockError, match="async"):

        @block("bad_async_test")
        async def bad():
            return None

    with pytest.raises(BlockError, match="async"):

        @block("bad_sync_test", kind="async")
        def bad_sync():
            return None
