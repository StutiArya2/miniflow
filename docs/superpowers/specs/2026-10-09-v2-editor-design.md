# V2: Editor 2.0

Status: built. Builds on the V1 editor (`miniflow/ui/static/editor.js`, `miniflow/ui/author.py`).

## Features

| Feature | Behaviour |
|---|---|
| Undo / redo | Every change is undoable (add, delete, move, connect, settings, rename, paste, tidy, restore); 100 steps; Ctrl/Cmd+Z, Ctrl/Cmd+Shift+Z or Ctrl+Y |
| Selection, copy, paste | Click, Shift-click, Shift-drag a box; Ctrl/Cmd+C/V/D; Delete; Esc clears. Pasted steps get unique names; `{{ step }}` references and `after` among pasted steps follow the renames |
| Drag and drop | Palette blocks and reusable flows are draggable (HTML5 drag and drop); dropping on the canvas adds the step centred on the pointer, using the zoom/pan transform; clicking still adds at the next free slot |
| Zoom and pan | Wheel zooms at the pointer (25–200 %), drag empty space pans, −/+/Fit buttons; a CSS transform, so saved positions don't change |
| Tidy up | `POST /api/layout` returns columns and rows from `ui/layout.py` (the run graph's layout); undoable |
| Version history | Each save keeps the replaced version in `.history/<flow>/<id>.json` with time, OS account and optional note; list, diff against the current file, restore |
| Run data on the canvas | Pick a run of the flow (latest by default): step state and item count, item counts on arrows, result sample and error in the settings panel; steps changed since the run, and their descendants, greyed out |
| Live checking | The flow is validated ~0.7 s after each change, so problem markers and signatures stay current |

## API additions

| Request | Result |
|---|---|
| `POST /api/flows/save` | accepts an optional `"message"` |
| `POST /api/flows/validate` | also returns `"signatures"`: settings-only identity per step |
| `GET /api/flows/<name>/versions` | `[{id, saved_at, user, message, steps}]`, newest first |
| `GET /api/flows/<name>/versions/<id>` | that version's flow document |
| `GET /api/flows/<name>/versions/<id>/diff` | `{added, removed, changed: {step: [{field, old, new}]}, flow: [...]}` from that version to the current file |
| `POST /api/flows/restore` | `{"name", "version"}`; the replaced version is kept |
| `POST /api/layout` | `{"graph": {"tasks": {name: {"after": [...]}}}}` → `{name: {col, row}}` |

All POSTs use the dashboard's existing checks (Host/Origin, JSON body, size limit) and need
`--flows`.

## Signatures

`signature.config_signature(task)` hashes code, settings, referenced parameters and
environment, without file contents, and is never `None`. The CLI passes it to the scheduler
(`config_signatures=`), which records it in the run graph as `config_signature`. The editor
compares it with the current one; runs made before V2 have none, so nothing is marked stale for
them. Partial-run reuse still uses the strict `step_signature`.

## Not in V2

Collaborative editing, flow history across machines (use git for that), undo across page
reloads, data previews for Python flows.
