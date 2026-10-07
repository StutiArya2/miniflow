# Phase 2 cloud-session prompts

Start each one as a separate session at claude.ai/code. Use the
`StutiArya2/miniflow` repo and an environment with **Trusted** network access and this setup script:

```bash
#!/bin/bash
pip install uv && uv sync --all-groups --all-extras
```

The three sessions are independent and can run at the same time.

---

## A: executors.py

> Read CLAUDE.md, then implement `miniflow/executors.py` exactly to the
> contract in its module docstring. Use a dispatch table keyed by
> `task.kind`. Pools are created lazily. The async loop runs in a daemon
> thread, and coroutines go through `asyncio.run_coroutine_threadsafe`.
> `shutdown()` is idempotent and stops the loop thread. Write
> `tests/test_executors.py`, covering: results from all three kinds, exceptions
> propagated through the Future, cpu tasks actually running in another PID,
> many concurrent io and async tasks, shutdown twice, use as a context manager,
> and the spawn start method (`multiprocessing.get_context("spawn")`). Put cpu
> worker functions in a module-level helper file such as `tests/_flows.py`.
> ruff, mypy and pytest must pass. Work on branch `phase2/executors` and open a PR to main.

## B: retry.py

> Read CLAUDE.md, then implement `miniflow/retry.py` exactly to the contract
> in its module docstring: `RetryPolicy` (frozen dataclass), `delay()`,
> `should_retry()`, `TaskTimeoutError` and the `deadline()` context manager.
> Write `tests/test_retry.py` with hypothesis properties: delay is never
> negative, never exceeds `cap * (1 + jitter)`, does not decrease with the
> attempt number when jitter is 0, and is reproducible with a seeded rng.
> Also check `should_retry` at the attempt boundary and that the deadline
> expires. ruff, mypy and pytest must pass. Work on branch `phase2/retry` and open a PR to main.

## C: store.py

> Read CLAUDE.md, then implement `miniflow/store.py` exactly to the contract
> in its module docstring: SQLite in WAL mode, one connection guarded by a
> lock, results pickled, and dataclasses for `AttemptRecord` and `RunRecord`.
> Use the `State` enum from `miniflow/states.py`. Write `tests/test_store.py`,
> covering: run creation, state updates, an attempts log, a save/load_completed
> round trip, latest_run, reopening a file database (resume after a restart),
> concurrent writes from 8 threads, and `:memory:`. ruff, mypy and pytest
> must pass. Work on branch `phase2/store` and open a PR to main.
