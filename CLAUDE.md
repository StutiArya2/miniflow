# MiniFlow: notes for Claude sessions

The spec is `docs/MiniFlow_Project_Document.pdf`. The build plan is in `docs/PLAN.md`.

## Hard rules
- **The core package `miniflow/` is standard-library only** (spec §3.1). Never add to
  `[project].dependencies`. Optional features go in `[project.optional-dependencies]`
  and must be imported lazily, with a fallback when they are missing.
- Python ≥ 3.11. Full type hints; `uv run mypy` (strict) must pass.
- Task states and transitions live in `miniflow/states.py`. Don't invent new states.
- Module responsibilities follow spec §3.4. Each stub's module docstring is its
  **interface contract**. Implement exactly that API; ask before changing it.
- `cpu` tasks are sent to worker processes **by reference** (`Task.__reduce__`).
  Never pickle `task.fn` directly.
- Calling convention: `fn(*upstream_results_in_after_order, *task.args, **task.kwargs)`.

## Workflow
- Tooling is uv: `uv sync --all-groups --all-extras`, then `uv run pytest`,
  `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`.
- Every module gets `tests/test_<module>.py`. Use hypothesis for invariants and
  keep each test under the 60 s pytest-timeout.
- Process-pool code must work under the **spawn** start method (Windows CI).
  Define worker functions at module level, never in tests' local scope.
- One branch per phase or module; open a PR to `main`. CI must be green.
