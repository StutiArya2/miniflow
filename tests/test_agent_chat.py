"""Claude chat uses a local client with scoped MCP tools and bounded output."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from miniflow.agent.chat import ChatError, ClaudeChat, mcp_config


def client(tmp_path: Path, script: str) -> list[str]:
    path = tmp_path / "fake_claude.py"
    path.write_text(script, encoding="utf-8")
    return [sys.executable, str(path)]


def wait(chat: ClaudeChat) -> dict:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        snapshot = chat.snapshot()
        if not snapshot["active"]:
            return snapshot
        time.sleep(0.01)
    pytest.fail("chat did not finish")


def test_stream_and_followup_context(tmp_path: Path):
    cmd = client(
        tmp_path,
        """import sys,json
args=sys.argv[1:]
assert args[args.index('--tools')+1] == ''
assert '--strict-mcp-config' in args
assert args[args.index('--permission-mode')+1] == 'dontAsk'
assert args[args.index('--setting-sources')+1] == ''
config=json.loads(args[args.index('--mcp-config')+1])
assert list(config['mcpServers']) == ['miniflow']
prompt=sys.stdin.read()
print(json.dumps({'type':'stream_event','event':{'type':'content_block_delta',
    'delta':{'type':'text_delta','text':'Working'}}}), flush=True)
print(json.dumps({'type':'assistant','message':{'content':
    [{'type':'tool_use','name':'mcp__miniflow__list_blocks'}]}}), flush=True)
print(json.dumps({'type':'result','is_error':False,
    'result':'Followup' if 'First response' in prompt else 'First response'}), flush=True)
""",
    )
    chat = ClaudeChat(tmp_path, tmp_path / "runs.db", command=cmd)
    try:
        chat.send("Help me", "hello_world")
        result = wait(chat)
        assert result["messages"][-1]["text"] == "First response"
        assert result["messages"][-1]["tools"] == ["list_blocks"]
        chat.send("Continue", "")
        assert wait(chat)["messages"][-1]["text"] == "Followup"
        chat.clear()
        assert not chat.snapshot()["messages"]
    finally:
        chat.shutdown()


def test_expired_login_becomes_reconnect_message(tmp_path: Path):
    cmd = client(
        tmp_path,
        "import json; print(json.dumps({'type':'result','is_error':True,"
        "'result':'401 OAuth token expired'}))",
    )
    chat = ClaudeChat(tmp_path, tmp_path / "runs.db", command=cmd)
    chat.send("Hello", "")
    result = wait(chat)
    assert result["needs_login"]
    assert "sign in" in result["error"].lower()
    chat.shutdown()


def test_busy_stop_and_shutdown(tmp_path: Path):
    cmd = client(tmp_path, "import time; time.sleep(30)")
    chat = ClaudeChat(tmp_path, tmp_path / "runs.db", command=cmd)
    turn = chat.send("Hello", "")["turn"]
    with pytest.raises(ChatError, match="still responding"):
        chat.send("Again", "")
    with pytest.raises(ChatError, match="still responding"):
        chat.clear()
    with pytest.raises(ChatError, match="changed"):
        chat.stop("other-turn")
    chat.stop(turn)
    assert wait(chat)["messages"][-1]["status"] == "stopped"
    chat.send("Again", "")
    chat.shutdown()
    assert not chat.snapshot()["active"]
    with pytest.raises(ChatError, match="closed"):
        chat.send("Again", "")


def test_no_result_and_invalid_input(tmp_path: Path):
    chat = ClaudeChat(tmp_path, tmp_path / "runs.db", command=client(tmp_path, "print('invalid')"))
    for value in ("", " " * 3, "x" * 12001):
        with pytest.raises(ChatError):
            chat.send(value, "")
    with pytest.raises(ChatError):
        chat.send("Hello", "../invalid")
    chat.send("Hello", "")
    assert "without a response" in wait(chat)["error"]
    chat.shutdown()


def test_output_limit_and_timeout(tmp_path: Path):
    cmd = client(tmp_path, "import json; print(json.dumps({'type':'result','result':'x'*100001}))")
    chat = ClaudeChat(tmp_path, tmp_path / "runs.db", command=cmd)
    chat.send("Hello", "")
    assert wait(chat)["error"]
    chat.shutdown()
    chat = ClaudeChat(
        tmp_path,
        tmp_path / "runs.db",
        command=client(tmp_path, "import time; time.sleep(30)"),
        timeout=0.1,
    )
    chat.send("Hello", "")
    assert "session time limit" in wait(chat)["error"]
    chat.shutdown()


def test_missing_client_and_safe_setup(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("miniflow.agent.chat.find_claude", lambda: None)
    chat = ClaudeChat(tmp_path, tmp_path / "runs.db")
    setup = chat.setup()
    assert not setup["available"]
    assert setup["desktop"]["mcpServers"]["miniflow"]["command"] == sys.executable
    with pytest.raises(ChatError, match="installed"):
        chat.send("Hello", "")
    chat.shutdown()


def test_setup_does_not_expose_account_or_credentials(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-secret")
    cmd = client(
        tmp_path,
        "import json; print(json.dumps({'loggedIn':True,'email':'private@example.com',"
        "'token':'secret'}))",
    )
    chat = ClaudeChat(tmp_path, tmp_path / "runs.db", command=cmd)
    setup = chat.setup()
    assert setup["login_found"]
    assert "private@example.com" not in json.dumps(setup)
    assert "secret" not in json.dumps(setup)
    chat.shutdown()


def test_mcp_config_preserves_bundled_runtime_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PYTHONHOME", "/bundle/python")
    monkeypatch.setenv("PRIVATE_TOKEN", "secret")
    cfg = mcp_config(tmp_path, tmp_path / "runs.db")["mcpServers"]["miniflow"]
    assert cfg["env"]["PYTHONHOME"] == "/bundle/python"
    assert "PRIVATE_TOKEN" not in cfg["env"]
    assert cfg["args"][:3] == ["-m", "miniflow", "mcp"]
    assert str(tmp_path.resolve()) in cfg["args"]


def test_signin_success_failure_and_shutdown(tmp_path: Path):
    cmd = client(tmp_path, "import sys; sys.exit(0)")
    chat = ClaudeChat(tmp_path, tmp_path / "runs.db", command=cmd)
    chat.login()
    deadline = time.monotonic() + 3
    while chat.setup()["login_active"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not chat.setup()["login_active"]
    assert not chat.setup()["login_error"]
    chat.shutdown()
    chat = ClaudeChat(
        tmp_path, tmp_path / "runs.db", command=client(tmp_path, "import sys; sys.exit(1)")
    )
    chat.login()
    deadline = time.monotonic() + 3
    while chat.setup()["login_active"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "Terminal command" in chat.setup()["login_error"]
    chat.shutdown()
    chat = ClaudeChat(
        tmp_path, tmp_path / "runs.db", command=client(tmp_path, "import time; time.sleep(30)")
    )
    chat.login()
    with pytest.raises(ChatError, match="signing in"):
        chat.send("Hello", "")
    chat.shutdown()
    assert not chat.setup()["login_active"]


def test_client_launch_failure_is_friendly(tmp_path: Path):
    chat = ClaudeChat(tmp_path, tmp_path / "runs.db", command=[str(tmp_path / "absent")])
    chat.send("Hello", "")
    assert "could not start" in wait(chat)["error"]
    chat.shutdown()


def test_active_stream_resets_idle_deadline(tmp_path: Path):
    cmd = client(
        tmp_path,
        """import json,time
for i in range(20):
    print(json.dumps({'type':'stream_event','event':{'type':'content_block_delta',
        'delta':{'type':'thinking_delta','thinking':'working'}}}), flush=True)
    time.sleep(.1)
print(json.dumps({'type':'result','result':'Finished'}), flush=True)
""",
    )
    chat = ClaudeChat(tmp_path, tmp_path / "runs.db", command=cmd, timeout=5, idle_timeout=1.5)
    try:
        chat.send("Build a full workflow", "")
        result = wait(chat)
        assert not result["error"]
        assert result["messages"][-1]["text"] == "Finished"
    finally:
        chat.shutdown()


def test_idle_deadline_stops_a_stalled_client(tmp_path: Path):
    chat = ClaudeChat(
        tmp_path,
        tmp_path / "runs.db",
        command=client(tmp_path, "import time; time.sleep(30)"),
        timeout=5,
        idle_timeout=0.1,
    )
    try:
        chat.send("Hello", "")
        assert "no progress" in wait(chat)["error"]
    finally:
        chat.shutdown()


def test_total_deadline_applies_even_with_progress(tmp_path: Path):
    cmd = client(
        tmp_path,
        """import json,time
for i in range(100):
    print(json.dumps({'type':'stream_event','event':{'type':'content_block_delta',
        'delta':{'type':'text_delta','text':'working '}}}), flush=True)
    time.sleep(.05)
""",
    )
    chat = ClaudeChat(tmp_path, tmp_path / "runs.db", command=cmd, timeout=0.5, idle_timeout=5)
    try:
        chat.send("Hello", "")
        assert "session time limit" in wait(chat)["error"]
    finally:
        chat.shutdown()
