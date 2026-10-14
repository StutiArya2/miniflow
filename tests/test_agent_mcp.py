"""MiniFlow's MCP server: JSON-RPC 2.0 over stdio."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from miniflow.agent import tools
from miniflow.agent.mcp import PROTOCOL_VERSIONS, McpServer
from miniflow.agent.owner import DashboardLink
from miniflow.agent.tools import Toolbox
from miniflow.cli import main
from miniflow.ui.owner import is_live, owner_path, read_owner
from tests.test_agent_owner import wait_until

HELLO = {"steps": {"hello": {"block": "log", "settings": {"message": "hi"}}}}


@pytest.fixture
def flows(tmp_path: Path) -> Path:
    d = tmp_path / "flows"
    d.mkdir()
    (d / "hello.json").write_text(json.dumps(HELLO), "utf-8")
    return d


@pytest.fixture
def server(flows, tmp_path) -> McpServer:
    return McpServer(Toolbox(flows, tmp_path / "s.db"))


def req(method: str, params: dict | None = None, id_: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": id_,
        "method": method,
        **({"params": params} if params is not None else {}),
    }


@pytest.mark.parametrize(
    ("asked", "expected"), [("2025-06-18", "2025-06-18"), ("1999-01-01", PROTOCOL_VERSIONS[0])]
)
def test_initialize_negotiates_the_version(server, asked, expected):
    reply = server.handle(
        req(
            "initialize",
            {
                "protocolVersion": asked,
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        )
    )
    result = reply["result"]
    assert reply["id"] == 1
    assert result["protocolVersion"] == expected
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "miniflow"
    assert "draft_block" in result["instructions"]


def test_notifications_get_no_reply(server):
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_ping(server):
    assert server.handle(req("ping")) == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_tools_list_matches_the_toolbox(server):
    listed = server.handle(req("tools/list"))["result"]["tools"]
    assert {t["name"] for t in listed} == set(server.box.tools)
    assert all(t["inputSchema"]["type"] == "object" for t in listed)


def test_a_tool_call_returns_structured_and_text_content(server):
    result = server.handle(req("tools/call", {"name": "get_flow", "arguments": {"name": "hello"}}))[
        "result"
    ]
    assert result["isError"] is False
    assert result["structuredContent"]["document"] == HELLO
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_a_tool_error_is_a_result_the_agent_can_read(server):
    result = server.handle(req("tools/call", {"name": "get_flow", "arguments": {}}))["result"]
    assert result["isError"] is True
    assert "name is required" in result["structuredContent"]["error"]


def test_an_unexpected_exception_is_reported_not_raised(server, monkeypatch):
    def boom(box, args):
        raise RuntimeError("kaput")

    monkeypatch.setitem(
        server.box.tools,
        "ping_me",
        tools.Tool(
            "ping_me", "test", {"type": "object", "properties": {}, "required": []}, "read", boom
        ),
    )
    result = server.handle(req("tools/call", {"name": "ping_me"}))["result"]
    assert result["isError"] is True
    assert "RuntimeError: kaput" in result["structuredContent"]["error"]


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ("not json", -32700),
        ("[]", -32600),
        ('{"jsonrpc": "1.0", "id": 1, "method": "ping"}', -32600),
        ('{"jsonrpc": "2.0", "id": true, "method": "ping"}', -32600),
        ('{"jsonrpc": "2.0", "id": 1, "method": "nope"}', -32601),
        ('{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope"}}', -32602),
        ('{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}', -32602),
    ],
)
def test_protocol_errors(server, line, code):
    assert server.handle_line(line)["error"]["code"] == code


def test_a_printing_plugin_cannot_corrupt_stdout(flows, tmp_path):
    name = "noisy_" + uuid.uuid4().hex[:8]
    (flows / "blocks").mkdir()
    (flows / "blocks" / f"{name}.py").write_text(
        f'from miniflow.blocks import block\nprint("noise from {name}")\n\n'
        f'@block("{name}")\ndef run():\n    return 1\n',
        "utf-8",
    )
    stdin = io.StringIO(
        json.dumps(req("tools/call", {"name": "list_blocks", "arguments": {}})) + "\n"
    )
    stdout = io.StringIO()
    McpServer(Toolbox(flows, tmp_path / "s.db")).serve(stdin, stdout)
    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1
    assert "noise" not in lines[0]
    assert name in {
        b["name"] for b in json.loads(lines[0])["result"]["structuredContent"]["blocks"]
    }


def test_a_real_subprocess_round_trip_in_utf8(flows, tmp_path):
    messages = [
        req(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
            1,
        ),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        req("tools/list", None, 2),
        req(
            "tools/call",
            {
                "name": "save_flow",
                "arguments": {
                    "name": "rupees",
                    "document": {"description": "₹ report", **HELLO},
                    "message": "₹",
                },
            },
            3,
        ),
        req("tools/call", {"name": "get_flow", "arguments": {"name": "rupees"}}, 4),
    ]
    stdin = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages).encode("utf-8")
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "miniflow",
            "mcp",
            "--flows",
            str(flows),
            "--db",
            str(tmp_path / "s.db"),
            "--no-runs",
        ],
        input=stdin,
        capture_output=True,
        timeout=50,
    )
    assert done.returncode == 0, done.stderr.decode(errors="replace")
    replies = [json.loads(line) for line in done.stdout.decode("utf-8").splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3, 4]
    assert "run_flow" not in {t["name"] for t in replies[1]["result"]["tools"]}
    assert replies[2]["result"]["isError"] is False
    assert replies[3]["result"]["structuredContent"]["document"]["description"] == "₹ report"


def test_mcp_needs_an_existing_flows_folder(tmp_path, capsys):

    assert main(["mcp", "--flows", str(tmp_path / "missing")]) == 2
    assert "doesn't exist" in capsys.readouterr().err


@pytest.mark.parametrize(
    "line",
    ["[" * 2000 + "0" + "]" * 2000, '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":NaN}}'],
)
def test_pathological_json_is_a_parse_error(server, line):
    assert server.handle_line(line)["error"]["code"] == -32700


def test_utf8_stdio_ignores_console_code_page(flows, tmp_path):
    done = subprocess.run(
        [sys.executable, "-m", "miniflow", "mcp", "--flows", str(flows), "--no-runs"],
        input=(
            json.dumps(
                req(
                    "tools/call",
                    {
                        "name": "save_flow",
                        "arguments": {
                            "name": "unicode",
                            "document": {"description": "₹ café", **HELLO},
                            "message": "₹",
                        },
                    },
                    1,
                ),
                ensure_ascii=False,
            )
            + "\n"
        ).encode(),
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        capture_output=True,
        timeout=15,
    )
    assert done.returncode == 0
    assert not json.loads(done.stdout.decode("utf-8"))["result"]["isError"]
    assert (
        json.loads((flows / "unicode.json").read_text(encoding="utf-8"))["description"] == "₹ café"
    )


def test_killed_mcp_leaves_its_dashboard_for_the_next_client(flows, tmp_path):
    db = tmp_path / "crash.db"
    command = [sys.executable, "-m", "miniflow", "mcp", "--flows", str(flows), "--db", str(db)]
    child = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    owner = None
    try:
        child.stdin.write(
            (
                json.dumps(req("tools/call", {"name": "run_flow", "arguments": {"name": "hello"}}))
                + "\n"
            ).encode()
        )
        child.stdin.flush()
        wait_until(lambda: read_owner(db) is not None, timeout=10)
        owner = read_owner(db)
        wait_until(lambda: is_live(owner), timeout=10)
        child.kill()
        child.communicate(timeout=10)
        second = DashboardLink(flows, db)
        try:
            assert second.url() == owner.url
        finally:
            second.close()
        assert is_live(read_owner(db))
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)
        if owner is None:
            owner = read_owner(db)
        if owner is not None and is_live(owner):
            os.kill(
                owner.pid, signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT
            )
            wait_until(lambda: not owner_path(db).exists(), timeout=15)


def test_nonfinite_tool_values_still_produce_valid_json(server, monkeypatch):
    monkeypatch.setitem(
        server.box.tools,
        "nonfinite",
        tools.Tool(
            "nonfinite",
            "test",
            {"type": "object", "properties": {}, "required": []},
            "read",
            lambda box, args: {"value": float("inf")},
        ),
    )
    result = server.handle(req("tools/call", {"name": "nonfinite"}))["result"]
    assert result["structuredContent"]["value"] == "Infinity"
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
