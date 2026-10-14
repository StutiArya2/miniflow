"""The tools an agent uses to build and run MiniFlow flows (standard library only).

Each tool is a function ``fn(box, args) -> dict`` registered with :func:`tool`: a name, a
description written for the agent, a JSON Schema for its arguments, and a group: ``read``
(changes nothing), ``write`` (flows and block drafts) or ``run`` (starts, cancels and reads
runs). :mod:`miniflow.agent.mcp` serves them over MCP; an Editor panel can call the same list.

    Toolbox(flows, db, *, read_only=False, runs=None)   what the tools work on
    available(*, read_only, runs) -> [Tool]             read only: no write or run tools;
                                                        runs=False: no run tools
    call(box, name, args) -> dict                       UnknownTool, or ToolError for an
                                                        expected failure (bad input, invalid
                                                        flow, conflict, missing flow)

``runs`` is a :class:`RunLink` (normally :class:`miniflow.agent.owner.DashboardLink`): run
tools never start runs in this process, so the dashboard that owns the database keeps one
queue. A :class:`ToolError` has a message for the agent and optional ``data``, for example
the current document after a conflicting save. Every save an agent makes is labelled
``(agent)`` in the flow's history. Environment values are never returned.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from miniflow.agent.drafts import DraftError, list_drafts, write_draft
from miniflow.agent.owner import LinkError
from miniflow.blocks import BlockError, Setting, get_block
from miniflow.flowfile import FlowFileError
from miniflow.store import StateStore
from miniflow.ui.author import FlowWorkspace, VersionConflict, WorkspaceError, block_catalog

__all__ = [
    "TOOLS",
    "RunLink",
    "Tool",
    "ToolError",
    "Toolbox",
    "UnknownTool",
    "available",
    "call",
    "tool",
]

Group = Literal["read", "write", "run"]


class ToolError(Exception):
    def __init__(self, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.data = data


class UnknownTool(LookupError):
    pass


class RunLink(Protocol):
    def url(self) -> str: ...
    def run(self, flow: str, environment: str | None) -> dict[str, Any]: ...
    def cancel(self, run_id: str) -> dict[str, Any]: ...
    def control(self, *, start: bool = True) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    group: Group
    fn: Callable[[Toolbox, dict[str, Any]], dict[str, Any]]


TOOLS: dict[str, Tool] = {}


def tool(
    name: str,
    description: str,
    properties: dict[str, dict[str, Any]] | None = None,
    required: Iterable[str] = (),
    group: Group = "read",
) -> Callable[
    [Callable[[Toolbox, dict[str, Any]], dict[str, Any]]],
    Callable[[Toolbox, dict[str, Any]], dict[str, Any]],
]:
    schema = {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
    }

    def register(
        fn: Callable[[Toolbox, dict[str, Any]], dict[str, Any]],
    ) -> Callable[[Toolbox, dict[str, Any]], dict[str, Any]]:
        TOOLS[name] = Tool(name, description, schema, group, fn)
        return fn

    return register


def available(*, read_only: bool, runs: bool) -> list[Tool]:
    def shown(t: Tool) -> bool:
        if t.group == "read":
            return True
        if read_only:
            return False
        return t.group == "write" or runs

    return [t for t in TOOLS.values() if shown(t)]


class Toolbox:
    def __init__(
        self,
        flows: str | Path,
        db: str | Path,
        *,
        read_only: bool = False,
        runs: RunLink | None = None,
    ) -> None:
        self.workspace = FlowWorkspace(flows)
        self.folder = self.workspace.folder
        self.db = Path(db).resolve()
        self.runs = runs
        self.tools = {t.name: t for t in available(read_only=read_only, runs=runs is not None)}


_JSON_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _check_args(t: Tool, args: object) -> dict[str, Any]:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    props: dict[str, dict[str, Any]] = t.schema["properties"]
    unknown = sorted(set(args) - set(props))
    if unknown:
        raise ToolError(f"unknown argument(s): {', '.join(map(str, unknown))}")
    for key in t.schema["required"]:
        if key not in args:
            raise ToolError(f"{key} is required")
    for key, value in args.items():
        kind = props[key]["type"]
        want = _JSON_TYPES[kind]
        if not isinstance(value, want) or (want is int and isinstance(value, bool)):
            raise ToolError(f"{key} must be {kind}")
    return args


def call(box: Toolbox, name: str, args: object) -> dict[str, Any]:
    t = box.tools.get(name)
    if t is None:
        raise UnknownTool(name)
    checked = _check_args(t, args)
    try:
        return t.fn(box, checked)
    except ToolError:
        raise
    except FileNotFoundError:
        raise ToolError(f"there is no flow called {checked.get('name')!r}") from None
    except DraftError as exc:
        raise ToolError(str(exc), exc.report) from None
    except FlowFileError as exc:
        raise ToolError("; ".join(exc.problems), {"problems": exc.problems}) from None
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"the flow file isn't valid JSON (line {exc.lineno}, column {exc.colno})"
        ) from None
    except (WorkspaceError, BlockError, LinkError) as exc:
        raise ToolError(str(exc)) from None


# ------------------------------------------------------------------ helpers
_NAME = {
    "type": "string",
    "description": "flow name: letters, digits, _ or -, starting with a letter",
}
_DOCUMENT = {
    "type": "object",
    "description": "the whole JSON flow document, as get_flow returns it",
}
_PLACEHOLDERS: dict[type, Any] = {str: "text", int: 0, float: 0, bool: False, list: [], dict: {}}


def _placeholder(s: Setting) -> Any:
    if s.secret:
        return "{{ env.NAME }}"
    if s.choices:
        return s.choices[0]
    first = s.type[0] if isinstance(s.type, tuple) else s.type
    return _PLACEHOLDERS.get(first, "value")


def _flow(box: Toolbox, name: str) -> dict[str, Any]:
    for _ in range(3):
        before = box.workspace.file_version(name)
        document = box.workspace.read(name)
        after = box.workspace.file_version(name)
        if before == after:
            return {"name": name, "document": document, "version": after}
    raise ToolError("the flow keeps changing; read it again")


# --------------------------------------------------------------------- read
@tool(
    "list_blocks",
    "List the blocks a flow step can use, with each setting's type, default and whether it is "
    "required or secret. Includes approved plugin blocks from the flows folder.",
    {
        "group": {
            "type": "string",
            "description": "only this palette group, e.g. Transform or Plugins",
        }
    },
)
def _list_blocks(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    blocks = box.workspace.catalog()
    if "group" in args:
        blocks = [b for b in blocks if b["group"].lower() == args["group"].lower()]
    return {"blocks": blocks}


@tool(
    "describe_block",
    "One block in detail: its settings, documentation and an example step to paste into a flow.",
    {"name": {"type": "string", "description": "block name"}},
    ["name"],
)
def _describe_block(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    box.workspace.catalog()  # loads the folder's plugin blocks
    spec = get_block(args["name"])
    entry = next(b for b in block_catalog() if b["name"] == spec.name)
    example = {
        "block": spec.name,
        "settings": {s.name: _placeholder(s) for s in spec.settings if s.required},
    }
    return {**entry, "doc": inspect.getdoc(spec.fn) or "", "example_step": example}


@tool(
    "list_flows",
    "List the flows in the folder: description, whether each is valid, its schedules and version.",
)
def _list_flows(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    flows = []
    for name in box.workspace.list():
        try:
            document = box.workspace.read(name)
        except (OSError, ValueError) as exc:
            flows.append(
                {
                    "name": name,
                    "description": "",
                    "valid": False,
                    "problems": [f"can't read it: {exc}"],
                    "schedules": [],
                    "version": box.workspace.file_version(name),
                }
            )
            continue
        result = box.workspace.validate(name, document)
        doc = document if isinstance(document, dict) else {}
        schedules = doc.get("schedules")
        flows.append(
            {
                "name": name,
                "description": str(doc.get("description", "")),
                "valid": result["valid"],
                "problems": result["problems"],
                "schedules": sorted(schedules) if isinstance(schedules, dict) else [],
                "version": box.workspace.file_version(name),
            }
        )
    return {"flows": flows}


@tool(
    "get_flow",
    "Read a flow's JSON document and version. Pass that version as base_version when saving.",
    {"name": _NAME},
    ["name"],
)
def _get_flow(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    return _flow(box, args["name"])


@tool("list_templates", "Starter flows that create_flow_from_template can copy.")
def _list_templates(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    return {"templates": box.workspace.templates()}


@tool(
    "list_environments", "Names of the environments a run can use (their values are never shown)."
)
def _list_environments(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    return {"environments": box.workspace.environments()}


@tool(
    "flow_history",
    "Earlier saved versions of a flow, newest first, with who saved them and why.",
    {"name": _NAME},
    ["name"],
)
def _flow_history(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    return {"versions": box.workspace.versions(args["name"])}


@tool("list_block_drafts", "Blocks drafted for a person to approve, with their check results.")
def _list_block_drafts(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    return {"drafts": list_drafts(box.folder)}


@tool(
    "validate_flow",
    "Check a flow document without saving it. Returns every problem in plain language.",
    {"name": _NAME, "document": _DOCUMENT},
    ["name", "document"],
)
def _validate_flow(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    result = box.workspace.validate(args["name"], args["document"])
    return {
        "valid": result["valid"],
        "problems": result["problems"],
        "steps": result.get("steps", []),
    }


@tool(
    "test_flow",
    "Run the sample checks in a flow's tests section (saved, or document if given).",
    {"name": _NAME, "document": _DOCUMENT},
    ["name"],
    group="write",
)
def _test_flow(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    document = args["document"] if "document" in args else box.workspace.read(args["name"])
    validation = box.workspace.validate(args["name"], document)
    if not validation["valid"]:
        raise ToolError("the flow has problems", {"problems": validation["problems"]})
    reports = box.workspace.test(args["name"], document) if document.get("tests") else []
    return {
        "passed": sum(bool(r["passed"]) for r in reports),
        "total": len(reports),
        "tests": reports,
    }


# -------------------------------------------------------------------- write
@tool(
    "save_flow",
    "Save a flow after validating it. For an existing flow, pass the version from get_flow as "
    "base_version; if the person changed it meanwhile the save is refused with the current "
    "document so you can merge. Leave base_version out for a new flow. The message goes into "
    "the flow's history.",
    {
        "name": _NAME,
        "document": _DOCUMENT,
        "message": {"type": "string", "description": "what changed, for the history"},
        "base_version": {"type": "string", "description": "version from get_flow"},
    },
    ["name", "document", "message"],
    group="write",
)
def _save_flow(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    name = args["name"]
    try:
        result = box.workspace.save(
            name,
            args["document"],
            message=f"{args['message']} (agent)".strip(),
            expect_version=True,
            base_version=args.get("base_version"),
        )
    except VersionConflict as exc:
        data: dict[str, Any] = {"current_version": exc.current}
        if exc.current is not None:
            try:
                data["current_document"] = box.workspace.read(name)
            except (OSError, ValueError) as read_error:
                data["current_document_error"] = str(read_error)
        raise ToolError(str(exc), data) from None
    if not result["valid"]:
        raise ToolError(
            "the flow has problems, so it wasn't saved", {"problems": result["problems"]}
        )
    return {
        "saved": True,
        "name": name,
        "version": hashlib.sha256(
            (json.dumps(args["document"], indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        ).hexdigest()[:16],
        "steps": result["steps"],
    }


@tool(
    "create_flow_from_template",
    "Create a new flow (and its sample data) from a starter template.",
    {"template": {"type": "string"}, "name": _NAME},
    ["template", "name"],
    group="write",
)
def _create_flow_from_template(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    box.workspace.create_template(
        args["template"], args["name"], message=f"created from {args['template']} (agent)"
    )
    return _flow(box, args["name"])


@tool(
    "restore_flow_version",
    "Restore an id from flow_history. The replaced version stays in the history.",
    {"name": _NAME, "version_id": {"type": "string"}},
    ["name", "version_id"],
    group="write",
)
def _restore_flow_version(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    name, vid = args["name"], args["version_id"]
    try:
        document = box.workspace.version(name, vid)
    except FileNotFoundError:
        raise ToolError(f"flow {name!r} has no version {vid!r}; see flow_history") from None
    result = box.workspace.save(name, document, message=f"restored {vid} (agent)")
    if not result["valid"]:
        raise ToolError("that version no longer validates", {"problems": result["problems"]})
    return _flow(box, name)


@tool(
    "draft_block",
    "Propose a new block written in Python. It is checked without running it and saved as a "
    "draft; nothing runs until a person approves it in the MiniFlow Editor. Blocks look like: "
    'from miniflow.blocks import Setting, block / @block("name", settings=[Setting("x", str)]) '
    "/ def fn(x): ... returning JSON-like data. Top-level code may only be imports, functions and "
    "literal constants. Import Setting and block without aliases; no star or relative imports. "
    "Defaults must be literals. Custom annotations require from __future__ import annotations.",
    {
        "name": {"type": "string", "description": "block name: lowercase letters, digits and _"},
        "source": {"type": "string", "description": "the whole Python file"},
        "replace": {"type": "boolean", "description": "replace a pending draft of the same name"},
    },
    ["name", "source"],
    group="write",
)
def _draft_block(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    draft = write_draft(
        box.folder, args["name"], args["source"], replace=args.get("replace", False)
    )
    return {
        "drafted": True,
        "name": draft["name"],
        "checks": draft["checks"],
        "imports": draft["imports"],
        "next": "Ask the person to review and approve it in the MiniFlow Editor under Pending "
        f"blocks (or: miniflow approve-block {draft['name']} --flows {box.folder}). Flows can't "
        "use it until then.",
    }


# --------------------------------------------------------------------- runs
_RUN_ID_WAIT = 5.0


def _link(box: Toolbox) -> RunLink:
    if box.runs is None:  # run tools are hidden then; this guards direct calls
        raise ToolError("runs are turned off for this server")
    return box.runs


def _newest_run(box: Toolbox, flow: str, since: float) -> str | None:
    with StateStore(box.db) as store:
        return next(
            (r.run_id for r in store.list_runs() if r.flow == flow and r.started_at >= since), None
        )


@tool(
    "run_flow",
    "Start a run of a saved flow and return at once (it may take up to a few seconds to learn "
    "the run id). Runs happen in the MiniFlow dashboard, in the same queue as the person's Run "
    "button; one is started for you if none is running. Check on it later with get_run. If the "
    "result says queued, call list_runs later to find its run id.",
    {
        "name": _NAME,
        "environment": {"type": "string", "description": "an environment from list_environments"},
    },
    ["name"],
    group="run",
)
def _run_flow(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    link = _link(box)
    name = args["name"]
    box.workspace.read(name)  # run tools accept configured JSON flows only
    since = time.time()
    active = link.run(name, args.get("environment"))
    where = {"dashboard": link.url(), "database": str(box.db)}
    if active.get("queued"):
        queue = link.control().get("queued", [])
        position = next(
            (
                i + 1
                for i, q in enumerate(queue)
                if q.get("flow") == name
                and q.get("source", f"manual:run:{name}") == f"manual:run:{name}"
            ),
            None,
        )
        return {"state": "queued", "position": position, **where}
    run_id = active.get("run_id")
    deadline = time.monotonic() + _RUN_ID_WAIT
    while run_id is None and time.monotonic() < deadline:
        run_id = _newest_run(box, name, since)
        if run_id is None:
            time.sleep(0.1)
    return {"state": "started", "run_id": run_id, **where}


@tool(
    "get_run",
    "Run status and per-step state, attempts, errors and result previews.",
    {"run_id": {"type": "string"}},
    ["run_id"],
    group="run",
)
def _get_run(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    with StateStore(box.db) as store:
        meta = next((r for r in store.list_runs() if r.run_id == args["run_id"]), None)
        if meta is None:
            raise ToolError(f"there is no run {args['run_id']!r}")
        tasks = store.changes_since(meta.run_id, 0.0)
    steps = [
        {
            "name": t.task, "state": t.state.value, "attempts": t.attempts, "error": t.error,
            "preview": t.preview, "from_cache": t.from_cache,
            "started_at": t.started_at, "finished_at": t.finished_at,
        }
        for t in tasks
    ]  # fmt: skip
    return {**asdict(meta), "finished": meta.finished_at is not None, "steps": steps}


@tool(
    "list_runs",
    "Recent runs with step-state counts, newest first, and runs waiting in the queue.",
    {
        "flow": {"type": "string"},
        "limit": {"type": "integer", "description": "1 to 200, default 20"},
    },
    group="run",
)
def _list_runs(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    limit = args.get("limit", 20)
    if not 1 <= limit <= 200:
        raise ToolError("limit must be from 1 to 200")
    flow = args.get("flow")
    with StateStore(box.db) as store:
        runs = [r for r in store.list_runs() if flow is None or r.flow == flow][:limit]
        out = []
        for r in runs:
            counts: dict[str, int] = {}
            for state in store.get_states(r.run_id).values():
                counts[state.value] = counts.get(state.value, 0) + 1
            out.append({**asdict(r), "counts": counts})
    queued = [
        q
        for q in _link(box).control(start=False).get("queued", [])
        if flow is None or q.get("flow") == flow
    ]
    return {"runs": out, "queued": queued}


@tool(
    "cancel_run",
    "Stop an active run. It ends INTERRUPTED and the person can resume it from the dashboard.",
    {"run_id": {"type": "string"}},
    ["run_id"],
    group="run",
)
def _cancel_run(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    _link(box).cancel(args["run_id"])
    return _get_run(box, args)
