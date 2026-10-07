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
| extra `miniflow[cloudpickle]` | cloudpickle | Optional: lets `cpu` tasks be lambdas or closures; default stays the stdlib pickle check from §3.3 |

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

### Phase 4: evaluation (§3.8)
- Build the benchmarks in a cloud session.
- **Run the final numbers locally** on the fixed 8-core machine so the speedup and Amdahl comparison is reproducible. Cloud VM core counts vary.
- Output CSVs and matplotlib plots (dev-only dependency) in `docs/results/` for the report.

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
- Benchmarks produce speedup and overhead plots.
