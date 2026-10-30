"""Private chat routes are available only on an editable, runnable dashboard."""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from urllib.parse import urlsplit

from miniflow.ui import DashboardServer


def request(server, method, path, body=None, **headers):
    url = urlsplit(server.url)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
    data = None if body is None else json.dumps(body).encode()
    try:
        conn.request(method, path, data, headers={"Content-Type": "application/json", **headers})
        res = conn.getresponse()
        return res.status, json.loads(res.read())
    finally:
        conn.close()


def test_chat_routes_and_guards(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("miniflow.agent.chat.find_claude", lambda: None)
    with DashboardServer(tmp_path / "runs.db", port=0, flows=tmp_path, allow_run=True) as server:
        assert request(server, "GET", "/api/agent/setup")[1]["available"] is False
        assert request(server, "GET", "/api/agent/chat")[1]["messages"] == []
        for path in ("/api/agent/setup", "/api/agent/chat"):
            assert request(server, "GET", path, Host="evil.test")[0] == 403
            assert request(server, "GET", path, Origin="http://evil.test")[0] == 403
        for action in ("send", "stop", "clear", "login", "restore"):
            path = "/api/agent/" + action
            assert request(server, "POST", path, {}, Origin="http://evil.test")[0] == 403
        assert request(server, "POST", "/api/agent/send", {"message": "Hello"})[0] == 400
        assert request(server, "POST", "/api/agent/send", {"message": False})[0] == 400
        assert (
            request(server, "POST", "/api/agent/send", {"message": "Hello", "flow": []})[0] == 400
        )
        assert request(server, "POST", "/api/agent/send", {"message": "x" * 70000})[0] == 413
        assert request(server, "POST", "/api/agent/login", {})[0] == 400
        assert request(server, "POST", "/api/agent/clear", {})[0] == 200
        assert (
            request(
                server,
                "POST",
                "/api/agent/restore",
                {"messages": [{"role": "user", "text": "Saved message"}]},
            )[0]
            == 200
        )
        assert (
            request(server, "GET", "/api/agent/chat")[1]["messages"][0]["text"] == "Saved message"
        )
        assert request(server, "POST", "/api/agent/unknown", {})[0] == 404
        assert request(server, "GET", "/api/agent/unknown")[0] == 404


def test_no_agent_on_readonly_dashboard(tmp_path: Path):
    for flows in (None, tmp_path):
        with DashboardServer(tmp_path / "runs.db", port=0, flows=flows) as server:
            assert request(server, "GET", "/api/agent/chat")[0] == 404
            assert request(server, "GET", "/api/agent/setup")[0] == 404
            assert request(server, "POST", "/api/agent/send", {"message": "Hello"})[0] == 404
