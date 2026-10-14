# MiniFlow: build plan

## Context
`MiniFlow_Project_Document.pdf` (VIT Assessment 8: Megha, Stuti, Jayesh) specifies **MiniFlow**, a task scheduler written using only the standard library. It covers:
- DAG validation (Kahn's algorithm, plus DFS to report cycles)
- scheduling by critical-path priority
- routing each task to thread, process or async workers
- retries with backoff and jitter, and SKIPPED propagation to downstream tasks
- SQLite resume, a CLI, and a pytest and benchmark evaluation

The folder holds only the PDF so far, with no code and no git. Local setup: Python 3.13.6, git 2.51, 8 cores, and `gh` logged in as **StutiArya2** (repo and workflow scopes).

Decisions: a **private repo with teammates as collaborators**, and **Claude builds everything** while the team reviews. Where it helps, use **Claude Code cloud sessions** (claude.ai/code) so modules are built in parallel on cloud VMs, each one opening a PR.

## Repo layout
```
miniflow/            # package
  __init__.py  task.py  graph.py  executors.py  retry.py
  store.py  scheduler.py  cli.py  __main__.py
  ui/                # dashboard (Phase 3.5): server.py, layout.py, static/{index.html, app.js, style.css}
tests/               # pytest, one file per module + fault-injection + integration
benchmarks/          # synthetic DAGs (chain, fan-out, diamond, random layered), runners, plots
examples/            # demo pipeline from §3.3 of the doc
docs/                # architecture notes, results for the report
pyproject.toml  README.md  CLAUDE.md  .github/workflows/ci.yml
```
`CLAUDE.md` stores the spec rules so that every cloud session follows them: stdlib only, Python ≥3.11, the state machine in §3.5, and the module ownership in §3.4.

## External dependencies
**Important constraint:** the submitted spec makes "Standard library only" a core design goal (§3.1), and the §2.7 research gap depends on it. So the **core `miniflow` package stays stdlib-only**. Everything else is fair game: dev tooling, tests, benchmarks and docs. Nice-to-have runtime features become **optional extras** that fall back cleanly when not installed. This keeps the spec's claim and still makes the project far more polished.

Managed with **uv** (fast env and lockfile), in `pyproject.toml` dependency groups:

| Group | Packages | Why |
|---|---|---|
| `dev` | ruff, mypy (strict), pre-commit | Lint, format, type checking; hooks run on every commit and in CI |
| `test` | pytest, pytest-cov, pytest-timeout, pytest-xdist, pytest-asyncio, **hypothesis**, **networkx** | Hypothesis generates random DAGs (with and without cycles) for property tests; networkx is a second oracle for topological order and longest path, alongside `graphlib`; timeouts stop tests that hang on a scheduler deadlock |
| `bench` | pytest-benchmark, pandas, matplotlib, seaborn, **dask[distributed]**, psutil | Speedup and overhead plots; **Dask as a real baseline** in the §3.8 comparison (the doc already cites it as the closest system); psutil records CPU use per worker kind |
| `docs` | mkdocs-material, mkdocstrings[python] | Docs site with API reference built from docstrings, deployable to GitHub Pages |
| extra `miniflow[rich]` | rich | Colored live progress table for `run`/`status`; falls back to plain logging |
| extra `miniflow[viz]` | graphviz (Python package and the Graphviz binary via `winget install Graphviz`) | Renders the DAG to PNG or SVG with nodes colored by state; the stdlib-only DOT text export always works |
| ~~extra `miniflow[cloudpickle]`~~ | — | Dropped in Phase 4: not in the spec. Instead, cpu tasks can now be built from any module-level function under any name, which was the real need. |

CI additionally runs ruff, mypy and coverage (fails below 90%) and publishes the docs site.

## Phases

### Phase 0: bootstrap (local, this session)
1. Install uv (`winget install astral-sh.uv`) and Graphviz. `git init`, then add the skeleton above: empty modules with docstrings and type stubs, `pyproject.toml` with the dependency groups and extras, `uv.lock`, `.pre-commit-config.yaml`, `.gitignore`, `CLAUDE.md`, README, `mkdocs.yml`, and a copy of the spec in `docs/`.
2. CI in GitHub Actions: run pytest on Ubuntu and Windows with Python 3.11 and 3.13. Windows matters because process pools use spawn there, which catches pickling bugs.
3. `gh repo create StutiArya2/miniflow --private --source . --push`, then `gh api` collaborator invites (needs Megha's and Jayesh's GitHub usernames).

### Phase 1: foundation (local, sequential; everything else depends on it)
- `task.py`: `@task(kind, after, retries, timeout)`, `Task` dataclass, and a registry. Check at registration that `cpu` tasks are picklable (module-level functions).
- `graph.py`: DAG build, rejection of unknown dependencies, Kahn ordering, DFS cycle path, and longest-path-to-sink ranks. Tests cross-check the ordering against `graphlib.TopologicalSorter`.
- Merge to `main`. The interfaces are now fixed, so parallel work is safe.

### Phase 2: parallel modules (**cloud sessions**, one per branch and PR)
Each session gets the repo, `CLAUDE.md` and the interface contract from Phase 1:
- **A: `executors.py`**: a single `submit()` that returns a `Future` for each worker kind. Thread pool, process pool (default size `os.cpu_count()`), and an asyncio loop on a background thread wrapped via `asyncio.run_coroutine_threadsafe`.
- **B: `retry.py`**: `RetryPolicy(base, cap, max_attempts)` with backoff `base·2^attempt + jitter`, capped at the maximum, plus a timeout helper implemented as a context manager.
- **C: `store.py`**: SQLite tables `runs`, `task_states` and `attempts`, results stored as pickle or JSON, plus `load_completed(run_id)` for resume.

All three only depend on Phase 1, so they run at the same time. I review each PR locally and merge it.

### Phase 3: integration (local or one cloud session)
- `scheduler.py`: in-degree counters, a `heapq` ready queue keyed by critical path, and `concurrent.futures.wait(FIRST_COMPLETED)`. Implements every transition in the §3.5 state machine, BFS marking of SKIPPED tasks, and resume that skips SUCCESS tasks.
- `cli.py`: `run`, `resume`, `status`, and `graph` (DOT export).
- Integration and fault-injection tests: random failures, timeouts, slow tasks, killing a run and resuming it, and checking from the store that execution order always respects dependencies.

**Status: ✅ done.** Decisions made while building it:
- **Resume creates a new run.** It reuses the stored results of SUCCESS tasks from the earlier run (`from_cache=True`), so both runs stay in the history. A result is reused only if all its upstream results were reused too.
- **Ready queues are per worker kind and limited to pool capacity.** Critical-path priority only works if the scheduler, not the pool's FIFO queue, picks the next task.
- **Timeouts:** coroutines are cancelled. Threads and processes can't be stopped, so the task is marked failed and the work is abandoned. The CLI then exits straight away (`cli.entry`) instead of waiting for it.
- **A crashed worker process** (`BrokenProcessPool`) counts as a failed attempt and `ExecutorRouter.restart_cpu_pool()` replaces the pool, once per broken pool.
- **Process pools default to `spawn` on every platform.** Forking while io and async threads exist can deadlock.
- **Results that can't be pickled** still flow to downstream tasks, but aren't stored, so they rerun on resume.
- Ctrl+C gives an `INTERRUPTED` run that can be resumed. Exit codes: 0 success, 1 failed, 130 interrupted.
- Demo flow with injected faults: `examples/faulty_etl.py`.

### Phase 3.5: dashboard (`miniflow ui`)
The spec only has a CLI and a Graphviz export. A live web dashboard makes the project much easier to demo and to explain in the viva. It is **still standard-library only**: no web framework, no npm, no CDN, and it works offline.

**Backend: `miniflow/ui/server.py`**
- `http.server.ThreadingHTTPServer` with a small router. It reads only from the SQLite `StateStore`, so it works during a run and after one.
- Endpoints:
  - `GET /api/runs`: list of runs (flow, status, start and end times, counts per state)
  - `GET /api/runs/<id>`: graph (tasks, kinds, edges, layout coordinates), the current state of every task, and the attempt log
  - `GET /api/runs/<id>/events`: Server-Sent Events stream that pushes state changes, so the page updates live without polling. Falls back to polling every second.
  - `GET /`: the static page, served from package data via `importlib.resources`
- Binds to `127.0.0.1` only. Read-only, so a demo can't change runs.

**Layout: `miniflow/ui/layout.py`**
- Layered DAG layout computed in Python from `DAG.topological_order()`: each task's column is the length of its longest path from a root. Rows within a column are ordered with a barycenter pass to reduce edge crossings.
- Returns x and y for each node so the browser only draws SVG. No JavaScript graph library is needed.

**Frontend: `miniflow/ui/static/`** (one page, vanilla JS and SVG, light and dark themes)
1. **Graph view.** Nodes are colored by state, using the same colors as `DAG.to_dot`. The shape shows the worker kind (box for io, 3D box for cpu, pill for async). Running tasks pulse, and edges into SKIPPED tasks are dashed. Clicking a node opens its details.
2. **Timeline (Gantt).** One lane per worker (thread, process PID, event loop) and one bar per attempt, so parallelism, retries and backoff gaps are visible at a glance. Failed attempts are red.
3. **Runs table.** All runs with status, duration and success/failed/skipped counts. Clicking a run loads it.
4. **Task details.** State, attempts, errors with tracebacks, durations, upstream and downstream tasks, and whether the result was loaded from the store on resume.
5. **Summary bar.** Progress, elapsed time, an estimate of the speedup over sequential execution (sum of task durations ÷ wall time), and tasks per worker kind.

**CLI**
- `miniflow ui [--db PATH] [--port 8765]` starts the dashboard and opens the browser.
- `miniflow run flow.py --ui` runs the flow with the dashboard running alongside it.

**What this needs from earlier phases (already in the Phase 2 contracts)**
- The store saves the DAG as JSON on each run (`runs.graph`), so the dashboard can draw a run without the flow file.
- Each attempt records which `worker` ran it, for the timeline lanes.
- Executor futures resolve to `Outcome(value, worker, started_at, finished_at)`, so timeline bars show the actual run time, not time spent waiting in the queue.

**Tests:** layout properties (every edge points to a later column; nodes don't overlap), the API's JSON shape against a store filled with test data, SSE delivering a state change, and one end-to-end check: run the example flow with `--ui`, read `/api/runs/<id>` and confirm every task is SUCCESS.

**Demo script for the evaluation:** run a flow with injected failures and `--ui`. Watch the retries in the timeline and the descendants of the failed task turn grey. Kill the run, start `resume`, and watch only the unfinished tasks run.

**Status: ✅ done.** Notes from building it:
- `miniflow ui` works from a separate process while a run is live. The server polls SQLite (WAL) every 0.25 s and pushes only what changed over SSE. The browser itself never polls during a run, except to refresh the runs list every 3 s for *Follow latest*.
- Bars for tasks still running are drawn from `task_states.updated_at` (the moment they became RUNNING) until "now". The worker name is only known when the attempt finishes.
- Attempts that time out have no worker, so they appear in a "timed out" lane.
- Speedup is Σ attempt time ÷ wall time, so runs that mostly wait on retry backoff can show less than ×1. That is correct, and worth explaining in the viva.
- Checked in the browser in light and dark mode and at phone width (375 px, no horizontal page scroll).

### Phase 3.6: dashboard controls (`miniflow ui --flows DIR`)
So the whole viva demo (fail, cancel, resume) can be driven from the browser instead of a terminal.

- `miniflow/ui/control.py`: `RunController` lists `DIR/*.py` (skipping `_*.py`) and launches each run as its own `python -m miniflow run|resume <file> --db <db> --quiet` process, with `MINIFLOW_FAIL_RATE` from the page. Using a separate process keeps `scheduler.py` unchanged, gives every run a fresh import (so module-level flow settings behave as in a terminal), and a crashing flow can't take the dashboard down.
- One run at a time. Cancel sends the process group SIGINT (Ctrl+Break on Windows, which `cli.entry` turns into `KeyboardInterrupt`), so the scheduler's own interrupt path marks the run INTERRUPTED. After 5 s the group is killed and the controller marks the run INTERRUPTED itself; it does the same, and reports an error, when a run's process dies mid-run.
- `ui/server.py`: `GET /api/control` and `POST /api/control/{run,resume,cancel}` exist only with `--flows`. A POST is refused unless its `Host` and `Origin` are the dashboard and its body is JSON (≤ 4 KB), so other web pages can't start runs.

**Status: ✅ done.** Notes from building it:
- Checked in the browser: run at 100% fault rate, cancel mid-run (INTERRUPTED), resume at 0% (SUCCESS, finished tasks reused), a refused request's error in the strip, and the unchanged read-only page without `--flows`.
- `/api/runs` replies can arrive out of order, so a reply sent before a run existed briefly showed "No runs yet" during the run. The page now ignores replies to all but its newest request.
- Resume is a fixed button in the control strip, not part of the run header, which is redrawn every 0.5 s during a run and every 3 s otherwise; a redrawn button can lose a click.
- Known and unchanged: after an interrupt the tasks that were running keep the RUNNING state (the scheduler's interrupt path doesn't update them), so the graph still shows them as running. Resume reruns them as usual.

### V1 milestone 1: no-code engine (JSON flows and blocks)
Roadmap and research: `docs/superpowers/specs/2026-10-08-v1-roadmap.md`; design:
`docs/superpowers/specs/2026-10-08-v1-m1-engine-design.md`.

**Status: ✅ done.** JSON flows compile to ordinary tasks, so `scheduler.py`, `store.py`,
`executors.py` and `graph.py` are unchanged. Notes:
- Each step's task function is `miniflow.blocks.runtime.run_step` (or `run_step_async`), and the step's configuration travels in the task's kwargs as plain data, so cpu steps run in worker processes; plugin blocks are re-imported there from their module reference.
- The loader reports every problem at once, with "Did you mean" suggestions; secrets must come from `{{ env.NAME }}` and are masked as `••••` in errors.
- `filter` conditions are parsed with `ast` and evaluated by walking the tree (no `eval`), with a whitelist of operators and functions.
- `on_failure` runs in the CLI after a FAILED run (so also for dashboard runs), not as a DAG step.

### V2: Editor 2.0
Design: `docs/superpowers/specs/2026-10-09-v2-editor-design.md`.

**Status: ✅ done.** Undo/redo and copy/paste, zoom/pan/fit and Tidy up, version history with
diff and restore, run data on the canvas. Notes:
- Undo is snapshot-based: every editor change calls `studioCommit()`, which records the flow as JSON text (100 steps). Dragging records once on release; settings record once when the field loses focus.
- The browser code is split into `editor-history.js`, `editor-view.js`, `editor-versions.js` and `editor-data.js` next to `editor.js`, plain scripts with no build step. Global names must not clash with `app.js` (an early `NODE_W` clash made the browser drop a whole file).
- History lives in `.history/<flow>/<id>.json` (gitignored). A save message belongs to the version it was saved with: `current.meta` holds it until the version is replaced.
- "Changed since this run" uses a new settings-only `config_signature` recorded with each run (an optional scheduler/store parameter like V1's `signatures`). The strict `step_signature` includes file contents, so a step writing its own output file always looked changed.

### V3: smarter flows, running it for real, more blocks
Milestones: 1 more blocks, 2 running it for real (cron, triggers, queue, retention, alerts),
3 smarter flows (`for_each`, if/else, per-step failure paths; needs a scheduler extension the team
should approve first).

**Milestone 1 (more blocks): ✅ done.** 14 new blocks (design: `docs/superpowers/specs/2026-10-09-v3-m1-blocks-design.md`).
- AWS request signing is checked against the AWS documentation example and was reproduced with `botocore` once; `read_xlsx` refuses XML with a DTD instead of using `defusedxml`.
- The database blocks import their driver lazily and say how to install it.

**Milestone 2 (dependable local runs): ✅ implemented.** Cron, catch-up, file and webhook
triggers, a bounded waiting queue, run retention and success actions are implemented on
`v3-runs` (design: `docs/superpowers/specs/2026-10-09-v3-m2-runs-design.md`).
Configurable parallel run slots are implemented for different flows; repeat launches of one
flow wait in the queue. The browser demo covered the schedule editor, webhook and file trigger;
the milestone PR remains.

### V4 milestone 1: agents over MCP ✅
Design: `docs/superpowers/specs/2026-10-14-mcp-server-design.md`; plan:
`docs/superpowers/plans/2026-10-14-mcp-server.md`.
- `miniflow mcp`: a standard-library MCP server (`miniflow/agent/`) with tools to read blocks,
  validate, save (with version checks), test and run flows, and draft blocks.
- One dashboard owns each database (`<db>.owner.json`); the MCP server sends runs to it.
- Block drafts are checked with `ast` only and approved by a person in the Editor's Pending
  blocks panel or with `miniflow approve-block`.
- Next: an "Ask Claude" panel in the Editor on the same tools; a conflict check in the
  Editor's own save.

### Phase 4: evaluation (§3.8)
- Build the benchmarks in a cloud session.
- **Run the final numbers locally** on the fixed 8-core machine so the speedup and Amdahl comparison is reproducible. Cloud VM core counts vary.
- Output CSVs and matplotlib plots (dev-only dependency) in `docs/results/` for the report.

**Status: ✅ done.** `uv run python -m benchmarks.run` (about 2.5 min) writes CSVs, figures and `results.md` to `docs/results/`. The interpretation for the report is in `docs/evaluation.md`. Notes:
- Bounds: the "best possible schedule" is level-by-level, ceil(level size / P) waves per level. It is tighter than min(P, work/span) and is the true optimum for fan-out and diamond shapes. For cpu tasks P = physical cores (4 on the dev laptop), not logical CPUs.
- Dask baseline: `dask.threaded` for io and `dask.multiprocessing` (spawn pool) for cpu.
- Every run is checked for dependency order, skip propagation and retry budgets: 440 runs, 0 violations.
- Spec gaps closed alongside: §3.3 cpu task arguments are checked for picklability at declaration, and cpu tasks can be made from any module-level function under any name (needed for the benchmarks). The unused `cloudpickle` extra was removed.

## How cloud usage fits
- Start each Phase 2 or 4 task at claude.ai/code. Select the `StutiArya2/miniflow` repo and an environment with **Trusted** network access and this setup script:
  ```bash
  #!/bin/bash
  pip install uv && uv sync --all-groups --all-extras
  apt-get install -y graphviz || true
  ```
- No network secrets are needed. The Claude GitHub App handles push and PRs for the selected repo.
- I'll write a ready-to-paste prompt for each cloud task: the module spec, the interface contract and its tests.
- I can't say for certain how cloud usage is billed against your plan or credits. Check claude.ai → Settings → Usage.

## Verification
- `pytest -q` passes locally on Windows and in CI on Ubuntu and Windows.
- `python -m miniflow run examples/pipeline.py` shows the io, cpu and async routing.
- Fault test: interrupting a run and running `resume` re-executes only the unfinished tasks.
- `miniflow run examples/pipeline.py --ui` opens the dashboard, which shows the graph filling in live and one timeline lane per worker.
- Benchmarks produce speedup and overhead plots.
