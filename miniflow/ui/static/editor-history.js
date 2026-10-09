/* MiniFlow Studio: undo/redo, multi-select and copy/paste.
 *
 * Every change to Studio.flow ends with studioCommit(), which records a snapshot of the
 * flow (as JSON text) after the change. Undo and redo move through those snapshots, so
 * any kind of change is undoable without special code per action.
 */
"use strict";

const StudioHistory = { states: [], index: -1, limit: 100 };

function studioSnapshot() {
  return JSON.stringify(Studio.flow);
}

/* Start a fresh history for the flow now on screen (open, new, starter, restore). */
function studioHistoryReset() {
  StudioHistory.states = [studioSnapshot()];
  StudioHistory.index = 0;
  Studio.multi = new Set();
  studioHistoryButtons();
}

/* Record the change just made to Studio.flow. Identical snapshots are skipped. */
function studioCommit() {
  const snap = studioSnapshot();
  Studio.dirty = true;
  if (snap === StudioHistory.states[StudioHistory.index]) return;
  StudioHistory.states = StudioHistory.states.slice(0, StudioHistory.index + 1);
  StudioHistory.states.push(snap);
  if (StudioHistory.states.length > StudioHistory.limit) StudioHistory.states.shift();
  StudioHistory.index = StudioHistory.states.length - 1;
  studioHistoryButtons();
  if (typeof studioScheduleCheck === "function") studioScheduleCheck();
}

function studioUndoRedo(step) {
  const target = StudioHistory.index + step;
  if (target < 0 || target >= StudioHistory.states.length) return;
  StudioHistory.index = target;
  Studio.flow = JSON.parse(StudioHistory.states[target]);
  Studio.dirty = true;
  if (Studio.selected && !(Studio.selected in Studio.flow.steps)) Studio.selected = null;
  Studio.multi = new Set([...Studio.multi].filter((name) => name in Studio.flow.steps));
  studioHistoryButtons();
  studioRender();
  studioMessage(step < 0 ? "Undone." : "Redone.");
  if (typeof studioScheduleCheck === "function") studioScheduleCheck();
}

function studioHistoryButtons() {
  const undo = document.getElementById("studio-undo");
  const redo = document.getElementById("studio-redo");
  if (undo) undo.disabled = StudioHistory.index <= 0;
  if (redo) redo.disabled = StudioHistory.index >= StudioHistory.states.length - 1;
}

/* ------------------------------------------------------------------ selection */
function studioSelection() {
  const names = new Set(Studio.multi || []);
  if (Studio.selected) names.add(Studio.selected);
  return [...names].filter((name) => name in Studio.flow.steps);
}

function studioToggleSelect(name, additive) {
  if (!additive) {
    Studio.multi = new Set();
    Studio.selected = name;
  } else if (studioSelection().includes(name)) {
    Studio.multi.delete(name);
    if (Studio.selected === name) Studio.selected = [...Studio.multi][0] || null;
  } else {
    Studio.multi.add(name);
    Studio.selected = name;
  }
  studioRender();
}

/* ------------------------------------------------------------- copy and paste */
const STEP_REF = /\{\{\s*([A-Za-z_]\w*)(?=[\s.}])/g;

/* Replace {{ old... }} with {{ new... }} inside any setting value, for the names in `map`. */
function studioRenameRefs(value, map) {
  if (typeof value === "string") {
    return value.replace(STEP_REF, (whole, name) => (name in map ? whole.replace(name, map[name]) : whole));
  }
  if (Array.isArray(value)) return value.map((item) => studioRenameRefs(item, map));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, studioRenameRefs(v, map)]));
  }
  return value;
}

function studioUniqueName(base, taken) {
  const stem = base.replace(/_\d+$/, "") || "step";
  let name = stem;
  let n = 2;
  while (taken.has(name)) name = `${stem}_${n++}`;
  return name;
}

/* Paste `steps` (name -> step) as new steps with unique names and shifted positions.
 * References between the pasted steps follow the renames; others are left alone. */
function studioPasteSteps(steps, offset = 40) {
  const taken = new Set(Object.keys(Studio.flow.steps));
  const map = {};
  for (const name of Object.keys(steps)) {
    map[name] = studioUniqueName(name, taken);
    taken.add(map[name]);
  }
  for (const [old, step] of Object.entries(steps)) {
    const copy = JSON.parse(JSON.stringify(step));
    copy.settings = studioRenameRefs(copy.settings || {}, map);
    if (copy.after) copy.after = copy.after.map((dep) => map[dep] || dep);
    const pos = copy.position || { x: 90, y: 90 };
    copy.position = { x: (Number(pos.x) || 90) + offset, y: (Number(pos.y) || 90) + offset };
    Studio.flow.steps[map[old]] = copy;
  }
  Studio.multi = new Set(Object.values(map));
  Studio.selected = Object.values(map)[0] || null;
  return map;
}

function studioCopy() {
  const names = studioSelection();
  if (!names.length) { studioMessage("Select one or more steps to copy.", true); return; }
  Studio.clipboard = JSON.stringify(Object.fromEntries(names.map((n) => [n, Studio.flow.steps[n]])));
  studioMessage(`Copied ${names.length} step${names.length > 1 ? "s" : ""}.`);
}

function studioPaste() {
  if (!Studio.clipboard) { studioMessage("Nothing copied yet.", true); return; }
  const map = studioPasteSteps(JSON.parse(Studio.clipboard));
  studioCommit();
  studioRender();
  studioMessage(`Pasted ${Object.values(map).join(", ")}.`);
}

function studioDuplicate() {
  studioCopy();
  if (Studio.clipboard) studioPaste();
}

function studioDeleteSelected() {
  const names = studioSelection();
  if (!names.length) return;
  for (const name of names) delete Studio.flow.steps[name];
  for (const other of Object.values(Studio.flow.steps)) {
    if (other.after) other.after = other.after.filter((dep) => !names.includes(dep));
  }
  Studio.selected = null;
  Studio.multi = new Set();
  studioCommit();
  studioRender();
  studioMessage(`Deleted ${names.join(", ")}. Undo with Ctrl+Z.`);
}

/* --------------------------------------------------------------- keyboard */
function studioTyping(target) {
  return target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement
    || target instanceof HTMLSelectElement || target?.isContentEditable;
}

document.addEventListener("keydown", (event) => {
  if (!Studio.visible || studioTyping(event.target)) return;
  const mod = event.ctrlKey || event.metaKey;
  const key = event.key.toLowerCase();
  if (mod && key === "z") { event.preventDefault(); studioUndoRedo(event.shiftKey ? 1 : -1); }
  else if (mod && key === "y") { event.preventDefault(); studioUndoRedo(1); }
  else if (mod && key === "c") { event.preventDefault(); studioCopy(); }
  else if (mod && key === "v") { event.preventDefault(); studioPaste(); }
  else if (mod && key === "d") { event.preventDefault(); studioDuplicate(); }
  else if (key === "delete" || key === "backspace") { event.preventDefault(); studioDeleteSelected(); }
  else if (key === "escape") { Studio.multi = new Set(); Studio.selected = null; studioRender(); }
});
