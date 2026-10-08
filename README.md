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

## Real-world examples

Four pipelines ship with MiniFlow. Each does a job people recognise, with **live public data**:

| Example | What it does | Data |
|---|---|---|
| `earthquakes` | Live earthquake monitor: most active regions, alerts for magnitude 5+ | US Geological Survey live feeds |
| `air_quality` | Air quality and best time to exercise outside for 8 Indian cities, with health alerts | Open-Meteo air-quality and weather forecasts |
| `news_digest` | Morning digest of the Hacker News front page: reading time and topics of each article | Hacker News API + the articles themselves |
| `shop_report` | An online shop's monthly sales in 7 currencies, converted to rupees, unusual days flagged | European Central Bank exchange rates (orders are sample data) |

```bash
miniflow examples                          # list them
miniflow example air_quality --ui          # run one and watch it in the dashboard
miniflow example news_digest --offline     # no internet: replay saved snapshots of real data
```

Reports and simulated alert or email messages are written to `miniflow_reports/`.
`--offline` replays snapshots recorded from a real run, including its failures (a website
that never answered fails the same way), so demos work without a network connection.

## Dashboard

```bash
miniflow ui                                # open the dashboard: Examples gallery + every run
miniflow run examples/faulty_etl.py --ui   # run with the dashboard open; it stays up until Ctrl+C
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

Options: `--port`, `--no-browser`, `--no-ui-wait` (close the dashboard when the run ends),
`--read-only` and `--offline` for `miniflow ui`. The server only binds to `127.0.0.1`. Its one
write action is the gallery's Run button, which can start only the bundled examples, one at a
time, and only from the dashboard's own page (other websites are refused). `--read-only`
hides it; the dashboard started by `run --ui` is always read-only.

**Optional steps:** `@task(optional=True)` marks a step the job can finish without. If it fails
for good, the steps after it still run and receive `None` in its place, and the run still counts
as a success. The news digest uses this so one slow website can't cancel the whole email.

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
