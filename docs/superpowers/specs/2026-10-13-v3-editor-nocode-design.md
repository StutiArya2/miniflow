# V3: no-code editor additions

Status: built on `v3-editor-nocode`. Builds on Editor 2.0 (`2026-10-09-v2-editor-design.md`).
Everything here can now be done in the editor without editing the JSON file. The flow format,
`scheduler.py`, `store.py`, `graph.py` and `executors.py` are unchanged.

## Features

| Feature | Behaviour |
|---|---|
| Rename a step | "Step name" in the settings panel. References follow: `{{ step }}` in other steps' settings, `after`, `outputs`, the `on_success`/`on_failure` settings and the flow tests' `inputs`/`expect`. Names are checked (letters, digits, `_`; unique; not `params`, `env` or `failure`). Undoable. |
| Run options | "Run options" in the settings panel: retries, timeout (seconds) and where a step runs (thread or separate process; async blocks stay async). An empty value removes the key, so files stay minimal. The server already checks these. |
| Connections | "Runs after" shows the step's dependencies as chips. A drawn one (`after`) has × to remove it; one that comes from a `{{ step }}` value is dashed and explains which setting creates it. Wires can be clicked: drawn wires are removed with Delete or "Remove connection"; dashed (inferred) wires explain themselves instead. |
| When the flow ends | A drawer with three parts: the success action and the failure action (any block, with its settings form), and named outputs. The failure action's "Use value from…" offers the `failure.*` values and parameters only, because step results aren't available there. |
| Problem list | Validation problems appear under the canvas. A problem about a step is a link that selects the step and centres it. |
| Palette groups | `/api/blocks` gives each block a `group`, taken from the built-in module it is defined in. Plugin blocks are grouped as "Plugins". Search matches group names too. |
| Quick add | A step's `+` port opens a search popover. The blocks this flow already uses come first. Picking one adds it to the right of the step, already connected. "Connect to an existing step…" keeps the old drag-free connect. |

## API change

`GET /api/blocks`: each block also has `"group"`. `miniflow.ui.author.block_group(ref)` maps
`miniflow.blocks.builtin.<module>:…` to a heading and anything else to `"Plugins"`.

## Not included

Loops and branches (see the V3 smart-flows design, pending review), editing reusable flows in
place, and per-step failure paths.
