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
| benchmarks      | ⏳ Phase 4 |
| `ui/` dashboard | ⏳ Phase 3.5: live graph, timeline, runs table (`miniflow ui`) |

## Running flows

```bash
miniflow run examples/pipeline.py          # live table (with the rich extra) or line-by-line log
miniflow status                            # tasks of the latest run
miniflow status --all                      # every run in miniflow.db
miniflow resume examples/faulty_etl.py     # rerun, reusing tasks that already succeeded
miniflow graph examples/faulty_etl.py --render svg -o flow.svg   # coloured by the latest run
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
