# MiniFlow

A parallel task scheduler with dependency-graph resolution, retries, and mixed
thread, process and async workers. It is built **only on the Python standard library**.

> VIT SCOPE · Advanced Python Programming · Assessment 8
> Megha Gautam (23MIC0058) · Stuti Arya (23MID0012) · Jayesh Pani (23MIC0059)
>
> Full specification: [`docs/MiniFlow_Project_Document.pdf`](docs/MiniFlow_Project_Document.pdf)

```python
from miniflow import task


@task(kind="io", retries=3, args=("https://example.com",))
def fetch(url): ...


@task(kind="cpu", after=[fetch])
def analyse(data): ...


@task(kind="async", after=[analyse])
async def publish(report): ...
```

Each task receives the results of its `after` tasks as positional arguments,
followed by its own `args`/`kwargs`. A task with upstream tasks must accept
their results, even if it ignores them.

| kind    | runs on                          | use for                  |
|---------|----------------------------------|--------------------------|
| `io`    | `ThreadPoolExecutor`             | blocking network / files |
| `cpu`   | `ProcessPoolExecutor`            | CPU-bound code (no GIL)  |
| `async` | asyncio loop in a worker thread  | coroutine clients        |

## Status

| Module          | Status |
|-----------------|--------|
| `task.py`       | ✅ done |
| `graph.py`      | ✅ done |
| `executors.py`  | ✅ done |
| `retry.py`      | ✅ done |
| `store.py`      | ✅ done |
| `scheduler.py`  | ✅ done |
| `cli.py`        | ✅ `run`, `resume`, `status`, `graph`, `validate` |
| `ui/` dashboard | ✅ `miniflow ui` and `run --ui`: Examples gallery, story, results, live graph, timeline |
| `examples/`     | ✅ four real-world pipelines on live public data, with offline snapshots |
| benchmarks      | ✅ `benchmarks/`: results in [`docs/results/results.md`](docs/results/results.md) |

## Running flows

```bash
miniflow run examples/pipeline.py          # live table (with the rich extra) or line-by-line log
miniflow status                            # tasks of the latest run
miniflow status --all                      # every run in miniflow.db
miniflow resume examples/faulty_etl.py     # rerun, reusing tasks that already succeeded
miniflow graph examples/faulty_etl.py --render svg -o flow.svg   # coloured by the latest run
```

## No-code flows (JSON)

A flow can also be a JSON file of configured **blocks**, with no Python to write. It runs
on the same scheduler, so retries, resume and the dashboard work the same way.

```json
{
  "params": {"min": {"type": "float", "default": 100}},
  "steps": {
    "sales": {"block": "read_csv", "settings": {"path": "data/sales.csv"}},
    "big":   {"block": "filter", "settings": {"rows": "{{ sales }}", "where": "amount > {{ params.min }}"}},
    "save":  {"block": "write_csv", "settings": {"rows": "{{ big }}", "path": "out/big.csv"}, "retries": 2}
  },
  "on_failure": {"block": "log", "settings": {"message": "failed at {{ failure.step }}", "level": "error"}},
  "on_success": {"block": "log", "settings": {"message": "saved {{ save.rows }} rows"}}
}
```

- **Placeholders** fill settings in when the step runs: `{{ step }}` (a step's result, which
  also makes this step wait for it), `{{ step.field.0 }}`, `{{ rows.count }}`,
  `{{ params.name }}` and `{{ env.NAME }}` (secrets come only from environment variables).
- **28 built-in blocks**, listed by `miniflow blocks` (`miniflow blocks filter` describes one):
  - data: `http_request`, `read_csv`, `write_csv`, `read_json`, `write_json`
  - transform: `filter`, `select`, `aggregate`, `sort`
  - systems and notify: `shell`, `sql`, `email`, `log`, `wait`
  - alerts, AI and cloud: `webhook`, `slack_message`, `teams_message`, `llm_prompt` (Anthropic or
    any OpenAI-compatible API), `s3_get`, `s3_put` (AWS or MinIO-style servers). Webhook URLs, API
    keys and cloud credentials must come from `{{ env.NAME }}`.
  - Excel: `read_xlsx`, `write_xlsx` (plain data sheets, no outside library)
  - dates and text: `date_format`, `date_add`, `date_diff` (a single value, or a field of every
    row) and `text_template` (turn rows into an email or chat message)
  - databases: `postgres`, `mysql`. These need a driver the core doesn't install: run
    `pip install psycopg[binary]` or `pip install pymysql`; without it the block says so.
- **Plugin blocks**: a `@block` function in a `blocks/` folder next to the flow (see
  `examples/blocks/currency.py`) is available to every flow in that folder.
- **Parameters and environments**: `--param min=50`, or `--environment prod` for the values
  in `environments/prod.json` (and secrets from `environments/prod.env`).
- **Checked before running**: every problem is reported at once, in plain language, with
  suggestions for misspelt names.

```bash
miniflow validate examples/sales_report.json
miniflow test examples/sales_report.json       # sample inputs and expected step results
miniflow run examples/sales_report.json --environment dev
miniflow ui --flows examples                   # edit, validate, save and run in the browser
```

The top bar has three tabs, **Examples**, **Runs** and **Editor**, always visible and with the
current one highlighted; the MiniFlow logo also goes back to Examples.

Use the **Editor** tab in the dashboard to open a visual canvas. Add blocks from the
searchable palette, connect them, edit settings, validate and save the JSON flow, then run it.
For a `read_csv` block, click **Upload CSV…** beside its path to choose a local file. MiniFlow
copies it into the flow workspace's `uploads/` folder and fills in the path automatically
(maximum 10 MB). You can then save and run the flow normally.
An empty flows folder also has **hello_world** and **sales_report** starters. **Use starter**
creates a compatible JSON flow and, for the sales report, its sample CSV. Open **Parameters**
to change defaults without editing JSON; use a setting's **Use value from…** selector to insert
a parameter or prior step. The **Environment** selector lists local environment names while
keeping their values on disk. Other saved JSON flows appear under **Reusable flows** in the
palette, with their declared parameters in the settings panel. Nodes also accept annotations.
Select a node and choose **Run up to selected** to execute just that node and its dependencies.
MiniFlow reuses results from the latest run when their step configuration, referenced parameters,
environment values, code, local file inputs and upstream results still match. A changed input
reruns the affected branch; each partial run appears in normal run history.
The equivalent CLI command is `miniflow run examples/sales_report.json --until by_region --reuse latest`.
The **Flow tests** tab accepts sample results for selected steps and expected results for
downstream steps. Tests run only the needed branch and leave the main run database untouched.
**Flow tests**, **Schedules**, **When the flow ends**, **History** and **Parameters** are tabs;
select one to edit that part of the workflow. While this flow is running, **Stop workflow**
in the editor stops its active run. The same action is available on the selected run's page.
Scheduled future runs remain enabled until you remove their schedule.
A step with `"flow": "parts/clean.json"` can reuse another JSON flow; its declared `params`
become settings, and its `outputs` become the result of that step. Inner steps remain visible
in the run graph.

**Editor 2.0** (V2) adds:

- **Undo and redo** for every change (Ctrl/Cmd+Z, Ctrl/Cmd+Shift+Z), and **copy, paste and
  duplicate** (Ctrl/Cmd+C, V, D). Shift-click or Shift-drag selects several steps; pasted
  steps get new names and their references to each other follow. Delete removes the selection.
- **Drag and drop** blocks (and reusable flows) from the palette onto the canvas; the step lands
  where you drop it, at any zoom. Clicking a block still adds it too.
- **Zoom and pan**: mouse wheel, drag empty space, −/+/**Fit**; **Tidy up** arranges the steps
  by dependency with the same layout as the run graph. Zooming doesn't change saved positions.
- **History**: every save keeps the version it replaces in `.history/<flow>/` next to the flow,
  with the time, account and an optional **Save note**. **Changes** lists what differs from the
  current file, step by step; **Restore** brings a version back (the replaced one is kept).
- **Run data on the canvas**: pick a run (the latest by default) to see each step's state and
  item count, item counts on the arrows, and a sample of the result in the settings panel.
  Steps whose settings changed since that run, and the steps after them, are greyed out.

For unattended local runs, add a schedule in the editor's **Schedules** tab and start
`miniflow serve --flows examples --db miniflow.db`. A flow can run every N seconds, daily at
local time, or from a five-field cron expression such as `0 9 * * mon-fri`. Set `catch_up` to
run once after the dashboard was off during scheduled times. A file watch such as
`{"name": "inbox", "watch": "inbox/*.csv"}` starts after a new file stops changing. A webhook
schedule names an environment variable holding its token; call `/api/hooks/FLOW/NAME` with
`Authorization: Bearer TOKEN` and a JSON body. Trigger details reach the flow as
`{{ env.MINIFLOW_TRIGGER }}`. Manual, scheduled and triggered runs wait in a bounded queue when
all run slots are busy; the dashboard shows queued runs and launch events. Use
`--max-concurrent N` to run up to N different flows at once (default 1). A second run of the
same flow waits so each run stays linked to its own history. `serve` keeps the newest 100
runs per flow by default; use `--keep-runs N` or `--keep-days D` to choose retention. Keep the
`serve` process running on the laptop for schedules and file watches to fire.

## Dashboard

```bash
miniflow ui                                # open the dashboard: Examples gallery + every run
miniflow run examples/faulty_etl.py --ui   # run with the dashboard open; it stays up until Ctrl+C
miniflow ui                                # browse every run in miniflow.db (live, too)
miniflow ui --flows examples               # ...and start, cancel and resume runs from the page
```

Made for people who have never seen a workflow engine:

- **Examples gallery**: one card per real-world example with a plain-English description and a
  **Run** button. Runs started there appear live.
- **Story**: what the pipeline is for and what to watch for while it runs.
- **What this run found**: the results of the pipeline's key steps in plain words, for example the
  city ranking, the alerts sent or revenue by country.
- **Step details**: what each step does, what it produced and, for a failure, the reason in one
  line. Attempts, dependencies and the Python traceback are folded under *More details*.

A local web page at `http://127.0.0.1:8765` (the next free port if that one is taken), built
only on `http.server`, Server-Sent Events and plain JavaScript/SVG. It shows:

- **List, Graph and Timeline tabs**: a table of every step with its state, tries and time.
  The graph shows tasks laid out by dependency depth, with a coloured dot for their state; the
  shape shows the worker kind (box = io, stacked box = cpu, pill = async). Edges that skip
  columns are routed between the boxes, and hovering or selecting a step highlights everything
  upstream and downstream of it. Drag to pan, Ctrl + scroll (or the buttons) to zoom; a minimap
  appears when the graph doesn't fit.
- **Timeline** (a Gantt chart): one row per step or per worker, one bar per attempt, dotted lines
  for retry backoff and a hover card for each attempt. *Critical path* highlights the chain of
  steps that decided the run's length; otherwise the header shows the parallel speedup (task
  time ÷ wall time).
- **Failure alert**: each failed step with its reason, and how many later steps were skipped.
- **Themes**: the button at the bottom of the sidebar picks Auto (follows your system), Light, Dark,
  Brutalist, Glass or Terminal; the choice is remembered in the browser.
- **Summary and runs**: steps done, time taken, retries, and the recent runs in the sidebar. *Follow latest run* jumps to new runs as they start.

With `--flows DIR`, a control strip at the top lets you pick a `.py` or `.json` flow in `DIR`,
set **Simulate failures** (a fault rate passed to the run as `MINIFLOW_FAIL_RATE`) from the **New run** menu and press **Start run**. While it runs
you can **Stop workflow**, which interrupts it like Ctrl+C, so it ends INTERRUPTED. A failed or
interrupted run gets a **Resume** button, which reruns only its unfinished tasks. Each
run is a separate `miniflow run`/`resume` process writing to the same database. Stopping the
dashboard cancels the runs it started.

Options: `--port`, `--no-browser`, `--no-ui-wait` (close the dashboard when the run ends).
The server only binds to `127.0.0.1`. Without `--flows` it is read-only. With it, the control
requests are only accepted from the dashboard page itself, and only name a flow in `DIR`.

### Example: Ocean Watch

`examples/ocean_watch.py` is a realistic job for demos: it scans 240 synthetic satellite
radar tiles for oil spills and ships (12 parallel downloads on threads, 12 detection batches
in separate processes, then a report and an alert) and writes
`examples/ocean_watch_data/report.html`. The walkthrough for presenting it is in
[`docs/demo.md`](docs/demo.md).

```bash
miniflow run examples/ocean_watch.py                    # about 6 s (10 s the first time)
miniflow run examples/ocean_watch.py --cpu-workers 1    # about 24 s: why cpu tasks use processes
```

`run` exits with 0 on success, 1 if any task failed, and 130 when interrupted.
State lives in `miniflow.db` (SQLite; change it with `--db`). `examples/faulty_etl.py`
injects failures (`MINIFLOW_FAIL_RATE`, default 0.4) and a hanging task with a timeout.

From Python:

```python
from miniflow import RetryPolicy, StateStore, run

result = run([fetch, analyse, publish], policy=RetryPolicy(base=0.5))
result.status  # "SUCCESS" | "FAILED" | "INTERRUPTED"
result["publish"]  # a task's return value
result.errors  # task -> "ExceptionType: message"
```

When a flow uses `cpu` tasks, start it under `if __name__ == "__main__":`. Worker
processes are spawned and re-import your module, so cpu functions must be at module level.

**Timeouts:** a coroutine that times out is cancelled. Python can't stop a running
thread or process, so a timed-out `io` or `cpu` task is marked failed and left to
finish in the background. The CLI exits straight away instead of waiting for it.

## Agents (MCP)

`miniflow mcp` lets an agent such as Claude Code or Claude Desktop build and run flows: it
reads the block list, writes and validates flows, saves them, runs them and reads the
results. Connect it to Claude Code with absolute paths:

```bash
claude mcp add miniflow -- uv run --directory /path/to/miniflow miniflow mcp --flows /path/to/flows --db /path/to/miniflow.db
```

For Claude Desktop, add this to its `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "miniflow": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/miniflow", "miniflow", "mcp",
               "--flows", "/path/to/flows", "--db", "/path/to/miniflow.db"]
    }
  }
}
```

- Every save the agent makes is validated first and labelled `(agent)` in the flow's
  history, so the Editor's History tab can undo it. It can't overwrite a flow you changed
  after it read it, and it never sees the values in your environment files.
- Runs go to the dashboard for that database (`miniflow serve`), in the same queue as your
  Run button. If none is running, the agent starts one and stops it when it exits.
- The agent can't install code. It can draft a block; you review it in the Editor under
  **Pending blocks** (or with `miniflow approve-block NAME --flows DIR`) and approve or
  discard it. An agent that can also run shell commands could write files itself, so for
  Claude Code the review is a check, and its own permission prompts are the guard.
- `--read-only` keeps only the tools that change nothing; `--no-runs` drops the run tools.

## Evaluation (spec §3.8)

```bash
uv run python -m benchmarks.run            # full run (a few minutes), writes docs/results/
uv run python -m benchmarks.run --quick    # small sizes for a quick check
```

Five experiments, each written to a CSV, plus figures and a summary in
[`docs/results/results.md`](docs/results/results.md):

1. **Speedup** over sequential execution on chain, fan-out, diamond and random layered
   DAGs (io- and cpu-bound), against the work/span bound and with Dask as a baseline.
2. **Worker choice**: one mixed io/cpu workload run with all threads, all processes, and
   MiniFlow's per-task routing.
3. **Scheduler overhead** per empty task as graphs grow to 5,000 tasks (in-memory vs
   SQLite-file store, and Dask).
4. **Fault tolerance**: run success rate and attempts under injected failure rates, with
   0, 1 and 3 retries, compared with the analytical expectation.
5. **Correctness**: every run above is checked against the state store for dependency
   order, skip propagation and retry budgets.

## Development

Uses [uv](https://docs.astral.sh/uv/). The core package has no runtime
dependencies; dev, test, benchmark and docs tooling live in dependency groups.

```bash
uv sync --all-groups --all-extras   # create .venv with everything
uv run pytest                        # tests (hypothesis + networkx oracles)
uv run pytest --cov                  # with coverage
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pre-commit install            # run the linters on every commit
uv run mkdocs serve                  # docs site
```

Optional extras: `miniflow[rich]` (live progress table), `miniflow[viz]`
(render the DAG to PNG/SVG; also needs the Graphviz `dot` program on PATH).
