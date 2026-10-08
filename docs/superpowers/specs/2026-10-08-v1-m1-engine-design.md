# V1 milestone 1: no-code engine

Part of the [V1 roadmap](2026-10-08-v1-roadmap.md). Status: built.

## Flow file

A flow is a `.json` file. Top-level fields: `description`, `params`, `steps` (required),
`on_failure`. The step name is the key under `steps`; it becomes the task name.

| Step field | Meaning |
|---|---|
| `block` | Which block the step runs (required) |
| `settings` | The block's settings; any value may contain placeholders |
| `after` | Extra steps to wait for without using their data |
| `retries`, `timeout` | As for Python tasks |
| `kind` | Override the block's worker type (`io`/`cpu`; `async` only for async blocks) |
| `note`, `position` | For people and, later, the editor; ignored when running |

Step names: letters, digits and `_`, not starting with a digit; `params`, `env` and
`failure` are reserved.

## Placeholders

| Placeholder | Value |
|---|---|
| `{{ step }}` | The step's result; the step also becomes a dependency |
| `{{ step.key.0.key }}` | Dictionary keys and list positions |
| `{{ rows.count }}`, `.first`, `.last` | On lists |
| `{{ params.name }}`, `{{ params }}` | Flow parameters |
| `{{ env.NAME }}` | An environment variable (`{{ env }}` alone is refused) |
| `{{ failure }}`, `{{ failure.step/error/failed/skipped/run_id }}` | Only in `on_failure` |

A setting that is exactly one placeholder receives the value itself; inside longer text
the value is inserted as text (JSON for lists and objects). Placeholders work inside
lists and objects.

## Parameters and environments

`"params": {"min": {"type": "float", "default": 100, "help": "..."}}` with types `str`,
`int`, `float`, `bool`, `list`, `dict`. Value order: `--param name=value`, then the
environment file, then `default`; a parameter without any is a problem. `--environment
prod` reads `environments/prod.json` (values) and `environments/prod.env` (`NAME=value`
lines, added to the environment unless already set) next to the flow file.

## Blocks

`@block(name, settings=[Setting(...)], kind=...)` registers a module-level function; it
receives its settings as keyword arguments and returns JSON-like data. `Setting(name,
type, default=REQUIRED, help, choices, secret, file)`: `secret` settings must be a single
`{{ env.NAME }}` and are masked as `••••` in errors; relative paths in `file` settings are
resolved against the flow file's folder. `*.py` files in a `blocks/` folder next to the
flow (or given with `--blocks DIR`) are loaded as plugins; names starting with `_` are
skipped.

| Group | Block | Main settings | Kind |
|---|---|---|---|
| data | `http_request` | url, method, headers, query, body, timeout, fail_on_error | io |
| | `read_csv` / `write_csv` | path, delimiter; convert_numbers / rows, columns | io |
| | `read_json` / `write_json` | path; data, indent | io |
| transform | `filter` | rows, where (safe condition language) | cpu |
| | `select` | rows, fields (list, or {new: old}) | cpu |
| | `aggregate` | rows, metrics ({name: "sum(field)"}), group_by, decimals | cpu |
| | `sort` | rows, by, descending | cpu |
| systems | `shell` | command (no shell features), cwd, timeout, check, env | io |
| | `sql` | database (SQLite), query, parameters, many | io |
| notify | `email` | to, subject, body, host, port, username, password (secret), tls | io |
| | `log` | message, level | io |
| | `wait` | seconds | async |

Conditions (`filter`) allow field names, `row['name with spaces']`, constants, lists,
comparisons, `in`, `and`/`or`/`not`, `+ - * / %` and `lower upper len abs round str int
float`. They are parsed with `ast` and evaluated by walking the tree; nothing is `eval`ed.

## How it runs

`load_json_flow` checks everything first and raises `FlowFileError` listing every
problem. It then builds one `Task` per step whose function is `run_step` (or
`run_step_async`) with the step's block name, block reference, raw settings, upstream
names, parameter values and the flow folder as kwargs. At run time the step fills in its
placeholders from the upstream results, checks each value against its `Setting`, calls the
block and masks secrets in errors. The scheduler, store, executors and graph are unchanged.

`on_failure` runs in the CLI after a FAILED run (dashboard runs go through the CLI too).

## Command line and dashboard

- `run`, `resume`, `validate`, `graph` accept `.json` flows, with `--param NAME=VALUE`,
  `--environment NAME` and `--blocks DIR`.
- `miniflow blocks` lists blocks; `miniflow blocks NAME` describes one.
- `miniflow ui --flows DIR` lists `.json` flows next to `.py` ones.

## Example

`examples/sales_report.json`: 40 orders in three currencies → plugin `convert_currency`
(`examples/blocks/currency.py`) → `filter` by a parameter → `aggregate` per region → `sort`
→ `write_csv` and SQLite (`sql` with `many`) → `log` summary; `on_failure` logs the error;
`environments/dev.json` and `prod.json` change the minimum.

## Not in this milestone

The dashboard doesn't yet show step results or offer parameters and environments when
starting a run (milestones 4 and 6). No conditional branching or loops (V2).
