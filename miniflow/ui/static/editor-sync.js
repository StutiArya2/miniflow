/* MiniFlow Studio: show saves made outside this editor on the canvas, live.
 *
 * Claude (in the chat pane or over MCP), another tab or a text editor can save the flow
 * that is open here. The editor polls the saved file's version; when it changes, the
 * canvas loads the new version in place and marks what changed: new steps and
 * connections in green, changed steps in amber, removed ones as dashed red ghosts.
 * Loading it is one undo step, so Ctrl+Z puts the previous version back.
 * With unsaved edits on screen nothing is replaced until the person chooses.
 */
"use strict";

const StudioSync = {
  name: "",         // flow whose saved version is tracked
  version: null,    // last saved version the editor has seen
  base: null,       // that saved flow, to compare the next save with
  changes: null,    // what the last outside save changed, drawn on the canvas
  author: "",
  pending: null,    // {flow, version, author} waiting while there are unsaved edits
  known: null,      // flow names seen while Claude works, to open a flow it creates
  busy: false,
  gen: 0,           // bumped by the editor's own saves so stale polls are dropped
};
const STUDIO_SYNC_MS = 1200;

const studioClone = (value) => JSON.parse(JSON.stringify(value ?? { steps: {} }));
const studioSame = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/* "from→to" for every connection drawn on the canvas: "after" plus {{ step }} uses. */
function studioFlowEdges(flow) {
  const steps = flow?.steps || {}, edges = new Set();
  for (const [name, step] of Object.entries(steps)) {
    const used = [...JSON.stringify(step?.settings || {}).matchAll(/\{\{\s*([A-Za-z_]\w*)/g)].map((m) => m[1]);
    for (const dep of [...(step?.after || []), ...used]) {
      if (dep !== name && dep in steps) edges.add(`${dep}→${name}`);
    }
  }
  return edges;
}

/* Which fields of a step differ, ignoring where it sits on the canvas. */
function studioStepFields(a, b) {
  const fields = [];
  for (const key of new Set([...Object.keys(a || {}), ...Object.keys(b || {})])) {
    if (key === "position") continue;
    if (key === "settings") {
      const sa = a?.settings || {}, sb = b?.settings || {};
      for (const s of new Set([...Object.keys(sa), ...Object.keys(sb)])) {
        if (!studioSame(sa[s], sb[s])) fields.push(s);
      }
    } else if (!studioSame(a?.[key], b?.[key])) fields.push(key);
  }
  return fields;
}

/* Differences between two flow documents, in the terms the canvas draws. */
function studioFlowChanges(before, after) {
  const a = before?.steps || {}, b = after?.steps || {};
  const changed = new Map();
  for (const name of Object.keys(b)) {
    if (!(name in a)) continue;
    const fields = studioStepFields(a[name], b[name]);
    if (fields.length) changed.set(name, fields);
  }
  const ea = studioFlowEdges(before), eb = studioFlowEdges(after);
  // the editor fills in empty tests, params and so on; empty and missing are the same
  const blank = (value) => value === undefined || value === "" || studioSame(value, []) || studioSame(value, {});
  const flowFields = [...new Set([...Object.keys(before || {}), ...Object.keys(after || {})])]
    .filter((key) => key !== "steps" && !studioSame(before?.[key], after?.[key])
      && !(blank(before?.[key]) && blank(after?.[key])));
  return {
    added: new Set(Object.keys(b).filter((name) => !(name in a))),
    changed,
    removed: Object.keys(a).filter((name) => !(name in b)).map((name) => ({
      name, step: a[name], index: Object.keys(a).indexOf(name),
    })),
    addedWires: new Set([...eb].filter((edge) => !ea.has(edge))),
    removedWires: [...ea].filter((edge) => !eb.has(edge)),
    flow: flowFields,
  };
}

function studioChangeCount(changes) {
  if (!changes) return 0;
  return changes.added.size + changes.changed.size + changes.removed.length
    + changes.addedWires.size + changes.removedWires.length + changes.flow.length;
}

function studioChangeSummary(changes) {
  const parts = [];
  const count = (n, one, many = `${one}s`) => n && parts.push(`${n} ${n === 1 ? one : many}`);
  count(changes.added.size, "step added", "steps added");
  count(changes.changed.size, "step changed", "steps changed");
  count(changes.removed.length, "step removed", "steps removed");
  const wires = changes.addedWires.size + changes.removedWires.length;
  count(wires, "connection changed", "connections changed");
  if (changes.flow.length) parts.push(`${changes.flow.join(", ")} changed`);
  return parts.join(" · ") || "no visible changes";
}

/* Draw the last outside save's changes on the canvas (called by studioRender). */
function studioMarkChanges(positions) {
  studioSyncBanner();
  const changes = StudioSync.changes;
  if (!changes || StudioSync.name !== Studio.name) return;
  for (const node of document.querySelectorAll("#studio-nodes .studio-node")) {
    const name = node.dataset.step;
    const fields = changes.changed.get(name);
    if (!changes.added.has(name) && !fields) continue;
    node.classList.add(changes.added.has(name) ? "sync-added" : "sync-changed");
    const badge = document.createElement("span");
    badge.className = "studio-sync-badge";
    badge.textContent = changes.added.has(name) ? "new" : "changed";
    badge.title = fields ? `${StudioSync.author} changed: ${fields.join(", ")}` : `${StudioSync.author} added this step`;
    node.append(badge);
  }
  const all = { ...positions };
  const host = document.getElementById("studio-nodes");
  for (const gone of changes.removed) {
    if (gone.name in Studio.flow.steps) continue; // put back since (undo, paste)
    const pos = gone.step?.position || {};
    const spot = {
      x: Number(pos.x) || 90 + (gone.index % 4) * 250, y: Number(pos.y) || 90 + Math.floor(gone.index / 4) * 150,
    };
    // a step placed automatically may now sit where the ghost was: move the ghost below it
    const taken = () => Object.values(all).some((p) => Math.abs(p.x - spot.x) < 190 && Math.abs(p.y - spot.y) < 84);
    for (let tries = 0; tries < 20 && taken(); tries += 1) spot.y += 100;
    all[gone.name] = spot;
    const ghost = document.createElement("div");
    ghost.className = "studio-node sync-removed";
    ghost.setAttribute("aria-hidden", "true");
    ghost.style.left = `${all[gone.name].x}px`; ghost.style.top = `${all[gone.name].y}px`;
    ghost.title = `${StudioSync.author} removed this step`;
    const title = document.createElement("strong"); title.textContent = gone.name;
    const kind = document.createElement("small"); kind.textContent = "removed";
    ghost.append(title, kind);
    host.prepend(ghost); // under the live nodes
  }
  const svg = document.getElementById("studio-wires");
  for (const path of svg.querySelectorAll("path.studio-wire")) {
    if (changes.addedWires.has(`${path.dataset.from}→${path.dataset.to}`)) path.classList.add("sync-added");
  }
  for (const edge of changes.removedWires) {
    const [from, to] = edge.split("→");
    if (studioFlowEdges(Studio.flow).has(edge)) continue;
    const line = studioWire(from, to, all);
    if (!line) continue;
    line.setAttribute("class", "studio-wire sync-removed");
    svg.prepend(line);
  }
}

function studioSyncBanner() {
  const bar = document.getElementById("studio-sync");
  if (!bar) return;
  const text = document.getElementById("studio-sync-text");
  const show = (id, visible) => { document.getElementById(id).hidden = !visible; };
  const pending = StudioSync.pending && StudioSync.name === Studio.name;
  const changes = StudioSync.changes && StudioSync.name === Studio.name ? StudioSync.changes : null;
  bar.hidden = !pending && !changes;
  bar.classList.toggle("warn", !!pending);
  if (pending) {
    text.textContent = `${StudioSync.pending.author} saved a new version of ${Studio.name} while you have unsaved edits.`;
  } else if (changes) {
    text.textContent = `${StudioSync.author} updated ${Studio.name}: ${studioChangeSummary(changes)}.`;
  }
  show("studio-sync-load", !!pending);
  show("studio-sync-keep", !!pending);
  show("studio-sync-show", !pending && !!changes && (changes.added.size + changes.changed.size + changes.removed.length) > 0);
  show("studio-sync-clear", !pending && !!changes);
}

function studioSyncAuthor(state) {
  if (/\(agent\)\s*$/.test(state.message || "")) return "Claude";
  return "Another editor";
}

/* Put a saved version on the canvas and remember what it changed. */
function studioSyncApply(flow, version, author) {
  const before = StudioSync.base;
  const wasEmpty = !Object.keys(Studio.flow.steps || {}).length;
  StudioSync.changes = studioFlowChanges(before, flow);
  StudioSync.author = author;
  StudioSync.version = version;
  StudioSync.base = studioClone(flow);
  StudioSync.pending = null;
  Studio.flow = studioClone(flow);
  if (Studio.selected && !(Studio.selected in Studio.flow.steps)) Studio.selected = null;
  Studio.multi = new Set([...Studio.multi].filter((name) => name in Studio.flow.steps));
  Studio.wire = null;
  Studio.problems = [];
  studioCommit();            // one undo step back to what was on screen
  Studio.dirty = false;      // the canvas now matches the saved file
  if (wasEmpty) studioFit();
  studioRender();
  studioSyncBanner();
  studioLoadVersions();
  const summary = studioChangeSummary(StudioSync.changes);
  studioMessage(`${author} updated ${Studio.name}: ${summary}. Press Ctrl+Z to undo it here.`);
}

/* The editor's own save or restore: the file now matches the canvas, nothing to mark. */
async function studioSyncSaved() {
  StudioSync.gen += 1;
  StudioSync.changes = null;
  StudioSync.pending = null;
  StudioSync.name = Studio.name;
  StudioSync.base = studioClone(Studio.flow);
  try { StudioSync.version = (await studioApi(`/api/flows/${encodeURIComponent(Studio.name)}/state`)).version; }
  catch { StudioSync.version = null; }
  studioSyncBanner();
}

async function studioSyncTrack() {
  const gen = StudioSync.gen, name = Studio.name;
  const state = await studioApi(`/api/flows/${encodeURIComponent(name)}/state`);
  if (gen !== StudioSync.gen || name !== Studio.name) return;
  if (StudioSync.name !== name) {
    // a different flow is open: start tracking it without marking anything
    let base = { steps: {} };
    if (state.version) {
      try { base = await studioApi(`/api/flows/${encodeURIComponent(name)}`); } catch { return; }
    }
    if (gen !== StudioSync.gen || name !== Studio.name) return;
    Object.assign(StudioSync, { name, version: state.version, base, changes: null, pending: null });
    studioSyncBanner();
    return;
  }
  if (state.version === StudioSync.version || state.version === StudioSync.pending?.version) return;
  if (state.version === null) { StudioSync.version = null; return; } // deleted or renamed
  const flow = await studioApi(`/api/flows/${encodeURIComponent(name)}`);
  if (gen !== StudioSync.gen || name !== Studio.name) return;
  if (studioSame(flow, Studio.flow)) {
    // the save matches the canvas (this editor saved it, or someone saved the same thing)
    Object.assign(StudioSync, { version: state.version, base: studioClone(flow), pending: null });
    studioSyncBanner();
    return;
  }
  const author = studioSyncAuthor(state);
  if (Studio.dirty) {
    StudioSync.pending = { flow, version: state.version, author };
    studioSyncBanner();
    studioMessage(`${author} saved a new version of ${name}. Your unsaved edits are still on screen.`, true);
  } else {
    studioSyncApply(flow, state.version, author);
  }
}

/* While Claude works with no saved flow open, open the first flow it creates. */
async function studioSyncWatchNew() {
  const active = typeof StudioClaude === "object" && StudioClaude.state?.active;
  if (!active) { StudioSync.known = null; return; }
  const names = await studioApi("/api/flows");
  if (!StudioSync.known) { StudioSync.known = new Set(names); return; }
  const fresh = names.filter((name) => !StudioSync.known.has(name));
  names.forEach((name) => StudioSync.known.add(name));
  const saved = Studio.name && names.includes(Studio.name);
  const empty = !Object.keys(Studio.flow.steps || {}).length; // a blank "New" flow has nothing to lose
  if (!fresh.length || saved || (Studio.dirty && !empty) || !Studio.visible) return;
  Studio.dirty = false;
  await studioOpen(fresh[0]);
  if (Studio.name !== fresh[0]) return;
  await studioList();
  StudioSync.gen += 1;
  Object.assign(StudioSync, {
    name: Studio.name, base: studioClone(Studio.flow), pending: null, author: "Claude",
    changes: studioFlowChanges({ steps: {} }, Studio.flow),
  });
  try { StudioSync.version = (await studioApi(`/api/flows/${encodeURIComponent(Studio.name)}/state`)).version; }
  catch { StudioSync.version = null; }
  studioRender();
  studioSyncBanner();
  studioMessage(`Claude created ${Studio.name}. Its steps are marked as new.`);
}

async function studioSyncTick() {
  if (StudioSync.busy || document.hidden || !Studio.visible) return;
  StudioSync.busy = true;
  try {
    await studioSyncWatchNew();
    if (Studio.name) await studioSyncTrack();
  } catch { /* the next tick tries again */ }
  finally { StudioSync.busy = false; }
}

function studioSyncInit() {
  document.getElementById("studio-sync-load").onclick = () => {
    const pending = StudioSync.pending;
    if (!pending) return;
    if (!window.confirm(`Replace your unsaved edits with ${pending.author}'s version? Ctrl+Z brings them back.`)) return;
    studioSyncApply(pending.flow, pending.version, pending.author);
  };
  document.getElementById("studio-sync-keep").onclick = () => {
    const pending = StudioSync.pending;
    if (!pending) return;
    // Compare later saves with this one; saving now replaces it (it stays in History).
    Object.assign(StudioSync, { version: pending.version, base: studioClone(pending.flow), pending: null });
    studioSyncBanner();
    studioMessage(`Kept your edits. Saving replaces ${pending.author}'s version, which stays in History.`);
  };
  document.getElementById("studio-sync-show").onclick = () => {
    const changes = StudioSync.changes;
    if (!changes) return;
    const first = [...changes.added, ...changes.changed.keys()].find((name) => name in Studio.flow.steps);
    if (first) studioFocusStep(first); else studioFit();
  };
  document.getElementById("studio-sync-clear").onclick = () => {
    StudioSync.changes = null;
    studioRender();
    studioSyncBanner();
  };
  setInterval(studioSyncTick, STUDIO_SYNC_MS);
}
