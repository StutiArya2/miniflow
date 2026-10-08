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
  "on_failure": {"block": "log", "settings": {"message": "failed at {{ failure.step }}", "level": "error"}}
}
```

- **Placeholders** fill settings in when the step runs: `{{ step }}` (a step's result, which
  also makes this step wait for it), `{{ step.field.0 }}`, `{{ rows.count }}`,
  `{{ params.name }}` and `{{ env.NAME }}` (secrets come only from environment variables).
- **14 built-in blocks**: `http_request`, `read_csv`, `write_csv`, `read_json`, `write_json`,
  `filter`, `select`, `aggregate`, `sort`, `shell`, `sql`, `email`, `log`, `wait`.
  `miniflow blocks` lists them and `miniflow blocks filter` describes one.
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

Use the **Editor** button in the dashboard to open a visual canvas. Add blocks from the
searchable palette, connect them, edit settings, validate and save the JSON flow, then run it.
Select a node and choose **Run up to selected** to execute just that node and its dependencies.
MiniFlow reuses results from the latest run when their step configuration, referenced parameters,
environment values, code, local file inputs and upstream results still match. A changed input
reruns the affected branch; each partial run appears in normal run history.
The equivalent CLI command is `miniflow run examples/sales_report.json --until by_region --reuse latest`.
The **Flow tests** panel accepts sample results for selected steps and expected results for
downstream steps. Tests run only the needed branch and leave the main run database untouched.
A step with `"flow": "parts/clean.json"` can reuse another JSON flow; its declared `params`
become settings, and its `outputs` become the result of that step. Inner steps remain visible
in the run graph.

For unattended local runs, add a schedule in the editor's **Schedules** panel and start
`miniflow serve --flows examples --db miniflow.db`. A flow can have named interval schedules
(`{"name": "frequent", "interval_seconds": 60}`) or daily local-time schedules
(`{"name": "morning", "daily": "09:00"`). The dashboard shows each next run and records
launched, missed and failed launches. Scheduled runs appear in the same history and inspection
views as manual runs. Keep the `serve` process running on the laptop for schedules to fire.

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
- **Story**: what the pipeline is for, what to watch for while it runs, and a short
  "How to read this page" guide.
- **What this run found**: the results of the pipeline's key steps in plain words, for example the
  city ranking, the alerts sent or revenue by country.
- **Step details**: what each step does, what it produced, every attempt and, for a failure, the
  reason in one line (the Python traceback is folded away).

A local web page at `http://127.0.0.1:8765` (the next free port if that one is taken), built
only on `http.server`, Server-Sent Events and plain JavaScript/SVG. It shows:

- **Graph**: tasks laid out by dependency depth and coloured by state; the shape shows the
  worker kind (box = io, stacked box = cpu, pill = async). Running tasks pulse and edges into
  skipped tasks are dashed.
- **Timeline**: one lane per worker thread, process or event loop, one bar per attempt, with
  dotted lines for retry backoff, so the parallelism and the retries are visible.
- **Task details**: state, attempts with the worker that ran each one, the full traceback, and
  upstream and downstream links.
- **Summary and runs**: progress, elapsed time, parallel speedup (task time ÷ wall time), retries,
  and every run in the database. *Follow latest* jumps to new runs as they start.

With `--flows DIR`, a control strip at the top lets you pick a `.py` or `.json` flow in `DIR`,
set a **fault rate** (passed to the run as `MINIFLOW_FAIL_RATE`) and press **Run**. While it runs
you can **Cancel** it, which interrupts it like Ctrl+C, so it ends INTERRUPTED. A failed or
interrupted run gets a **Resume this run** button, which reruns only its unfinished tasks. Each
run is a separate `miniflow run`/`resume` process writing to the same database, and one runs at
a time. Stopping the dashboard cancels the run it started.

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
