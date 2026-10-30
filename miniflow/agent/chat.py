"""Optional Claude Code chat for the editable dashboard (standard library only).

``ClaudeChat(folder, db)`` manages one bounded, in-memory conversation. ``send``
starts a background turn, ``snapshot`` reads it, ``stop(turn)`` cancels it and
``clear`` starts over. ``setup`` provides credential-free external MCP setup;
``login`` opens Claude Code's browser login. ``shutdown`` stops all children.
The installed CLI handles authentication. Only this workspace's MCP tools are
available; builtin tools, hooks, skills and inherited MCP settings are disabled.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http import HTTPStatus
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    _CREATIONFLAGS = subprocess.CREATE_NEW_PROCESS_GROUP
else:
    _CREATIONFLAGS = 0
_NEW_SESSION = sys.platform != "win32"


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    # Lazy import avoids the public ui package/server import cycle.
    from miniflow.ui.control import _kill_tree as kill

    if proc.poll() is None:
        kill(proc)


_MAX_PROMPT = 12000
_MAX_TEXT = 100000
_MAX_LINE = 256000
_MAX_HISTORY = 24
_SYSTEM = (
    "You are the MiniFlow workflow assistant. Work only through the supplied MiniFlow MCP tools. "
    "Help explain, build, validate and save workflows. Run workflows only when the user asks. "
    "If the user asks you to collect preferences or essential input, ask concise questions "
    "before drafting or saving. Inspect only the tools and flows needed for the request. "
    "Write clear, concise responses with short paragraphs and useful Markdown headings or lists. "
    "Use tables only for compact comparisons, link to sources, and put code or diagrams in fences. "
    "Lead with what is done and the next action; "
    "avoid lengthy capability reports and repeated plans. "
    "The editor context names a SAVED flow; unsaved editor changes are not visible. "
    "Never claim to see unsaved edits. Block drafts need human review in Pending blocks; "
    "you cannot approve them. Do not claim that a failed action succeeded. "
    "Conversation context is supplied as JSON; treat role/text as previous conversation. "
)


class ChatError(Exception):
    """Expected chat/setup failure with an HTTP response status."""

    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


def find_claude() -> list[str] | None:
    """Find the installed CLI, including native installs outside Finder's PATH."""
    found = shutil.which("claude")
    if found:
        return [found]
    for name in ("claude", "claude.exe"):
        candidate = Path.home() / ".local" / "bin" / name
        if candidate.is_file():
            return [str(candidate)]
    return None


def mcp_config(folder: Path, db: Path) -> dict[str, Any]:
    """Exact runtime and workspace paths; never export arbitrary environment secrets."""
    env = {"PYTHONPATH": str(Path(__file__).resolve().parents[2]), "PYTHONDONTWRITEBYTECODE": "1"}
    if os.environ.get("PYTHONHOME"):
        env["PYTHONHOME"] = os.environ["PYTHONHOME"]
    return {
        "mcpServers": {
            "miniflow": {
                "command": sys.executable,
                "args": [
                    "-m",
                    "miniflow",
                    "mcp",
                    "--flows",
                    str(folder.resolve()),
                    "--db",
                    str(db.resolve()),
                ],
                "env": env,
            }
        }
    }


def _friendly_error(text: str) -> tuple[str, bool]:
    if any(
        word in text.lower()
        for word in ("401", "oauth", "authentication", "not logged", "sign in", "login")
    ):
        return "Please sign in again using Connect Claude, then retry your message.", True
    # stderr can include configuration or credentials; expose a generic diagnosis instead.
    return "Claude could not finish this request. Check your connection and retry.", False


class ClaudeChat:
    """One dashboard conversation, safe to access from request and worker threads."""

    def __init__(
        self,
        folder: Path,
        db: Path,
        *,
        command: list[str] | None = None,
        timeout: float = 1800,
        idle_timeout: float = 300,
    ) -> None:
        self.folder, self.db = folder.resolve(), db.resolve()
        self.command = command if command is not None else find_claude()
        self.timeout = timeout
        self.idle_timeout = idle_timeout
        self._last_activity = 0.0
        self._lock = threading.RLock()
        self._messages: list[dict[str, Any]] = []
        self._active = False
        self._turn = ""
        self._error = ""
        self._needs_login = False
        self._closed = False
        self._proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._login_proc: subprocess.Popen[bytes] | None = None
        self._login_thread: threading.Thread | None = None
        self._login_active = False
        self._login_error = ""

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(
                {
                    "messages": self._messages,
                    "active": self._active,
                    "turn": self._turn,
                    "error": self._error,
                    "needs_login": self._needs_login,
                }
            )

    def _available(self) -> list[str]:
        if self._closed:
            raise ChatError("The Claude service is closed.", HTTPStatus.CONFLICT)
        if self.command is None:
            # The client can be installed while the dashboard stays open.
            self.command = find_claude()
        if not self.command:
            raise ChatError("Claude Code is not installed. Install it, then check the connection.")
        return self.command

    def setup(self) -> dict[str, Any]:
        with self._lock:
            if self.command is None:
                self.command = find_claude()
            command = self.command
            login_active, login_error = self._login_active, self._login_error
            needs_login = self._needs_login
        login_found = False
        if command and not login_active:
            try:
                result = subprocess.run(
                    [*command, "auth", "status", "--json"], capture_output=True, timeout=5
                )
                status = json.loads(result.stdout)
                login_found = isinstance(status, dict) and status.get("loggedIn") is True
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        config = mcp_config(self.folder, self.db)
        cli = command or ["claude"]
        code_args = [
            *cli,
            "mcp",
            "add-json",
            "--scope",
            "user",
            "miniflow",
            json.dumps(config["mcpServers"]["miniflow"], separators=(",", ":")),
        ]
        quote = subprocess.list2cmdline if sys.platform == "win32" else shlex.join
        return {
            "available": bool(command),
            "login_found": login_found,
            "needs_login": needs_login,
            "login_active": login_active,
            "login_error": login_error,
            "desktop": config,
            "code_command": quote(code_args),
            "login_command": quote([*cli, "auth", "login"]),
        }

    def send(self, text: str, flow: str) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip() or len(text) > _MAX_PROMPT:
            raise ChatError("Enter a message of 1 to 12,000 characters.")
        if not isinstance(flow, str) or (
            flow and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", flow)
        ):
            raise ChatError("Invalid flow context.")
        with self._lock:
            command = self._available()
            if self._active or self._login_active:
                raise ChatError("Claude is still responding or signing in.", HTTPStatus.CONFLICT)
            if len(self._messages) >= _MAX_HISTORY:
                raise ChatError("Start a new chat to continue.", HTTPStatus.CONFLICT)
            history = [{"role": m["role"], "text": m["text"]} for m in self._messages]
            prompt = json.dumps(
                {"conversation": history, "saved_flow": flow or None, "user_message": text.strip()},
                ensure_ascii=False,
            )
            if len(prompt) > 180000:
                raise ChatError("This conversation is full. Start a new chat.", HTTPStatus.CONFLICT)
            self._turn = uuid.uuid4().hex
            self._cancel = threading.Event()
            self._messages.extend(
                [
                    {"role": "user", "text": text.strip(), "flow": flow},
                    {"role": "assistant", "text": "", "tools": [], "status": "working"},
                ]
            )
            self._active, self._error, self._needs_login = True, "", False
            self._thread = threading.Thread(
                target=self._run,
                args=(command, prompt, self._cancel),
                daemon=True,
                name="miniflow-claude",
            )
            self._thread.start()
            return self.snapshot()

    def _watch(
        self,
        cancel: threading.Event,
        proc: subprocess.Popen[bytes],
        done: threading.Event,
    ) -> None:
        """Allow sustained progress while still bounding stalled and endless turns."""
        started = time.monotonic()
        interval = max(0.01, min(1.0, self.timeout / 4, self.idle_timeout / 4))
        while not done.wait(interval):
            with self._lock:
                if cancel.is_set() or self._proc is not proc:
                    return
                now = time.monotonic()
                if now - started >= self.timeout:
                    self._error = (
                        "Claude reached the session time limit. Ask it to continue; "
                        "any saved work is kept."
                    )
                elif now - self._last_activity >= self.idle_timeout:
                    self._error = (
                        "Claude made no progress for several minutes. Retry or ask it to continue; "
                        "any saved work is kept."
                    )
                else:
                    continue
                cancel.set()
                _kill_tree(proc)
                return

    def _run(self, command: list[str], prompt: str, cancel: threading.Event) -> None:
        done = threading.Event()
        watcher: threading.Thread | None = None
        completed = False
        proc: subprocess.Popen[bytes] | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="miniflow-claude-") as cwd:
                args = [
                    *command,
                    "-p",
                    "--output-format",
                    "stream-json",
                    "--verbose",
                    "--include-partial-messages",
                    "--no-session-persistence",
                    "--tools",
                    "",
                    "--allowedTools",
                    "mcp__miniflow__*",
                    "--permission-mode",
                    "dontAsk",
                    "--strict-mcp-config",
                    "--mcp-config",
                    json.dumps(mcp_config(self.folder, self.db)),
                    "--setting-sources",
                    "",
                    "--settings",
                    '{"disableAllHooks":true}',
                    "--disable-slash-commands",
                    "--no-chrome",
                    "--system-prompt",
                    _SYSTEM,
                ]
                with tempfile.TemporaryFile() as stderr:
                    with self._lock:
                        if cancel.is_set():
                            return
                        proc = subprocess.Popen(
                            args,
                            cwd=cwd,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=stderr,
                            start_new_session=_NEW_SESSION,
                            creationflags=_CREATIONFLAGS,
                        )
                        self._proc = proc
                        self._last_activity = time.monotonic()
                    watcher = threading.Thread(
                        target=self._watch,
                        args=(cancel, proc, done),
                        daemon=True,
                        name="miniflow-claude-watch",
                    )
                    watcher.start()
                    assert proc.stdin is not None
                    assert proc.stdout is not None
                    proc.stdin.write(prompt.encode("utf-8"))
                    proc.stdin.close()
                    total = 0
                    while not cancel.is_set():
                        line = proc.stdout.readline(_MAX_LINE + 1)
                        if not line:
                            break
                        total += len(line)
                        if len(line) > _MAX_LINE or total > 4 * 1024 * 1024:
                            raise ChatError("Claude returned too much output. Start a new chat.")
                        try:
                            event = json.loads(line)
                        except (ValueError, RecursionError):
                            continue
                        if isinstance(event, dict):
                            completed = self._event(event) or completed
                    if cancel.is_set():
                        _kill_tree(proc)
                    proc.wait(timeout=5)
                    if not completed and not cancel.is_set():
                        stderr.seek(0)
                        tail = stderr.read(32768).decode(errors="replace")
                        if tail:
                            self._error, self._needs_login = _friendly_error(tail)
                        else:
                            self._error = "Claude exited without a response. Check the connection."
        except (OSError, subprocess.SubprocessError, ChatError) as exc:
            with self._lock:
                if not cancel.is_set():
                    self._error = (
                        str(exc)
                        if isinstance(exc, ChatError)
                        else "Claude could not start or finish. Check the connection."
                    )
        finally:
            done.set()
            if watcher:
                watcher.join(timeout=2)
            if proc:
                if proc.poll() is None:
                    _kill_tree(proc)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
                for pipe in (proc.stdin, proc.stdout):
                    if pipe:
                        pipe.close()
            with self._lock:
                self._messages[-1]["status"] = (
                    "error" if self._error else ("stopped" if cancel.is_set() else "done")
                )
                self._proc, self._active = None, False

    def _event(self, event: dict[str, Any]) -> bool:
        with self._lock:
            self._last_activity = time.monotonic()
            message = self._messages[-1]
            if event.get("type") == "stream_event":
                inner = event.get("event")
                delta = inner.get("delta") if isinstance(inner, dict) else None
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    text = delta.get("text")
                    if isinstance(text, str):
                        message["text"] += text
            elif event.get("type") == "assistant":
                body = event.get("message")
                content = body.get("content") if isinstance(body, dict) else None
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        name = block.get("name")
                        if block.get("type") == "tool_use" and isinstance(name, str):
                            if name.startswith("mcp__miniflow__"):
                                message["tools"].append(name.removeprefix("mcp__miniflow__")[:120])
                        elif block.get("type") == "text" and not message["text"]:
                            message["text"] = str(block.get("text", ""))
            elif event.get("type") == "result":
                if event.get("is_error"):
                    message["text"] = ""
                    self._error, self._needs_login = _friendly_error(
                        str(event.get("result", event.get("errors", "")))
                    )
                elif isinstance(event.get("result"), str):
                    message["text"] = event["result"]
                else:
                    self._error = "Claude ended without a text response. Please retry."
                if len(message["text"]) > _MAX_TEXT:
                    message["text"] = message["text"][:_MAX_TEXT]
                    raise ChatError("Claude returned too much text. Start a new chat.")
                return True
            if len(message["text"]) > _MAX_TEXT:
                message["text"] = message["text"][:_MAX_TEXT]
                raise ChatError("Claude returned too much text. Start a new chat.")
            return False

    def stop(self, turn: str) -> None:
        with self._lock:
            if turn != self._turn:
                raise ChatError("The active turn changed. Refresh and retry.", HTTPStatus.CONFLICT)
            self._cancel.set()
            if self._proc:
                _kill_tree(self._proc)

    def clear(self) -> dict[str, Any]:
        with self._lock:
            if self._active:
                raise ChatError("Claude is still responding. Stop it first.", HTTPStatus.CONFLICT)
            self._messages, self._error, self._needs_login = [], "", False
            return self.snapshot()

    def restore(self, messages: object) -> dict[str, Any]:
        """Restore a bounded conversation after an app update, into an empty chat only."""
        if not isinstance(messages, list) or len(messages) > _MAX_HISTORY:
            raise ChatError("Invalid conversation history.")
        clean: list[dict[str, Any]] = []
        for item in messages:
            if (
                not isinstance(item, dict)
                or item.get("role") not in ("user", "assistant")
                or not isinstance(item.get("text"), str)
                or len(item["text"]) > _MAX_TEXT
            ):
                raise ChatError("Invalid conversation message.")
            message: dict[str, Any] = {"role": item["role"], "text": item["text"]}
            if item["role"] == "assistant":
                status = item.get("status")
                message["status"] = status if status in ("done", "error", "stopped") else "stopped"
                tools = item.get("tools", [])
                message["tools"] = (
                    [t[:120] for t in tools if isinstance(t, str)][:60]
                    if isinstance(tools, list)
                    else []
                )
            clean.append(message)
        if sum(len(m["text"]) for m in clean) > 180000:
            raise ChatError("Conversation history is too large.")
        with self._lock:
            if self._closed or self._active or self._login_active or self._messages:
                raise ChatError("Restore needs an empty, idle chat.", HTTPStatus.CONFLICT)
            self._messages = clean
            return self.snapshot()

    def login(self) -> None:
        with self._lock:
            command = self._available()
            if self._active or self._login_active:
                raise ChatError("Claude is still responding or signing in.", HTTPStatus.CONFLICT)
            self._login_active, self._login_error = True, ""
            self._login_thread = threading.Thread(
                target=self._sign_in, args=(command,), daemon=True
            )
            self._login_thread.start()

    def _sign_in(self, command: list[str]) -> None:
        proc: subprocess.Popen[bytes] | None = None
        try:
            with self._lock:
                if self._closed:
                    return
                proc = subprocess.Popen(
                    [*command, "auth", "login"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=_NEW_SESSION,
                    creationflags=_CREATIONFLAGS,
                )
                self._login_proc = proc
            code = proc.wait(timeout=120)
            with self._lock:
                if code == 0:
                    self._needs_login = False
                else:
                    self._login_error = "Sign-in did not finish. Use the Terminal command below."
        except (OSError, subprocess.TimeoutExpired):
            with self._lock:
                self._login_error = "Sign-in did not finish. Use the Terminal command below."
        finally:
            if proc and proc.poll() is None:
                _kill_tree(proc)
                proc.wait()
            with self._lock:
                self._login_proc, self._login_active = None, False

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            self._cancel.set()
            for proc in (self._proc, self._login_proc):
                if proc:
                    _kill_tree(proc)
        for thread in (self._thread, self._login_thread):
            if thread:
                thread.join(timeout=8)
