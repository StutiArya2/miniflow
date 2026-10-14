# MCP server: let an agent build and run flows

Status: design. Part of the agentic features work. Step 1 is this MCP server. Step 2 (a
"Ask Claude" panel in the Editor) reuses the same tools and gets its own spec later.

## Goal

`miniflow mcp --flows DIR` lets an MCP client (Claude Code, Claude Desktop, or any other)
read the block catalog, write and validate JSON flows, run them, read the results, and
propose new blocks. A person asks "build a flow that reads `sales.csv`, converts the amounts
to rupees and emails a summary every Monday", and the agent looks up the blocks, writes the
flow, validates it, fixes the problems it is told about, saves it, and test-runs it. The flow
then shows up in the Editor like any other, and the run shows up on the Runs tab.

Out of scope for now: the in-app chat panel, Python (`*.py`) flows, and deleting anything.

## Shape

```
miniflow/agent/__init__.py
miniflow/agent/tools.py    the tool layer: plain functions over FlowWorkspace, the dashboard
                           client and StateStore; JSON in, JSON out; no MCP knowledge
miniflow/agent/mcp.py      MCP over stdio: JSON-RPC framing, initialize, tools/list,
                           tools/call, ping; maps tool errors to MCP tool results
miniflow/agent/drafts.py   block drafts: static checks, list, approve, discard (shared by the
                           MCP tools, the dashboard endpoints and the CLI)
miniflow/agent/owner.py    finds the dashboard that runs flows for a database, or starts one
miniflow/cli.py            new `mcp` subcommand and `blocks approve`
miniflow/ui/server.py      draft endpoints, the owner file
miniflow/ui/control.py     finds flows saved by another process
miniflow/ui/static/        the Editor's Pending blocks panel
```

The tool layer is separate so Step 2 can call the same functions from the dashboard server.
Each tool is declared once (name, description, JSON Schema for its input, function), and
both `tools/list` and the future panel are generated from that list.

**Standard library only.** The stdio transport is newline-delimited JSON-RPC 2.0 with a
handful of methods, about 200 lines with `json` and `sys`. Using the official `mcp` SDK would
mean an optional extra, a lazy import and a fallback, all for less than it saves, so we write
it ourselves. The server negotiates the protocol version in `initialize` (reply with the
client's version if we support it, otherwise our newest) and declares only the `tools`
capability.

**stdout belongs to the protocol.** Anything else printed there corrupts the stream. While a
tool runs, `sys.stdout` is redirected to `sys.stderr` (a plugin block that `print`s on import
must not break the session), and logging goes to stderr. The dashboard the server may start
gets `stdout=DEVNULL`.

## Command

```bash
miniflow mcp --flows DIR [--db miniflow.db] [--read-only] [--no-runs]
```

- `--flows` is required. Every tool works inside that folder only, through `FlowWorkspace`
  and the dashboard's run control, which already refuse paths, symlinks and names outside it.
- `--read-only` keeps only the read tools (no save, restore, run, cancel or block drafts).
- `--no-runs` drops the run tools but allows editing, and never starts a dashboard.

Registration, documented in the README:

```bash
claude mcp add miniflow -- uv run --directory /path/to/miniflow miniflow mcp --flows examples
```

and the matching `mcpServers` entry for Claude Desktop's config file.

## Tools

Every result is a JSON object, returned as `structuredContent` plus the same JSON as text for
clients that ignore structured output. Expected failures (a bad name, an invalid flow, a busy
flow) come back as a tool result with `isError: true` and a plain-language message, so the
agent can read it and correct itself. Protocol errors are kept for malformed requests.

### Reading

| Tool | Input | Returns | Built on |
|---|---|---|---|
| `list_blocks` | `group?` | name, group, kind, description, settings (type, required, default, choices, secret, file) | `FlowWorkspace.catalog()` |
| `describe_block` | `name` | one catalog entry, its docstring, and a ready-to-paste step example | `get_block` |
| `list_flows` | none | name, description, valid or not, schedules, `version` | `list`, `read`, `validate` |
| `get_flow` | `name` | the document and its `version` | `FlowWorkspace.read` |
| `list_templates` | none | starter templates | `templates()` |
| `list_environments` | none | names only, never values | `environments()` |
| `flow_history` | `name` | saved versions: time, user, message | `versions()` |

`version` is a hash of the file's bytes. It is used for conflict checks when saving.

### Writing flows

| Tool | Input | Returns | Built on |
|---|---|---|---|
| `validate_flow` | `name`, `document` | `valid`, every problem, step order | `FlowWorkspace.validate` |
| `save_flow` | `name`, `document`, `message`, `base_version?` | the validation result and the new `version` | `FlowWorkspace.save` |
| `create_flow_from_template` | `template`, `name` | the new document | `create_template` |
| `test_flow` | `name`, `document?` | each declared sample-input check, passed or with its difference | `FlowWorkspace.test` |
| `restore_flow_version` | `name`, `version_id` | the restored document | `FlowWorkspace.restore` |

- `save_flow` never writes an invalid flow; `FlowWorkspace.save` already validates first.
- **Conflicts.** The person may be editing the same flow in the Editor. If the file exists,
  `base_version` is required and must match the file's current `version`; otherwise the save
  is refused with the current document attached, so the agent can merge and try again. A new
  flow needs no `base_version` and is refused if the name is taken.
- The history message gets an `(agent)` suffix, so the Editor's version list shows which
  saves came from an agent. Every save is in the history, so `restore_flow_version` is the undo.
- Secrets keep their existing rule: a flow with a secret written in plain text is refused, so
  the agent has to use `{{ env.NAME }}`. Values from `environments/*.env` are never returned.

### Runs (left out with `--read-only` or `--no-runs`)

| Tool | Input | Returns |
|---|---|---|
| `run_flow` | `name`, `environment?` | at once: `{"state": "started", "run_id"}` or `{"state": "queued", "position"}` |
| `get_run` | `run_id` | status, per-step state, the error of each failed step, attempt count, result previews |
| `list_runs` | `flow?`, `limit?` (default 20) | recent runs with state counts, then the runs waiting in the queue |
| `cancel_run` | `run_id` | the run's state afterwards |

`run_flow` never waits. Its tool description tells the agent to call `get_run` later to see
how the run went, and `list_runs` to find the id of a run that was queued. Results are the
stored previews, not full outputs, to keep replies small. Error text is already masked for
secrets by the block runtime.

## One process runs flows: the dashboard

`RunController` matches each process it starts to its run by taking "the newest run of that
flow since I launched it". Two controllers on one database would take each other's runs, and
one could mark the other's run INTERRUPTED. So exactly one process runs flows for a database,
and everyone else asks it to.

**The owner file.** A dashboard that can run flows (`miniflow ui --flows` or `miniflow serve`)
creates `<db>.owner.json` with exclusive create (`open(..., "x")`). It holds the pid, port,
flows folder and start time, and is removed on shutdown. If the file exists, the dashboard
checks it: when that pid is alive and its `/api/control` answers, the new dashboard refuses to
start ("a dashboard for this database is already running at http://127.0.0.1:PORT"). A file
left by a crashed process is replaced. A read-only `miniflow ui` without `--flows` needs no
owner file and is unaffected.

**The MCP server never runs flows itself.** On its first run tool it reads the owner file:

- A live dashboard for the same flows folder: run tools go to its existing
  `POST /api/control/run` and `/cancel` endpoints, and `GET /api/control` for the queue. Runs
  started this way wait in the same queue and count against the same `--max-concurrent` as
  the Run button and schedules.
- A live dashboard for a different flows folder: run tools fail with a message naming both
  folders, since the same database can't serve two folders' runs.
- No dashboard: the MCP server starts one, `python -m miniflow serve --flows DIR --db DB
  --no-browser`, waits up to 10 s for the owner file, and uses it. Its address goes into the
  `run_flow` result, so the person can open it and watch. That dashboard is the MCP server's
  child: it stops when the MCP server exits, and a run still in progress becomes INTERRUPTED,
  which the person can resume from the dashboard later.

Two MCP servers starting a dashboard at the same moment is fine: one wins the exclusive
create, the other's `serve` refuses, and both read the winner's owner file.

`get_run` and `list_runs` read the database directly; reads were always safe to share.

**New flows reach the Run button.** `RunController` lists the flows folder once, at startup,
so a flow the agent saves afterwards would be "no flow named X". On an unknown name, it now
looks for `DIR/<name>.json` (same name rules as `FlowWorkspace`) before refusing.

## Proposing blocks

A block is Python that runs with the user's full permissions, so **the agent can only draft a
block. A person approves it in the Editor.** Nothing the agent writes is imported or run before
approval.

| Tool | Input | Returns |
|---|---|---|
| `draft_block` | `name`, `source` | static check results, and "ask the person to approve it in the Editor under Pending blocks" |
| `list_block_drafts` | none | pending drafts with their check results |

`draft_block` writes `blocks/_draft_<name>.py`. `load_plugins` skips files that start with `_`,
so a draft is never loaded. Static checks use `ast` only, never import:

- the source parses, and has exactly one module-level function decorated with
  `@block("<name>", ...)` whose name matches;
- `<name>` is not an existing block or draft (no silent overwrites), and the file name won't
  shadow a module: `load_plugins` imports by file name with the folder first on `sys.path`, so
  a block named `json` or `requests` would replace that module for every later import;
- `settings` names match the function's parameters;
- module-level code is limited to imports, functions, classes, constants and docstrings, so
  nothing runs just from importing the file;
- the imports are listed in the result, so the person can see what the block uses.

### Approving in the Editor

The Editor shows a **Pending blocks** button in the block list with a count when there are
drafts. It opens a panel listing each draft: the source (read-only), every check result, the
modules it imports, and when it was drafted. Two buttons:

- **Approve** asks once more ("This code will run on your computer with your permissions"),
  then renames the draft to `blocks/<name>.py` and loads it. The block appears in the Plugins
  group straight away. If importing it fails, the file is renamed back to a draft and the error
  is shown, so a broken block never stays installed.
- **Discard** removes the draft file.

Endpoints, which exist only when the dashboard has `--flows` and isn't `--read-only`, and use the
same Host, Origin and body-size checks as `/api/control`:

```
GET  /api/blocks/drafts            drafts: name, source, sha256, checks, imports, drafted_at
POST /api/blocks/drafts/approve    {"name", "sha256"}
POST /api/blocks/drafts/discard    {"name", "sha256"}
```

The `sha256` is the hash of the source the person was shown. If the draft changed since, the
request is refused and the panel reloads, so a draft can't be swapped between reading and
approving. A draft whose checks fail can be discarded but not approved.

The same approval is available in a terminal for people who use Claude Code there:
`miniflow blocks approve NAME --flows DIR` prints the source and checks, then asks `y/N`.

The MCP process imports an approved block on its next catalog read, as `load_plugins` imports
new files. Changing an approved block is out of scope: `load_plugins` skips modules it has
already imported, so an edited block would stay stale in running processes. The agent drafts a
block under a new name instead.

**What approval protects against.** It stops an agent that has only the MCP tools (Claude
Desktop, for example) from installing code. An agent that also has a shell, like Claude Code,
can write files into `blocks/` itself, and the dashboard endpoints are reachable from any local
program. For those agents the Pending blocks panel is a review step, and Claude Code's own
permission prompts are the guard.

## Decisions

- **Agent changes are visible and reversible.** Flows go through the existing history and
  validation; blocks go through approval in the Editor. There is no delete tool.
- **Runs go through one owner per database**, so the Run button, schedules and agents share
  one queue and one concurrency limit.
- **Runs don't wait.** `run_flow` returns at once, and the agent checks back with `get_run`.
  It never holds a tool call open, so no client timeout applies.
- **Not exposed:** CSV upload (the agent can reference files already in the folder), resume
  and partial runs, schedule management (schedules live in the flow document, so `save_flow`
  covers them), and Python flows.
- **Known gap:** the Editor doesn't notice a flow the agent saved while it was open, and saving
  there replaces the agent's version. Both versions stay in the history. A conflict check in
  the Editor's save is a follow-up.

## Tests

`tests/test_agent_tools.py`, `tests/test_agent_mcp.py`, `tests/test_agent_drafts.py`,
`tests/test_agent_owner.py`, plus additions to `tests/test_ui.py` and `tests/test_control.py`.

- Protocol: `initialize` and version negotiation, `tools/list` against the declared schemas,
  unknown method and malformed JSON, `ping`. Run over in-memory streams, plus one real
  `miniflow mcp` subprocess round trip.
- stdout: a plugin that prints on import doesn't corrupt the stream.
- Flows: save refuses invalid documents, a stale `base_version` and a taken name; the history
  message has the `(agent)` suffix; restore works; environment values never appear in any
  result.
- Ownership: a second dashboard on the same database is refused; a stale owner file is
  replaced; the MCP server forwards to a running dashboard; with none, it starts one and stops
  it on exit; a dashboard for another folder gives the clear error; two MCP servers starting
  at once end up with one dashboard; a run started by the MCP server and one from the Run
  button share the queue. Must work under spawn (Windows CI).
- Control: a flow saved after the controller started can be run.
- Drafts: a draft whose module-level code would create a file never creates it (proof it is
  never imported); each static check refuses its case, including a name that shadows `json`;
  approve with a stale `sha256` is refused; approve of a block that fails to import goes back
  to a draft; discard; the endpoints don't exist with `--read-only`; `blocks approve` with `y`
  and with `n`.
- Hypothesis: any tool given arbitrary JSON input returns an error result and never raises
  out of the server.

## Demo

1. `claude mcp add miniflow -- uv run miniflow mcp --flows examples`
2. In Claude Code: "Make a flow that reads `examples/data/sales.csv`, converts amounts to
   rupees with `convert_currency`, keeps orders over 5000 and writes `big_orders.xlsx`."
3. Watch it call `list_blocks`, `validate_flow` (fix), `save_flow`, `run_flow`, then
   `get_run`. With no dashboard open, it starts one and gives its address. Open it: the flow
   is in the Editor with an `(agent)` history entry, and the run is on the Runs tab.
4. With the dashboard open, click Run on a slow flow, then ask the agent to run the same
   flow. Its run waits in the queue behind yours.
5. Ask for a block that isn't there. It drafts one. In the Editor, open Pending blocks, read
   it, approve it; it appears in the Plugins group and the agent can use it.
