/* MiniFlow Studio: run data on the canvas and live checking.
 *
 * Shows a run of the flow being edited (the latest by default): each step's state and
 * item count, item counts on the arrows, and a sample of the result in the settings
 * panel. Steps whose block or settings changed since that run (by step signature), and
 * everything after them, are greyed out as "changed since this run".
 */
"use strict";

let studioCheckTimer = null;

/* Validate quietly ~0.7 s after the last change: traffic lights and signatures stay current. */
function studioScheduleCheck() {
  clearTimeout(studioCheckTimer);
  studioCheckTimer = setTimeout(studioCheck, 700);
}

async function studioCheck() {
  if (!Studio.visible || !Studio.name) return;
  try {
    const result = await studioApi("/api/flows/validate", { name: Studio.name, flow: Studio.flow });
    Studio.problems = result.problems || [];
    Studio.signatures = result.signatures || {};
    studioRender();
  } catch { /* the toolbar's Validate button reports errors */ }
}

async function studioLoadRuns(choose) {
  const select = document.getElementById("studio-run-data");
  if (!select || !Studio.name) return;
  let runs = [];
  try { runs = (await studioApi("/api/runs")).filter((r) => r.flow === Studio.name); } catch { runs = []; }
  const keep = choose ?? select.value;
  select.replaceChildren(
    new Option(runs.length ? "Latest run" : "No runs yet", runs.length ? "latest" : ""),
    ...runs.slice(0, 20).map((r) => new Option(
      `${new Date(r.started_at * 1000).toLocaleString()} · ${r.status.toLowerCase()}`, r.run_id)),
    new Option("Hide run data", "none"),
  );
  select.value = [...select.options].some((o) => o.value === keep) ? keep : select.options[0].value;
  const id = select.value === "latest" ? runs[0]?.run_id : select.value;
  if (!id || id === "none") { Studio.runData = null; studioRender(); return; }
  try {
    const detail = await studioApi(`/api/runs/${encodeURIComponent(id)}`);
    Studio.runData = { run: detail.run, graph: detail.graph, tasks: new Map(detail.tasks.map((t) => [t.task, t])) };
  } catch { Studio.runData = null; }
  studioRender();
}

/* Steps whose result in the shown run no longer matches the flow as edited. */
function studioStale() {
  const stale = new Set();
  const data = Studio.runData;
  if (!data) return stale;
  const old = data.graph?.tasks || {};
  const names = Object.keys(Studio.flow.steps || {});
  for (const name of names) {
    const before = old[name]?.config_signature, now = Studio.signatures[name];
    // new since the run, or settings changed (runs made before V2 have no config_signature)
    if (!(name in old) || (before && now && before !== now)) stale.add(name);
  }
  let grew = true;
  while (grew) {  // anything after a changed step changes too
    grew = false;
    for (const name of names) {
      if (!stale.has(name) && studioDeps(name).some((dep) => stale.has(dep))) { stale.add(name); grew = true; }
    }
  }
  return stale;
}

function studioItems(task) {
  if (!task || task.item_count === null || task.item_count === undefined) return "";
  return `${task.item_count} item${task.item_count === 1 ? "" : "s"}`;
}

/* Called at the end of studioRender: badges on steps and counts on arrows. */
function studioDecorate(positions) {
  const data = Studio.runData;
  const stale = studioStale();
  for (const node of document.querySelectorAll("#studio-nodes .studio-node")) {
    const name = node.dataset.step;
    const task = data?.tasks.get(name);
    if (!data || !task) continue;
    const badge = document.createElement("span");
    badge.className = `studio-run-badge st-${task.state}`;
    badge.textContent = [task.state.toLowerCase(), studioItems(task)].filter(Boolean).join(" · ");
    node.append(badge);
    if (stale.has(name)) {
      node.classList.add("stale");
      node.title = `${node.title ? `${node.title}\n` : ""}Changed since this run; its result may differ now.`;
    }
  }
  if (!data) return;
  const svg = document.getElementById("studio-wires");
  for (const name of Object.keys(Studio.flow.steps)) {
    for (const dep of studioDeps(name)) {
      const items = studioItems(data.tasks.get(dep));
      const a = positions[dep], b = positions[name];
      if (!items || !a || !b) continue;
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", String((a.x + 200 + b.x) / 2));
      label.setAttribute("y", String((a.y + b.y) / 2 + 30));
      label.setAttribute("class", "studio-wire-label");
      label.textContent = items;
      svg.append(label);
    }
  }
}

/* "Last run" section of the settings panel for one step. */
function studioRunSection(name) {
  const task = Studio.runData?.tasks.get(name);
  if (!task) return null;
  const box = document.createElement("section");
  box.className = "studio-run-result";
  const h = document.createElement("h3");
  h.textContent = "In the shown run";
  const state = document.createElement("p");
  const took = task.started_at && task.finished_at ? ` in ${(task.finished_at - task.started_at).toFixed(2)} s` : "";
  state.textContent = `${task.state.toLowerCase()}${took}${task.item_count != null ? ` · ${studioItems(task)}` : ""}${task.from_cache ? " · reused" : ""}`;
  box.append(h, state);
  if (studioStale().has(name)) {
    box.append(Object.assign(document.createElement("p"), { className: "hint", textContent: "This step (or one before it) changed since this run." }));
  }
  if (task.error) {
    box.append(Object.assign(document.createElement("pre"), { className: "error", textContent: task.error.trim().split("\n").slice(-6).join("\n") }));
  } else if (task.sample !== null && task.sample !== undefined) {
    box.append(Object.assign(document.createElement("pre"), { className: "studio-sample", textContent: JSON.stringify(task.sample, null, 2) }));
  } else if (task.preview) {
    box.append(Object.assign(document.createElement("pre"), { className: "studio-sample", textContent: task.preview }));
  }
  return box;
}
