# V3 milestone 3: loops, branches and step failure paths

**Status:** design for Megha and Stuti to review before changes to `scheduler.py`,
`store.py` and the run graph interface. This follows milestone 2 on `v3-runs`.

## What a flow author can build

The editor adds **For each**, **If / else** and **On failure** elements. Their JSON
forms are written here so a flow remains portable and editable without the UI.

### For each

```json
{
  "steps": {
    "tiles": {"block": "read_json", "settings": {"path": "tiles.json"}},
    "process_tile": {
      "for_each": "{{ tiles }}",
      "block": "filter",
      "settings": {"rows": "{{ item }}", "where": "cloud_cover < 20"},
      "parallel": 8,
      "retries": 2
    },
    "save": {"block": "write_json", "settings": {
      "path": "processed.json", "data": "{{ process_tile }}"
    }}
  }
}
```

`for_each` resolves to a list at run time. `item` and `item_index` are available
only within that loop's settings. The loop runs one configured block per item, or
one referenced subflow if `flow` replaces `block`. Its result is a list in source
order, even when items finish out of order. Empty input returns `[]`. `parallel`
is a positive per-loop cap; worker-kind pool limits still apply. A configurable
hard maximum (default 10,000 items) refuses accidental unbounded expansion.

Each item appears in the run graph with its own attempts, result, preview and
error. The original `process_tile` box is the join point that downstream steps
wait for. If an item fails after retries, the loop fails; unfinished siblings
may finish, and ordinary downstream steps are skipped. A later Resume reuses
successful items whose input and configuration still match.

### If / else

```json
{
  "steps": {
    "has_sales": {
      "if": "{{ report.count }} > 0",
      "then": [
        {"name": "publish", "block": "write_json", "settings": {
          "path": "report.json", "data": "{{ report }}"
        }}
      ],
      "else": [
        {"name": "empty", "block": "log", "settings": {
          "message": "No sales today"
        }}
      ]
    }
  }
}
```

The condition uses the existing safe expression evaluator; no Python `eval`.
Each arm is an ordered list of named steps, connected in that order. A subflow
can express a more complex arm. The chosen arm runs; the other arm's steps are
recorded as `SKIPPED` with reason `condition`. The `has_sales` result is the
last chosen step's result (or `null` for an empty arm). A condition skip does
not turn an otherwise successful flow into `FAILED`.

### Per-step failure path

```json
{
  "steps": {
    "download": {
      "block": "http_request",
      "settings": {"url": "https://example.invalid/data"},
      "retries": 2,
      "on_failure": "alert_download"
    },
    "alert_download": {
      "block": "slack_message",
      "settings": {
        "webhook_url": "{{ env.SLACK_HOOK }}",
        "text": "{{ failure.step }}: {{ failure.error }}"
      }
    }
  }
}
```

After `download` exhausts retries, `alert_download` runs once with the existing
`failure.step` and `failure.error` context. Its other normal dependencies must
already be complete. Normal descendants of the failed step are skipped; the
failure path itself remains runnable. The run is `FAILED` even if the alert
succeeds. This keeps alerts from silently converting lost data into success.
The existing flow-level `on_failure` action still runs at the end. The editor
draws the failure connection in orange and explains both actions.

## Scheduler and store interface proposal

The current `DAG` is immutable and the scheduler creates every task row before
execution. Keep that public DAG contract. Inside one run, the scheduler owns a
mutable execution graph copied from the validated DAG. Add a private expansion
callback for JSON `for_each` source tasks; Python DAG users do not need it.

1. The compiler creates two static tasks for a loop: `loop.__items` evaluates
   the list after all referenced upstream steps; `loop` collects item results.
   The collector initially depends on `loop.__items`.
2. When `loop.__items` succeeds, the scheduler validates the list length, adds
   one item task per element, and extends the collector's dependencies **before**
   releasing the source task. This prevents the collector from starting early.
   Item tasks use the existing `run_step` or subflow runner by reference, so
   process-pool spawning continues to work on Windows.
3. `StateStore.add_tasks(run_id, tasks, graph_updates)` writes each `PENDING`
   task row and its graph metadata in one SQLite transaction. The graph metadata
   carries `group`, `item_index`, `item_key` and `edge_kind`. The store remains
   readable during a run; the dashboard refreshes graph/layout when its revision
   changes. Each item then uses the existing state and attempt recording paths.
4. Scheduler maps (`tasks`, `upstream`, `downstream`, `waiting`, `state`,
   `attempts`, ready heaps and ranks) gain the item tasks atomically in the
   scheduler thread. A per-loop in-flight counter enforces `parallel`; ready
   items from other loops can still dispatch while one loop reaches its cap.
5. An item key is a hash of canonical item JSON plus a duplicate ordinal.
   The loop's item signature also includes the block/subflow configuration,
   environment and upstream signatures. Resume reconstructs the current items
   and reuses a previous successful item only when that signature matches.
   Result collection follows current input order, not completion order.

Branches use static compiled tasks plus a guard on each arm. The scheduler
evaluates the guard after the condition task succeeds and marks only the
unchosen arm `SKIPPED` with reason `condition`. The branch join waits for the
chosen arm, then releases ordinary downstream tasks. `RunResult` and the store
keep the existing state enum; a skip reason distinguishes a normal unchosen
branch from failure propagation when computing final run status.

Failure paths add a special edge kind from a step to its handler. On final
failure, the scheduler makes that handler ready after its other dependencies,
then skips ordinary descendants. The handler gets a read-only failure context.
The source step stays `FAILED`, so final status remains `FAILED`.

## Bounds and compatibility

- No user-supplied code is evaluated for conditions. The existing AST whitelist
  checks them when the flow is saved.
- Dynamic item names are internal and deterministic; user step names cannot
  collide with the reserved `.__items` and `[item-key]` forms.
- An expansion error becomes a normal failed task with a clear explanation.
  Items must be JSON-serializable so their resume keys stay stable. The maximum
  item count and `parallel` are checked before dispatch.
- Existing JSON flows and Python DAGs keep their behavior. Old databases can
  be opened; new graph metadata is optional for old runs.
- A flow test may supply the loop's source result and assert selected item
  outputs. Partial runs include the whole loop when a downstream step needs it.
- The run graph groups the item boxes inside the loop element; expanding a group
  shows each item and its attempts. At large counts it initially shows a
  summary and pages the item nodes so the browser stays responsive.

## Acceptance demo and checks

1. Use the editor to build a 240-item tile flow with `parallel: 8`, save and run
   it offline. The graph shows item progress, parallel attempts, item counts and
   ordered output.
2. Make one item fail once, then succeed on retry. Make another fail after all
   retries; its error and the skipped downstream step are visible. Resume after
   fixing it and confirm successful items are reused individually.
3. Run an if/else flow with both true and false sample inputs. Confirm the
   unchosen arm is `SKIPPED` and both successful runs remain `SUCCESS`.
4. Fail a step with an `on_failure` path. Confirm its alert runs, normal
   descendants skip, and the run remains `FAILED`.
5. Run the same cases on Ubuntu and Windows (Python 3.11 and 3.13), with strict
   mypy, Ruff and the repository's 90% coverage gate.

**Review request:** approve the mutable run-graph and `StateStore.add_tasks`
interfaces, the per-item resume key, and the rule that a failure handler does
not make a failed flow successful. These are the three decisions that change
the scheduler/store contract.
