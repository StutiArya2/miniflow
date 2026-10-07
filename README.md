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
followed by its own `args`/`kwargs`.

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
| `executors.py`  | ⏳ Phase 2 |
| `retry.py`      | ⏳ Phase 2 |
| `store.py`      | ⏳ Phase 2 |
| `scheduler.py`  | ⏳ Phase 3 |
| `cli.py`        | 🟡 `validate`, `graph` done; `run`, `resume`, `status` in Phase 3 |

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

CLI:

```bash
uv run miniflow validate examples/pipeline.py
uv run miniflow graph examples/pipeline.py -o pipeline.dot
```

Optional extras: `miniflow[rich]` (live progress table), `miniflow[viz]`
(render the DAG to PNG/SVG), `miniflow[cloudpickle]` (lambdas as cpu tasks).
