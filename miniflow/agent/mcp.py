"""MiniFlow as an MCP server over stdio (standard library only).

``miniflow mcp --flows DIR`` reads JSON-RPC 2.0 messages from stdin, one per line, and
writes one reply per line to stdout, following the Model Context Protocol's stdio
transport. It answers ``initialize``, ``ping``, ``tools/list`` and ``tools/call``, and
ignores notifications. The tools are :mod:`miniflow.agent.tools`.

    McpServer(box)
    .handle(message) -> dict | None     one decoded message in; its reply, or None for a
                                        notification
    .handle_line(line) -> dict | None   the same from one line of text, parse errors included
    .serve(stdin, stdout) -> None       until stdin closes

A tool's expected failure is a normal result with ``isError: true`` so the agent can read
it and correct itself; malformed requests get JSON-RPC errors. While serving,
``sys.stdout`` points at stderr, so a block that prints can't corrupt the stream.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, TextIO

from miniflow import __version__
from miniflow.agent.tools import Toolbox, ToolError, UnknownTool, call

__all__ = ["INSTRUCTIONS", "PROTOCOL_VERSIONS", "McpServer"]

log = logging.getLogger("miniflow.agent")

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "MiniFlow runs workflows made of blocks. To build a flow: find blocks with list_blocks or "
    "describe_block, write the flow document, call validate_flow until it reports no problems, "
    "then save_flow. Before changing a flow, read it with get_flow and pass its version as "
    "base_version. run_flow returns at once; check the run later with get_run. If no block "
    "does what is needed, propose one with draft_block; a person must approve it in the "
    "Editor before any flow can use it."
)
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602


def _reject_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON number {value}")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _result(id_: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _content(payload: dict[str, Any], is_error: bool) -> dict[str, Any]:
    # Python values such as Infinity aren't JSON numbers; expose their spelling as text.
    payload = json.loads(_dumps(payload), parse_constant=str)
    return {
        "content": [{"type": "text", "text": _dumps(payload)}],
        "structuredContent": payload,
        "isError": is_error,
    }


class McpServer:
    def __init__(self, box: Toolbox) -> None:
        self.box = box

    def handle_line(self, line: str) -> dict[str, Any] | None:
        try:
            message = json.loads(line, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            return _error(None, PARSE_ERROR, "not valid JSON")
        return self.handle(message)

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return _error(
                None,
                INVALID_REQUEST,
                "expected one JSON-RPC 2.0 message (batches aren't supported)",
            )
        id_ = message.get("id")
        if "method" not in message and ("result" in message or "error" in message):
            return None  # a reply to a request this server never sends
        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _error(
                id_ if isinstance(id_, str | int) else None,
                INVALID_REQUEST,
                "expected a JSON-RPC 2.0 request",
            )
        if "id" not in message:
            return None  # a notification: initialized, cancelled, ...
        if isinstance(id_, bool) or not isinstance(id_, str | int):
            return _error(None, INVALID_REQUEST, "id must be a string or a number")
        params = message.get("params", {})
        if not isinstance(params, dict):
            return _error(id_, INVALID_PARAMS, "params must be an object")
        method = message["method"]
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(
                id_,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "miniflow", "version": __version__},
                    "instructions": INSTRUCTIONS,
                },
            )
        if method == "ping":
            return _result(id_, {})
        if method == "tools/list":
            tools = [
                {"name": t.name, "description": t.description, "inputSchema": t.schema}
                for t in self.box.tools.values()
            ]
            return _result(id_, {"tools": tools})
        if method == "tools/call":
            return self._call(id_, params)
        return _error(id_, METHOD_NOT_FOUND, f"unknown method {method!r}")

    def _call(self, id_: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            return _error(id_, INVALID_PARAMS, "name must be a string")
        try:
            result = call(self.box, name, params.get("arguments", {}))
            return _result(id_, _content(result, False))
        except UnknownTool:
            return _error(id_, INVALID_PARAMS, f"unknown tool {name!r}")
        except ToolError as exc:
            payload: dict[str, Any] = {"error": str(exc)}
            if exc.data:
                payload["details"] = exc.data
            return _result(id_, _content(payload, True))
        except Exception as exc:
            log.exception("tool %s failed", name)
            return _result(
                id_,
                _content({"error": f"internal error in {name}: {type(exc).__name__}: {exc}"}, True),
            )

    def serve(self, stdin: TextIO, stdout: TextIO) -> None:
        real_stdout = sys.stdout
        sys.stdout = sys.stderr  # print() from any block goes to stderr, not into the protocol
        try:
            for line in stdin:
                if not line.strip():
                    continue
                reply = self.handle_line(line)
                if reply is not None:
                    stdout.write(_dumps(reply) + "\n")
                    stdout.flush()
        finally:
            sys.stdout = real_stdout
