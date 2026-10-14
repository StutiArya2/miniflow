/* MiniFlow Studio: local visual editor for the JSON flow format. */
"use strict";

const Studio = {
  blocks: new Map(), flow: { steps: {} }, name: "", selected: null,
  subflows: new Map(), environments: [],
  connecting: null, problems: [], dirty: false, visible: false,
  testIndex: 0,
  section: "examples",       // which top tab is current: examples, runs or editor
  multi: new Set(),          // extra selected steps (Shift-click or Shift-drag)
  clipboard: null,           // copied steps as JSON text
  runData: null,             // run shown on the canvas: {run, graph, tasks}
  signatures: {},            // current step signatures from the last validation
  wire: null,                // selected connection: {from, to}
  activePanel: "test",       // one workflow-settings tab is visible at a time
  stopping: false,
};

const STUDIO_PANELS = ["test", "schedule", "finish", "history", "params"];

function studioSelectPanel(name, focus = false) {
  if (!STUDIO_PANELS.includes(name)) return;
  Studio.activePanel = name;
  for (const key of STUDIO_PANELS) {
    const tab = document.getElementById(`studio-${key}-tab`);
    const panel = document.getElementById(`studio-${key}-panel`);
    const selected = key === name;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    panel.hidden = !selected;
  }
  if (focus) document.getElementById(`studio-${name}-tab`).focus();
  if (name === "finish") studioRenderFinish(true);
  if (name === "history") studioLoadVersions();
}

function studioPanelKeydown(event) {
  const index = STUDIO_PANELS.indexOf(Studio.activePanel);
  const next = event.key === "ArrowRight" ? (index + 1) % STUDIO_PANELS.length
    : event.key === "ArrowLeft" ? (index + STUDIO_PANELS.length - 1) % STUDIO_PANELS.length
      : event.key === "Home" ? 0 : event.key === "End" ? STUDIO_PANELS.length - 1 : -1;
  if (next < 0) return;
  event.preventDefault();
  studioSelectPanel(STUDIO_PANELS[next], true);
}

function studioMessage(message, error = false) {
  const el = document.getElementById("studio-message");
  el.textContent = message;
  el.classList.toggle("error", error);
}

function studioShow(show) {
  Studio.visible = show;
  document.body.classList.toggle("studio-active", show);
  document.getElementById("studio").hidden = !show;
  studioNav(show ? "editor" : Studio.section === "editor" ? "examples" : Studio.section);
  try { history.replaceState(null, "", show ? "#editor" : S.runId ? `#run=${S.runId}` : location.pathname); }
  catch { /* sandboxed */ }
  if (show) { studioSelectPanel(Studio.activePanel); studioRender(); studioLoadRuns(); studioScheduleCheck(); }
  else loadRuns();
  studioRenderStop();
}

/* Top navigation: Examples | Runs | Editor. The current section is marked for the eye and
 * for screen readers. */
function studioNav(section) {
  Studio.section = section;
  document.body.dataset.view = section;
  for (const name of ["examples", "runs", "editor"]) {
    const tab = document.getElementById(`view-${name}`);
    if (name === section) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
}

/* Leave the editor (if open) and go to the example gallery or the run view. */
function studioGo(section) {
  studioShow(false);
  studioNav(section);
  if (section === "examples") document.getElementById("gallery").open = true;
  window.scrollTo({ top: 0 });
}

async function studioApi(path, data) {
  const response = await fetch(path, data === undefined ? { cache: "no-store" } : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok && !Array.isArray(result.problems)) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}

async function studioUploadCsv(file) {
  if (file.size > 10 * 1024 * 1024) throw new Error("CSV file must be 10 MB or smaller.");
  const response = await fetch(`/api/flows/upload-csv?name=${encodeURIComponent(file.name)}`, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream", "X-MiniFlow-Upload": "csv" },
    body: file,
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result.path;
}

async function studioList() {
  const names = await studioApi("/api/flows");
  const select = document.getElementById("studio-flow");
  select.replaceChildren(new Option("Choose a flow…", ""), ...names.map((n) => new Option(n, n)));
  select.value = Studio.name && names.includes(Studio.name) ? Studio.name : "";
}

async function studioOpen(name) {
  if (!name) return;
  if (Studio.dirty && !window.confirm("Discard unsaved workflow changes?")) return;
  try {
    Studio.flow = await studioApi(`/api/flows/${encodeURIComponent(name)}`);
    Studio.name = name;
    Studio.selected = null;
    Studio.testIndex = 0;
    Studio.problems = [];
    Studio.dirty = false;
    studioHistoryReset();
    document.getElementById("studio-name").value = name;
    studioShow(true);
    studioFit();
    studioLoadVersions();
    studioMessage(`Editing ${name}. Select a node or add a block.`);
  } catch (error) { studioMessage(String(error), true); }
}

function studioNew() {
  if (Studio.dirty && !window.confirm("Discard unsaved workflow changes?")) return;
  Studio.flow = { description: "", params: {}, steps: {} };
  Studio.name = "new_flow";
  Studio.selected = null;
  Studio.testIndex = 0;
  Studio.problems = [];
  studioHistoryReset();
  Studio.dirty = true;
  document.getElementById("studio-name").value = Studio.name;
  studioShow(true);
  studioMessage("Add a block from the palette to begin.");
}

async function studioUseTemplate() {
  if (Studio.dirty && !window.confirm("Discard unsaved workflow changes?")) return;
  const template = document.getElementById("studio-template").value;
  const existing = [...document.getElementById("studio-flow").options].map((option) => option.value);
  let name = template;
  let number = 2;
  while (existing.includes(name)) name = `${template}_${number++}`;
  try {
    Studio.flow = await studioApi("/api/flows/template", { template, name });
    Studio.name = name; Studio.selected = null; Studio.problems = []; Studio.dirty = false;
    studioHistoryReset();
    document.getElementById("studio-name").value = name;
    await studioList(); await loadControl();
    const subflows = await studioApi("/api/subflows");
    Studio.subflows = new Map(subflows.map((flow) => [flow.name, flow]));
    studioShow(true);
    studioMessage(`Starter ${name} is ready. Change a parameter or add a block, then run it.`);
  } catch (error) { studioMessage(String(error), true); }
}

function studioDefault(setting) {
  if (Object.hasOwn(setting, "default")) return setting.default;
  const kind = setting.types[0];
  return kind === "list" ? [] : kind === "dict" ? {} : kind === "bool" ? false
    : kind === "int" || kind === "float" ? 0 : "";
}

/* Where a new step goes: at `at` (canvas coordinates, from a drop) or in the next grid slot. */
function studioSlot(at, count) {
  return at
    ? { x: Math.max(10, Math.round(at.x)), y: Math.max(10, Math.round(at.y)) }
    : { x: 90 + (count % 4) * 250, y: 90 + Math.floor(count / 4) * 150 };
}

/* Add a `blockName` step at `at` (or the next free slot); with `after`, it runs after that step. */
function studioAdd(blockName, at, after) {
  const spec = Studio.blocks.get(blockName);
  if (!spec) return;
  let name = blockName.replace(/[^A-Za-z0-9_]/g, "_");
  let index = 2;
  while (Object.hasOwn(Studio.flow.steps, name)) name = `${blockName}_${index++}`;
  const count = Object.keys(Studio.flow.steps).length;
  Studio.flow.steps[name] = {
    block: blockName, settings: Object.fromEntries(spec.settings.map((s) => [s.name, studioDefault(s)])),
    ...(after ? { after: [after] } : {}),
    position: studioSlot(at, count),
  };
  Studio.selected = name;
  Studio.multi = new Set();
  Studio.wire = null;
  studioCommit();
  studioRender();
  studioMessage(after ? `${name} added after ${after}. Configure it in the settings panel.`
    : `${name} added. Configure it in the settings panel.`);
}

/* Palette groups in a fixed order; anything else (plugins) comes last. */
const STUDIO_GROUPS = ["Read & write", "Transform", "Text & dates", "Excel", "Messages, AI & cloud", "Email & utilities", "Databases", "System"];

function studioBlockButton(b) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "studio-block";
  button.textContent = b.name;
  const small = document.createElement("small");
  small.textContent = `${b.kind} · ${b.description}`;
  button.append(small);
  button.onclick = () => studioAdd(b.name);
  studioDraggable(button, "block", b.name);
  return button;
}

function studioPalette() {
  const query = document.getElementById("studio-search").value.trim().toLowerCase();
  const items = [...Studio.blocks.values()].filter((b) =>
    `${b.name} ${b.description} ${b.group || ""}`.toLowerCase().includes(query));
  const groups = new Map();
  for (const b of items) {
    const group = b.group || "Other";
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(b);
  }
  const order = (g) => { const i = STUDIO_GROUPS.indexOf(g); return i < 0 ? STUDIO_GROUPS.length : i; };
  const list = document.getElementById("studio-blocks");
  list.replaceChildren(...[...groups.keys()].sort((a, b) => order(a) - order(b) || a.localeCompare(b))
    .flatMap((group) => {
      const heading = document.createElement("h4");
      heading.className = "studio-group";
      heading.textContent = group;
      return [heading, ...groups.get(group).map(studioBlockButton)];
    }));
  if (!items.length) list.replaceChildren(Object.assign(document.createElement("p"), { className: "hint", textContent: "No block matches." }));
  document.getElementById("studio-subflows").replaceChildren(...[...Studio.subflows.values()]
    .filter((flow) => flow.name !== Studio.name && `${flow.name} ${flow.description}`.toLowerCase().includes(query))
    .map((flow) => {
      const button = document.createElement("button"); button.className = "studio-block";
      button.type = "button"; button.textContent = flow.name;
      const small = document.createElement("small"); small.textContent = flow.description || "Reusable workflow";
      button.append(small); button.onclick = () => studioAddFlow(flow.name);
      studioDraggable(button, "flow", flow.name);
      return button;
    }));
}

function studioAddFlow(flowName, at) {
  const flow = Studio.subflows.get(flowName);
  if (!flow) return;
  let name = flowName.replace(/[^A-Za-z0-9_]/g, "_");
  let index = 2;
  while (Object.hasOwn(Studio.flow.steps, name)) name = `${flowName}_${index++}`;
  const count = Object.keys(Studio.flow.steps).length;
  Studio.flow.steps[name] = {
    flow: `${flowName}.json`,
    settings: Object.fromEntries(Object.entries(flow.params || {}).filter(([, spec]) =>
      Object.hasOwn(spec, "default")).map(([key, spec]) => [key, spec.default])),
    position: studioSlot(at, count),
  };
  Studio.selected = name; Studio.multi = new Set(); studioCommit(); studioRender();
  studioMessage(`${name} added as a reusable flow.`);
}

function studioPosition(name, index) {
  const pos = Studio.flow.steps[name].position || {};
  return { x: Number(pos.x) || 90 + (index % 4) * 250, y: Number(pos.y) || 90 + Math.floor(index / 4) * 150 };
}

function studioWire(from, to, positions) {
  const a = positions[from], b = positions[to];
  if (!a || !b) return null;
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  const x1 = a.x + 200, y1 = a.y + 37, x2 = b.x, y2 = b.y + 37;
  const bend = Math.max(45, Math.abs(x2 - x1) / 2);
  path.setAttribute("d", `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`);
  return path;
}

/* Settings of `step` whose value uses {{ dep }} (or {{ dep.field }}). */
function studioSettingsUsing(step, dep) {
  const pattern = new RegExp(`\\{\\{\\s*${dep}(?=[\\s.}])`);
  return Object.entries(step.settings || {})
    .filter(([, value]) => pattern.test(typeof value === "string" ? value : JSON.stringify(value)))
    .map(([key]) => key);
}

function studioWires(positions) {
  const svg = document.getElementById("studio-wires");
  const edges = [];
  for (const [name, step] of Object.entries(Studio.flow.steps)) {
    const inferred = [...JSON.stringify(step.settings || {}).matchAll(/\{\{\s*([A-Za-z_]\w*)/g)]
      .map((match) => match[1]);
    for (const dep of new Set([...(step.after || []), ...inferred])) {
      if (!(dep in Studio.flow.steps) || dep === name) continue;
      const line = studioWire(dep, name, positions);
      if (!line) continue;
      const explicit = (step.after || []).includes(dep);
      const chosen = Studio.wire?.from === dep && Studio.wire?.to === name;
      line.setAttribute("class", `studio-wire${explicit ? "" : " inferred"}${chosen ? " chosen" : ""}`);
      // a wide invisible copy of the path makes the thin wire easy to click
      const hit = line.cloneNode();
      hit.setAttribute("class", "studio-wire-hit");
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `${dep} → ${name}: click to select`;
      hit.append(title);
      hit.addEventListener("pointerdown", (event) => event.stopPropagation());
      hit.addEventListener("click", (event) => {
        event.stopPropagation();
        Studio.wire = { from: dep, to: name };
        Studio.selected = null; Studio.multi = new Set();
        studioRender();
        studioMessage(explicit ? `${dep} → ${name} selected. Press Delete to remove it.`
          : studioInferredWhy(dep, name));
      });
      edges.push(line, hit);
    }
  }
  svg.replaceChildren(...edges);
}

function studioInferredWhy(from, to) {
  const keys = studioSettingsUsing(Studio.flow.steps[to] || {}, from);
  return `This connection comes from {{ ${from} }} in ${to}'s ${keys.length ? keys.join(", ") : "settings"}. `
    + "Change that setting to remove it.";
}

/* Remove the connection `wire` if it was drawn by hand ("after"); explain it otherwise. */
function studioRemoveWire(wire) {
  const step = Studio.flow.steps[wire.to];
  if (!step) { Studio.wire = null; studioRender(); return; }
  if (!(step.after || []).includes(wire.from)) { studioMessage(studioInferredWhy(wire.from, wire.to), true); return; }
  step.after = step.after.filter((dep) => dep !== wire.from);
  if (!step.after.length) delete step.after;
  const still = studioSettingsUsing(step, wire.from).length > 0;
  Studio.wire = null;
  studioCommit();
  studioRender();
  studioMessage(still ? `Removed the drawn connection, but ${studioInferredWhy(wire.from, wire.to)}`
    : `${wire.from} no longer runs before ${wire.to}. Undo with Ctrl+Z.`, still);
}

/* Settings-panel content for a selected connection. */
function studioWirePanel() {
  const { from, to } = Studio.wire;
  const box = document.createElement("div");
  box.className = "studio-wire-panel";
  const text = document.createElement("p");
  const explicit = (Studio.flow.steps[to]?.after || []).includes(from);
  text.textContent = explicit ? `${from} runs before ${to}.` : studioInferredWhy(from, to);
  box.append(text);
  if (explicit) {
    const remove = document.createElement("button");
    remove.type = "button"; remove.className = "btn danger"; remove.textContent = "Remove connection";
    remove.onclick = () => studioRemoveWire({ from, to });
    box.append(remove);
  }
  for (const name of [from, to]) {
    const open = document.createElement("button");
    open.type = "button"; open.className = "btn"; open.textContent = `Open ${name}`;
    open.onclick = () => { Studio.wire = null; studioToggleSelect(name, false); };
    box.append(open);
  }
  return box;
}

function studioConnect(target) {
  const source = Studio.connecting;
  Studio.connecting = null;
  if (!source || source === target) return;
  const after = Studio.flow.steps[target].after || [];
  if (!after.includes(source)) after.push(source);
  Studio.flow.steps[target].after = after;
  studioCommit();
  studioRender();
  studioMessage(`${source} now runs before ${target}.`);
}

function studioRender() {
  if (!Studio.visible) return;
  studioPalette();
  const names = Object.keys(Studio.flow.steps || {});
  const positions = Object.fromEntries(names.map((name, i) => [name, studioPosition(name, i)]));
  const host = document.getElementById("studio-nodes");
  host.replaceChildren(...names.map((name) => {
    const step = Studio.flow.steps[name], pos = positions[name];
    const node = document.createElement("div");
    node.dataset.step = name;
    node.className = "studio-node" + (studioSelection().includes(name) ? " selected" : "")
      + (Studio.problems.some((p) => p.includes(`step '${name}'`)) ? " invalid" : "")
      + (Studio.connecting === name ? " connecting" : "");
    node.style.left = `${pos.x}px`; node.style.top = `${pos.y}px`;
    if (step.note) node.title = step.note;
    const title = document.createElement("strong"); title.textContent = name;
    const kind = document.createElement("small"); kind.textContent = step.block || `Sub-flow: ${step.flow}`;
    const input = document.createElement("button"); input.className = "input-port";
    input.type = "button"; input.title = "Connect a preceding block"; input.textContent = "●";
    input.onclick = (event) => { event.stopPropagation(); studioConnect(name); };
    const output = document.createElement("button"); output.className = "output-port";
    output.type = "button"; output.title = "Add or connect the next step"; output.textContent = "+";
    output.onclick = (event) => { event.stopPropagation(); studioQuickAdd(name, output); };
    node.append(input, title, kind, output);
    node.onclick = (event) => studioToggleSelect(name, event.shiftKey);
    node.onpointerdown = (event) => {
      if (event.target instanceof HTMLButtonElement) return;
      const startX = event.clientX, startY = event.clientY, initial = { ...pos };
      let moved = false;
      node.setPointerCapture(event.pointerId);
      node.onpointermove = (move) => {
        if (!node.hasPointerCapture(move.pointerId)) return;
        const zoom = typeof StudioView === "object" ? StudioView.zoom : 1;
        step.position = { x: Math.max(10, initial.x + (move.clientX - startX) / zoom), y: Math.max(10, initial.y + (move.clientY - startY) / zoom) };
        node.style.left = `${step.position.x}px`; node.style.top = `${step.position.y}px`;
        positions[name] = step.position; studioWires(positions); moved = true;
      };
      node.onpointerup = () => { node.onpointermove = null; if (moved) studioCommit(); };
    };
    return node;
  }));
  studioWires(positions);
  studioDecorate(positions);
  studioInspector();
  studioRenderProblems();
  studioRenderTests();
  studioRenderSchedules();
  studioRenderParams();
  studioRenderFinish();
}

/* ------------------------------------------------------------ quick add next */
function studioQuickAddClose() {
  document.getElementById("studio-quick-add")?.remove();
}

/* Where a step added after `source` goes: to its right, below anything already there. */
function studioNextSlot(source) {
  const names = Object.keys(Studio.flow.steps);
  const from = studioPosition(source, names.indexOf(source));
  const at = { x: from.x + 250, y: from.y };
  const taken = names.map((name, i) => studioPosition(name, i));
  while (taken.some((p) => Math.abs(p.x - at.x) < 180 && Math.abs(p.y - at.y) < 80)) at.y += 120;
  return at;
}

/* Popover from a step's "+" port: pick a block to add after the step, or connect to an existing one. */
function studioQuickAdd(source, anchor) {
  studioQuickAddClose();
  const pop = document.createElement("div");
  pop.id = "studio-quick-add";
  pop.className = "studio-quick-add panel";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", `Add a step after ${source}`);
  const search = document.createElement("input");
  search.placeholder = `Add after ${source}…`;
  search.setAttribute("aria-label", "Search blocks to add");
  const list = document.createElement("div");
  list.className = "studio-quick-list";
  const uses = new Map();
  for (const step of Object.values(Studio.flow.steps)) if (step.block) uses.set(step.block, (uses.get(step.block) || 0) + 1);
  const used = [...uses.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6).map(([block]) => block);
  const pick = (button, run) => { button.type = "button"; button.className = "studio-quick-item"; button.onclick = () => { studioQuickAddClose(); run(); }; return button; };
  const fill = () => {
    const query = search.value.trim().toLowerCase();
    const blocks = [...Studio.blocks.values()].filter((b) => `${b.name} ${b.description} ${b.group || ""}`.toLowerCase().includes(query));
    blocks.sort((a, b) => (used.includes(a.name) ? used.indexOf(a.name) : 99) - (used.includes(b.name) ? used.indexOf(b.name) : 99));
    const connect = pick(document.createElement("button"), () => {
      Studio.connecting = source; studioRender();
      studioMessage(`Select the input port (●) of the step that should run after ${source}.`);
    });
    connect.textContent = "Connect to an existing step…";
    connect.classList.add("connect");
    list.replaceChildren(connect, ...blocks.map((b) => {
      const button = pick(document.createElement("button"), () => studioAdd(b.name, studioNextSlot(source), source));
      button.textContent = b.name;
      const small = document.createElement("small");
      small.textContent = used.includes(b.name) ? "used in this flow" : b.group || b.kind;
      button.append(small);
      return button;
    }));
  };
  search.oninput = fill;
  search.onkeydown = (event) => {
    if (event.key === "Escape") { studioQuickAddClose(); }
    else if (event.key === "Enter") { list.querySelectorAll(".studio-quick-item")[search.value ? 1 : 0]?.click(); }
  };
  fill();
  pop.append(search, list);
  document.body.append(pop);
  const box = anchor.getBoundingClientRect();
  const left = Math.min(box.right + 8, window.innerWidth - pop.offsetWidth - 8);
  const top = Math.min(box.top - 10, window.innerHeight - pop.offsetHeight - 8);
  pop.style.left = `${Math.max(8, left)}px`;
  pop.style.top = `${Math.max(8, top)}px`;
  search.focus();
  setTimeout(() => document.addEventListener("pointerdown", function away(event) {
    if (!pop.contains(event.target)) { studioQuickAddClose(); document.removeEventListener("pointerdown", away); }
  }), 0);
}

/* ------------------------------------------------------------------ problems */
function studioRenderProblems() {
  const host = document.getElementById("studio-problems");
  if (!host) return;
  host.hidden = !Studio.problems.length;
  host.replaceChildren(...Studio.problems.map((problem) => {
    const item = document.createElement("li");
    const step = /step '([A-Za-z_]\w*)'/.exec(problem)?.[1];
    if (step && step in Studio.flow.steps) {
      const button = document.createElement("button");
      button.type = "button"; button.className = "studio-problem";
      button.textContent = problem; button.title = `Show ${step}`;
      button.onclick = () => studioFocusStep(step);
      item.append(button);
    } else {
      item.textContent = problem;
    }
    return item;
  }));
}

function studioParamDefault(type) {
  return type === "bool" ? false : type === "int" || type === "float" ? 0
    : type === "list" ? [] : type === "dict" ? {} : "";
}

function studioRenameParamReferences(previous, next) {
  const pattern = new RegExp(`(\\{\\{\\s*params\\.)${previous}(?=\\.|\\s*\\}\\})`, "g");
  const rewrite = (value) => {
    if (typeof value === "string") return value.replace(pattern, `$1${next}`);
    if (Array.isArray(value)) return value.map(rewrite);
    if (value && typeof value === "object") {
      for (const key of Object.keys(value)) value[key] = rewrite(value[key]);
    }
    return value;
  };
  rewrite(Studio.flow.steps);
  rewrite(Studio.flow.outputs);
  rewrite(Studio.flow.on_failure);
  rewrite(Studio.flow.on_success);
  for (const test of Studio.flow.tests || []) if (Object.hasOwn(test.params || {}, previous)) {
    test.params[next] = test.params[previous]; delete test.params[previous];
  }
}

function studioRenderParams() {
  const host = document.getElementById("studio-params-editor");
  const params = Studio.flow.params ||= {};
  const description = document.createElement("textarea");
  description.value = Studio.flow.description || "";
  description.placeholder = "What does this workflow do?";
  description.setAttribute("aria-label", "Flow description");
  description.onchange = () => { Studio.flow.description = description.value; studioCommit(); };
  const rows = Object.entries(params).map(([key, spec]) => {
    const row = document.createElement("div"); row.className = "studio-param-row";
    const name = document.createElement("input"); name.value = key;
    name.setAttribute("aria-label", `Parameter ${key} name`);
    name.onchange = () => {
      const next = name.value.trim();
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(next) || (next !== key && Object.hasOwn(params, next))) {
        studioMessage("Parameter names must be unique and use letters, numbers or _.", true);
        name.value = key; return;
      }
      if (next !== key) {
        params[next] = spec; delete params[key]; studioRenameParamReferences(key, next);
        studioCommit(); studioRender();
      }
    };
    const type = document.createElement("select"); type.setAttribute("aria-label", `Parameter ${key} type`);
    type.append(...["str", "int", "float", "bool", "list", "dict"].map((value) => new Option(value, value)));
    type.value = spec.type || "str";
    type.onchange = () => { spec.type = type.value; spec.default = studioParamDefault(type.value); studioCommit(); studioRenderParams(); };
    const enabled = document.createElement("input"); enabled.type = "checkbox";
    enabled.checked = Object.hasOwn(spec, "default");
    enabled.setAttribute("aria-label", `Parameter ${key} has default`);
    enabled.onchange = () => {
      if (enabled.checked) spec.default = studioParamDefault(spec.type || "str");
      else delete spec.default;
      studioCommit(); studioRenderParams();
    };
    let value;
    if (spec.type === "bool") {
      value = document.createElement("select");
      value.append(new Option("false", "false"), new Option("true", "true"));
      value.value = String(spec.default ?? false);
      value.onchange = () => { spec.default = value.value === "true"; studioCommit(); };
    } else {
      value = spec.type === "list" || spec.type === "dict"
        ? document.createElement("textarea") : document.createElement("input");
      if (value instanceof HTMLInputElement && ["int", "float"].includes(spec.type)) {
        value.type = "number"; value.step = spec.type === "int" ? "1" : "any";
      }
      value.value = typeof spec.default === "string" ? spec.default : JSON.stringify(spec.default ?? "");
      value.onchange = () => {
        const raw = value.value;
        const parsed = spec.type === "int" || spec.type === "float" ? Number(raw)
          : spec.type === "list" || spec.type === "dict" ? studioTestValue(raw) : raw;
        spec.default = parsed; studioCommit();
      };
    }
    value.disabled = !enabled.checked;
    value.setAttribute("aria-label", `Parameter ${key} default`);
    const help = document.createElement("input"); help.value = spec.help || "";
    help.placeholder = "Help text"; help.setAttribute("aria-label", `Parameter ${key} help`);
    help.onchange = () => { spec.help = help.value; studioCommit(); };
    const remove = document.createElement("button"); remove.type = "button";
    remove.className = "btn danger"; remove.textContent = "Remove";
    remove.onclick = () => { delete params[key]; studioCommit(); studioRenderParams(); };
    row.append(name, type, enabled, value, help, remove);
    return row;
  });
  const add = document.createElement("button"); add.type = "button"; add.className = "btn";
  add.textContent = "Add parameter";
  add.onclick = () => {
    let number = 1;
    while (Object.hasOwn(params, `parameter_${number}`)) number++;
    params[`parameter_${number}`] = { type: "str", default: "" };
    studioCommit(); studioRenderParams();
  };
  host.replaceChildren(description, ...rows, add);
}

function studioRenderSchedules() {
  const host = document.getElementById("studio-schedule-editor");
  const schedules = Studio.flow.schedules ||= [];
  const rows = schedules.map((schedule, index) => {
    const row = document.createElement("div"); row.className = "studio-test-row studio-schedule-row";
    const name = document.createElement("input"); name.value = schedule.name;
    name.setAttribute("aria-label", `Schedule ${index + 1} name`);
    name.onchange = () => { schedule.name = name.value.trim(); studioCommit(); };
    const type = document.createElement("select"); type.setAttribute("aria-label", `Schedule ${index + 1} type`);
    type.append(
      new Option("Every N seconds", "interval_seconds"),
      new Option("Daily at local time", "daily"),
      new Option("Cron expression", "cron"),
      new Option("When a file appears", "watch"),
      new Option("Webhook call", "webhook"),
    );
    type.value = ["daily", "cron", "watch", "webhook"].find((key) => schedule[key] !== undefined) || "interval_seconds";
    type.onchange = () => {
      for (const key of ["daily", "interval_seconds", "cron", "watch", "webhook", "poll_seconds"]) delete schedule[key];
      if (type.value === "daily") schedule.daily = "09:00";
      else if (type.value === "cron") schedule.cron = "0 9 * * mon-fri";
      else if (type.value === "watch") schedule.watch = "inbox/*.csv";
      else if (type.value === "webhook") schedule.webhook = "MINIFLOW_HOOK_TOKEN";
      else schedule.interval_seconds = 60;
      if (type.value === "watch") schedule.poll_seconds = 5;
      if (["watch", "webhook"].includes(type.value)) delete schedule.catch_up;
      studioCommit(); studioRenderSchedules();
    };
    const value = document.createElement("input");
    value.setAttribute("aria-label", `Schedule ${index + 1} value`);
    value.type = type.value === "daily" ? "time" : "number";
    if (type.value !== "daily" && type.value !== "interval_seconds") value.type = "text";
    if (value.type === "number") { value.min = "1"; value.step = "1"; }
    value.placeholder = type.value === "cron" ? "0 9 * * mon-fri"
      : type.value === "watch" ? "inbox/*.csv"
      : type.value === "webhook" ? "TOKEN_ENV_NAME" : "";
    value.title = type.value === "webhook" ? "Name of the environment variable holding the secret token" : "";
    value.value = String(schedule[type.value]);
    value.onchange = () => {
      schedule[type.value] = type.value === "interval_seconds" ? Number(value.value) : value.value;
      studioCommit();
    };
    const extra = document.createElement("label"); extra.className = "field";
    if (type.value === "watch") {
      const poll = document.createElement("input"); poll.type = "number";
      poll.min = "1"; poll.step = "1"; poll.value = String(schedule.poll_seconds ?? 5);
      poll.setAttribute("aria-label", `Schedule ${index + 1} poll seconds`);
      poll.onchange = () => { schedule.poll_seconds = Number(poll.value); studioCommit(); };
      extra.append("Check every (s)", poll);
    } else if (["interval_seconds", "daily", "cron"].includes(type.value)) {
      const catchUp = document.createElement("input"); catchUp.type = "checkbox";
      catchUp.checked = !!schedule.catch_up;
      catchUp.onchange = () => { schedule.catch_up = catchUp.checked; studioCommit(); };
      extra.append(catchUp, " Catch up after shutdown");
    }
    const remove = document.createElement("button"); remove.className = "btn danger";
    remove.type = "button"; remove.textContent = "Remove";
    remove.onclick = () => { schedules.splice(index, 1); studioCommit(); studioRenderSchedules(); };
    row.append(name, type, value, extra, remove);
    return row;
  });
  const add = document.createElement("button"); add.className = "btn";
  add.type = "button"; add.textContent = "Add schedule";
  add.onclick = () => {
    let number = schedules.length + 1;
    while (schedules.some((item) => item.name === `schedule_${number}`)) number++;
    schedules.push({ name: `schedule_${number}`, interval_seconds: 60 });
    studioCommit(); studioRenderSchedules();
  };
  host.replaceChildren(...rows, add);
}

function studioTestValue(text) {
  try { return JSON.parse(text); } catch { return text; }
}

function studioTestRows(caseData, key) {
  const group = document.createElement("div"); group.className = "studio-test-group";
  const heading = document.createElement("h3");
  heading.textContent = key === "inputs" ? "Sample step results" : "Expected step results";
  group.append(heading);
  const values = caseData[key] ||= {};
  const names = Object.keys(Studio.flow.steps);
  for (const [step, value] of Object.entries(values)) {
    const row = document.createElement("div"); row.className = "studio-test-row";
    const picker = document.createElement("select");
    picker.setAttribute("aria-label", `${key} step`);
    picker.append(...names.map((name) => new Option(name, name)));
    picker.value = step;
    picker.onchange = () => {
      if (picker.value !== step) { values[picker.value] = values[step]; delete values[step]; studioCommit(); studioRenderTests(); }
    };
    const field = document.createElement("textarea");
    field.setAttribute("aria-label", `${key} value for ${step}`);
    field.value = JSON.stringify(value, null, 2);
    field.onchange = () => { values[step] = studioTestValue(field.value); studioCommit(); };
    const remove = document.createElement("button"); remove.type = "button";
    remove.className = "btn"; remove.textContent = "Remove";
    remove.onclick = () => { delete values[step]; studioCommit(); studioRenderTests(); };
    row.append(picker, field, remove); group.append(row);
  }
  const add = document.createElement("button"); add.type = "button";
  add.className = "btn"; add.textContent = key === "inputs" ? "Add sample" : "Add expectation";
  add.onclick = () => {
    const next = names.find((name) => !Object.hasOwn(values, name));
    if (!next) { studioMessage("Add a step to the canvas first.", true); return; }
    values[next] = null; studioCommit(); studioRenderTests();
  };
  group.append(add);
  return group;
}

function studioRenderTests() {
  const host = document.getElementById("studio-test-editor");
  const cases = Studio.flow.tests ||= [];
  const toolbar = document.createElement("div"); toolbar.className = "studio-test-toolbar";
  const select = document.createElement("select"); select.setAttribute("aria-label", "Test case");
  select.append(...cases.map((test, index) => new Option(test.name || `Case ${index + 1}`, String(index))));
  if (cases.length) select.value = String(Math.min(Studio.testIndex, cases.length - 1));
  select.onchange = () => { Studio.testIndex = Number(select.value); studioRenderTests(); };
  const add = document.createElement("button"); add.type = "button"; add.className = "btn"; add.textContent = "New test case";
  add.onclick = () => {
    cases.push({ name: `case ${cases.length + 1}`, inputs: {}, expect: {} });
    Studio.testIndex = cases.length - 1; studioCommit();
    studioSelectPanel("test");
    studioRenderTests();
  };
  toolbar.append(select, add);
  if (!cases.length) { host.replaceChildren(toolbar); return; }
  const current = cases[Studio.testIndex];
  const label = document.createElement("label"); label.textContent = "Test name ";
  const name = document.createElement("input"); name.value = current.name;
  name.setAttribute("aria-label", "Test name");
  name.onchange = () => { current.name = name.value; studioCommit(); studioRenderTests(); };
  label.append(name); toolbar.append(label);
  const remove = document.createElement("button"); remove.type = "button"; remove.className = "btn danger"; remove.textContent = "Delete test";
  remove.onclick = () => { cases.splice(Studio.testIndex, 1); Studio.testIndex = 0; studioCommit(); studioRenderTests(); };
  toolbar.append(remove);
  host.replaceChildren(toolbar, studioTestRows(current, "inputs"), studioTestRows(current, "expect"));
}

async function studioTest() {
  Studio.name = document.getElementById("studio-name").value.trim();
  try {
    const reports = await studioApi("/api/flows/test", { name: Studio.name, flow: Studio.flow });
    if (!Array.isArray(reports)) { studioMessage((reports.problems || ["Tests could not run."]).join("\n"), true); return; }
    const passed = reports.filter((report) => report.passed).length;
    studioMessage(`${passed}/${reports.length} tests passed\n` + reports.map((report) =>
      `${report.passed ? "PASS" : "FAIL"} ${report.name}${report.problems.length ? ": " + report.problems.join("; ") : ""}`).join("\n"), passed !== reports.length);
    studioSelectPanel("test");
  } catch (error) { studioMessage(String(error), true); }
}

function studioValue(raw, types) {
  if (typeof raw !== "string" || raw.includes("{{")) return raw;
  if (types.some((t) => ["list", "dict", "int", "float", "bool"].includes(t))) {
    try { return JSON.parse(raw); } catch { return raw; }
  }
  return raw;
}

/* Form fields for a block's settings, writing into `settings`. Options:
 * `exclude`: a step name left out of "Use value from…" (the step itself);
 * `extra`: more [label, value] choices for that picker (e.g. failure details);
 * `steps`: false where step results aren't available (the failure action);
 * `alive`: false once the form's step was replaced (a CSV upload finishing late). */
function studioSettingsForm(settings, fields, { exclude = null, extra = [], steps = true, alive = () => true, block = "" } = {}) {
  return fields.map((setting) => {
    const label = document.createElement("label");
    label.textContent = `${setting.name}${setting.required ? " *" : ""}`;
    const value = settings[setting.name] ?? studioDefault(setting);
    const field = setting.choices?.length || setting.types.includes("bool")
      ? document.createElement("select")
      : setting.types.includes("list") || setting.types.includes("dict")
        ? document.createElement("textarea") : document.createElement("input");
    if (field instanceof HTMLSelectElement) {
      const choices = setting.choices?.length ? setting.choices : [false, true];
      field.append(...choices.map((choice) => new Option(String(choice), String(choice))));
    }
    if (field instanceof HTMLInputElement) {
      field.type = setting.secret ? "password" : "text";
      if (setting.types.includes("int") || setting.types.includes("float")) field.inputMode = "decimal";
    }
    field.value = typeof value === "string" ? value : JSON.stringify(value);
    field.placeholder = setting.secret ? "{{ env.NAME }}" : "";
    field.onchange = () => { settings[setting.name] = studioValue(field.value, setting.types); studioCommit(); };
    label.append(field);
    if (block === "read_csv" && setting.name === "path") {
      const upload = document.createElement("button");
      upload.type = "button"; upload.className = "btn"; upload.textContent = "Upload CSV…";
      const picker = document.createElement("input");
      picker.type = "file"; picker.accept = ".csv,text/csv"; picker.hidden = true;
      upload.onclick = () => picker.click();
      picker.onchange = async () => {
        const file = picker.files?.[0];
        if (!file) return;
        upload.disabled = true; upload.textContent = "Uploading…";
        try {
          const path = await studioUploadCsv(file);
          if (alive()) {
            field.value = path;
            settings.path = path;
            studioCommit();
            studioMessage(`Uploaded ${file.name}. The path is ready to use.`);
          }
        } catch (error) { studioMessage(`Upload failed: ${error}`, true); }
        finally { upload.disabled = false; upload.textContent = "Upload CSV…"; picker.value = ""; }
      };
      label.append(upload, picker);
    }
    if (!(field instanceof HTMLSelectElement)) {
      const source = document.createElement("select");
      source.setAttribute("aria-label", `Use value for ${setting.name}`);
      source.append(new Option("Use value from…", ""),
        ...extra.map(([text, ref]) => new Option(text, ref)),
        ...Object.keys(Studio.flow.params || {}).map((key) => new Option(`Parameter: ${key}`, `{{ params.${key} }}`)),
        ...Object.keys(steps ? Studio.flow.steps : {}).filter((key) => key !== exclude)
          .map((key) => new Option(`Step: ${key}`, `{{ ${key} }}`)));
      source.onchange = () => {
        if (!source.value) return;
        field.value = source.value;
        settings[setting.name] = source.value;
        studioCommit();
        studioRender();
      };
      label.append(source);
    }
    if (setting.help) { const hint = document.createElement("span"); hint.className = "hint"; hint.textContent = setting.help; label.append(hint); }
    return label;
  });
}

/* Retries, timeout and worker kind for one block step. An empty value removes the key. */
function studioRunOptions(step, spec) {
  const box = document.createElement("details");
  box.className = "studio-run-options";
  box.open = ["retries", "timeout", "kind"].some((key) => Object.hasOwn(step, key));
  const summary = document.createElement("summary");
  summary.textContent = "Run options";
  box.append(summary);
  const number = (key, text, hint, attrs) => {
    const label = document.createElement("label"); label.textContent = text;
    const input = document.createElement("input");
    input.type = "number"; Object.assign(input, attrs);
    input.setAttribute("aria-label", text);
    input.value = step[key] ?? "";
    input.placeholder = key === "retries" ? "0" : "No limit";
    input.onchange = () => {
      const raw = input.value.trim();
      if (raw === "" || (key === "retries" && Number(raw) === 0)) delete step[key];
      else step[key] = Number(raw);
      studioCommit();
    };
    label.append(input, Object.assign(document.createElement("span"), { className: "hint", textContent: hint }));
    return label;
  };
  box.append(
    number("retries", "Retries", "Try again this many times if the step fails.", { min: "0", step: "1" }),
    number("timeout", "Timeout (seconds)", "Stop the step if it takes longer.", { min: "0.1", step: "any" }),
  );
  if (spec.kind !== "async") {
    const label = document.createElement("label"); label.textContent = "Runs on";
    const kind = document.createElement("select");
    kind.setAttribute("aria-label", "Runs on");
    kind.append(
      new Option(`Default (${spec.kind === "cpu" ? "separate process" : "thread"})`, ""),
      new Option("Thread: waiting on files, web, databases (io)", "io"),
      new Option("Separate process: heavy calculation (cpu)", "cpu"),
    );
    kind.value = step.kind || "";
    kind.onchange = () => {
      if (!kind.value || kind.value === spec.kind) delete step.kind;
      else step.kind = kind.value;
      studioCommit();
    };
    label.append(kind);
    box.append(label);
  }
  return box;
}

/* Connections into a step: drawn ones can be removed; ones from {{ }} values are explained. */
function studioDependencyChips(name, step) {
  const box = document.createElement("div");
  box.className = "studio-chips";
  for (const dep of studioDeps(name)) {
    const chip = document.createElement("span");
    const drawn = (step.after || []).includes(dep);
    chip.className = `studio-chip${drawn ? "" : " inferred"}`;
    chip.textContent = dep;
    if (drawn) {
      const remove = document.createElement("button");
      remove.type = "button"; remove.textContent = "×";
      remove.title = `Stop running after ${dep}`;
      remove.setAttribute("aria-label", `Remove connection from ${dep}`);
      remove.onclick = () => studioRemoveWire({ from: dep, to: name });
      chip.append(remove);
    } else {
      chip.title = studioInferredWhy(dep, name);
    }
    box.append(chip);
  }
  if (!box.childElementCount) box.append(Object.assign(document.createElement("span"), { className: "hint", textContent: "Runs first: nothing before it." }));
  return box;
}

function studioInspector() {
  const host = document.getElementById("studio-settings");
  if (Studio.wire && !(Studio.wire.from in Studio.flow.steps && Studio.wire.to in Studio.flow.steps)) Studio.wire = null;
  if (Studio.wire) {
    document.getElementById("studio-inspector-title").textContent = `${Studio.wire.from} → ${Studio.wire.to}`;
    host.replaceChildren(studioWirePanel());
    return;
  }
  const name = Studio.selected, step = Studio.flow.steps[name];
  document.getElementById("studio-inspector-title").textContent = name ? name : "Settings";
  if (!step) { host.textContent = "Select a node on the canvas."; return; }
  const spec = Studio.blocks.get(step.block);
  const child = step.flow ? Studio.subflows.get(step.flow.replace(/\.json$/, "")) : null;
  const fields = spec ? spec.settings : child ? Object.entries(child.params || {}).map(([key, def]) => ({
    name: key, types: [def.type || "str"], required: !Object.hasOwn(def, "default"),
    ...(Object.hasOwn(def, "default") ? { default: def.default } : {}),
    help: def.help || "", secret: false,
  })) : [];
  const form = document.createElement("form"); form.onsubmit = (event) => event.preventDefault();
  step.settings ||= {};
  const rename = document.createElement("label"); rename.textContent = "Step name";
  const renameField = document.createElement("input");
  renameField.value = name; renameField.autocomplete = "off";
  renameField.onchange = () => {
    const next = renameField.value.trim();
    if (next === name) return;
    if (studioRenameStep(name, next)) { studioRender(); studioMessage(`Renamed ${name} to ${next} everywhere it was used.`); }
    else renameField.value = name;
  };
  rename.append(renameField, Object.assign(document.createElement("span"), {
    className: "hint", textContent: "Other steps, outputs and tests that use it follow the new name." }));
  form.append(rename);
  form.append(...studioSettingsForm(step.settings, fields, {
    exclude: name, block: step.block, alive: () => Studio.flow.steps[name] === step,
  }));
  const after = document.createElement("label"); after.textContent = "Runs after";
  after.append(studioDependencyChips(name, step));
  const picker = document.createElement("select");
  picker.setAttribute("aria-label", "Add a step to run before this one");
  picker.append(new Option("Add dependency…", ""), ...Object.keys(Studio.flow.steps)
    .filter((other) => other !== name && !(step.after || []).includes(other)).map((other) => new Option(other, other)));
  picker.onchange = () => { if (picker.value) { (step.after ||= []).push(picker.value); studioCommit(); studioRender(); } };
  after.append(picker); form.append(after);
  if (spec) form.append(studioRunOptions(step, spec));
  const note = document.createElement("label"); note.textContent = "Annotation";
  const noteField = document.createElement("textarea");
  noteField.value = step.note || ""; noteField.placeholder = "Explain this step for teammates";
  noteField.onchange = () => { step.note = noteField.value; studioCommit(); };
  note.append(noteField); form.append(note);
  const remove = document.createElement("button"); remove.type = "button"; remove.className = "btn danger"; remove.textContent = "Delete block";
  remove.onclick = () => {
    delete Studio.flow.steps[name];
    for (const other of Object.values(Studio.flow.steps)) other.after = (other.after || []).filter((dep) => dep !== name);
    Studio.selected = null; studioCommit(); studioRender();
  };
  form.append(remove);
  const result = studioRunSection(name);
  host.replaceChildren(...(result ? [result, form] : [form]));
}

/* ------------------------------------------------- when the flow ends */
const STUDIO_FAILURE_REFS = [
  ["Failure: which step failed", "{{ failure.step }}"],
  ["Failure: error message", "{{ failure.error }}"],
  ["Failure: failed steps", "{{ failure.failed }}"],
  ["Failure: skipped steps", "{{ failure.skipped }}"],
  ["Failure: run id", "{{ failure.run_id }}"],
];

function studioFinishAction(key, title, hint) {
  const box = document.createElement("section");
  box.className = "studio-finish-action";
  const heading = document.createElement("h3"); heading.textContent = title;
  const help = Object.assign(document.createElement("p"), { className: "hint", textContent: hint });
  const action = Studio.flow[key];
  const picker = document.createElement("select");
  picker.setAttribute("aria-label", `${title}: block`);
  picker.append(new Option("Do nothing", ""), ...[...Studio.blocks.values()]
    .map((b) => new Option(`${b.name} (${b.group || b.kind})`, b.name)));
  picker.value = action?.block || "";
  picker.onchange = () => {
    const spec = Studio.blocks.get(picker.value);
    if (!spec) delete Studio.flow[key];
    else Studio.flow[key] = { block: spec.name, settings: Object.fromEntries(spec.settings.map((s) => [s.name, studioDefault(s)])) };
    studioCommit(); studioRenderFinish(true);
  };
  box.append(heading, help, picker);
  const spec = action && Studio.blocks.get(action.block);
  if (spec) {
    action.settings ||= {};
    const form = document.createElement("div"); form.className = "studio-finish-form";
    form.append(...studioSettingsForm(action.settings, spec.settings, {
      extra: key === "on_failure" ? STUDIO_FAILURE_REFS : [], steps: key !== "on_failure", block: spec.name,
      alive: () => Studio.flow[key] === action,
    }));
    box.append(form);
  }
  return box;
}

function studioFinishOutputs() {
  const box = document.createElement("section");
  box.className = "studio-finish-action";
  const heading = document.createElement("h3"); heading.textContent = "Outputs";
  const help = Object.assign(document.createElement("p"), { className: "hint",
    textContent: "Named results of this flow. When it is used as a reusable flow, these are what the parent flow gets." });
  box.append(heading, help);
  const outputs = Studio.flow.outputs || {};
  for (const [key, value] of Object.entries(outputs)) {
    const row = document.createElement("div"); row.className = "studio-output-row";
    const name = document.createElement("input"); name.value = key;
    name.setAttribute("aria-label", `Output ${key} name`);
    name.onchange = () => {
      const next = name.value.trim();
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(next) || (next !== key && Object.hasOwn(outputs, next))) {
        studioMessage("Output names must be unique and use letters, numbers or _.", true);
        name.value = key; return;
      }
      Studio.flow.outputs = Object.fromEntries(Object.entries(outputs).map(([k, v]) => [k === key ? next : k, v]));
      studioCommit(); studioRenderFinish(true);
    };
    const field = document.createElement("input");
    field.value = typeof value === "string" ? value : JSON.stringify(value);
    field.placeholder = "{{ step }} or {{ step.field }}";
    field.setAttribute("aria-label", `Output ${key} value`);
    field.onchange = () => { outputs[key] = studioTestValue(field.value); studioCommit(); };
    const source = document.createElement("select");
    source.setAttribute("aria-label", `Use value for output ${key}`);
    source.append(new Option("Use value from…", ""),
      ...Object.keys(Studio.flow.steps).map((step) => new Option(`Step: ${step}`, `{{ ${step} }}`)),
      ...Object.keys(Studio.flow.params || {}).map((p) => new Option(`Parameter: ${p}`, `{{ params.${p} }}`)));
    source.onchange = () => { if (source.value) { outputs[key] = source.value; studioCommit(); studioRenderFinish(true); } };
    const remove = document.createElement("button"); remove.type = "button";
    remove.className = "btn danger"; remove.textContent = "Remove";
    remove.onclick = () => {
      delete outputs[key];
      if (!Object.keys(outputs).length) delete Studio.flow.outputs;
      studioCommit(); studioRenderFinish(true);
    };
    row.append(name, field, source, remove);
    box.append(row);
  }
  const add = document.createElement("button"); add.type = "button"; add.className = "btn";
  add.textContent = "Add output";
  add.onclick = () => {
    const out = Studio.flow.outputs ||= {};
    let number = 1;
    while (Object.hasOwn(out, `output_${number}`)) number++;
    const last = Object.keys(Studio.flow.steps).at(-1);
    out[`output_${number}`] = last ? `{{ ${last} }}` : "";
    studioCommit(); studioRenderFinish(true);
  };
  box.append(add);
  return box;
}

/* `force` rebuilds even while a field in the drawer has focus (after a change made there). */
function studioRenderFinish(force = false) {
  const host = document.getElementById("studio-finish-editor");
  if (!host) return;
  const panel = document.getElementById("studio-finish-panel");
  // A hidden tab doesn't need its forms; rebuild it when selected.
  if (panel && panel.hidden) {
    const count = ["on_success", "on_failure"].filter((key) => Studio.flow[key]).length
      + Object.keys(Studio.flow.outputs || {}).length;
    document.getElementById("studio-finish-count").textContent = count ? `(${count} set)` : "";
    host.replaceChildren();
    return;
  }
  document.getElementById("studio-finish-count").textContent = "";
  if (!force && host.contains(document.activeElement)) return;  // don't rebuild under the cursor
  host.replaceChildren(
    studioFinishAction("on_success", "When the flow succeeds",
      "Runs once after every step succeeded, for example a Slack message or an email. It can use step results."),
    studioFinishAction("on_failure", "When the flow fails",
      "Runs once if the flow failed. Use the Failure values to say which step failed and why."),
    studioFinishOutputs(),
  );
}

async function studioValidate(save = false) {
  Studio.name = document.getElementById("studio-name").value.trim();
  try {
    const message = document.getElementById("studio-save-message");
    const result = await studioApi(`/api/flows/${save ? "save" : "validate"}`, {
      name: Studio.name, flow: Studio.flow, ...(save && message?.value ? { message: message.value } : {}),
    });
    Studio.problems = result.problems || [];
    Studio.signatures = result.signatures || Studio.signatures;
    studioRender();
    studioMessage(result.valid ? (save ? `Saved ${Studio.name}.json` : "Workflow is valid.")
      : Studio.problems.join("\n"), !result.valid);
    if (save && result.valid) {
      Studio.dirty = false; await studioList(); await loadControl();
      if (message) message.value = "";
      studioLoadVersions();
      const subflows = await studioApi("/api/subflows");
      Studio.subflows = new Map(subflows.map((flow) => [flow.name, flow]));
      studioPalette();
    }
    return result.valid;
  } catch (error) { studioMessage(String(error), true); return false; }
}

async function studioRun() {
  if (!await studioValidate(true)) return;
  try {
    await studioApi("/api/control/run", {
      flow: Studio.name, fail_rate: 0,
      environment: document.getElementById("studio-environment").value,
    });
    studioGo("runs");
    studioMessage(`${Studio.name} started.`);
    setTimeout(loadRuns, 500);
  } catch (error) { studioMessage(String(error), true); }
}

function studioActiveRun() {
  return (S.control?.running || []).find((item) => item.flow === Studio.name && item.run_id);
}

function studioRenderStop() {
  const button = document.getElementById("studio-stop");
  if (!button) return;
  const active = studioActiveRun();
  button.disabled = !active || Studio.stopping || S.controlBusy;
  button.title = active ? `Stop the active run of ${Studio.name}; schedules stay enabled`
    : "Available while this flow is running";
}

async function studioStop() {
  const active = studioActiveRun();
  if (!active || Studio.stopping) return;
  Studio.stopping = true;
  studioRenderStop();
  try {
    await studioApi("/api/control/cancel", { run_id: active.run_id });
    studioMessage(`Stopped the running ${active.flow} workflow.`);
    await loadControl();
    await loadRuns();
    await studioLoadRuns();
  } catch (error) { studioMessage(`Could not stop the workflow: ${error}`, true); }
  finally { Studio.stopping = false; studioRenderStop(); }
}

async function studioPartial() {
  if (!Studio.selected) { studioMessage("Select a step on the canvas first.", true); return; }
  const step = Studio.selected;
  if (!await studioValidate(true)) return;
  try {
    await studioApi("/api/control/partial", {
      flow: Studio.name, step, environment: document.getElementById("studio-environment").value,
    });
    studioGo("runs");
    setTimeout(loadRuns, 500);
  } catch (error) { studioMessage(String(error), true); }
}

/* Functions the editor needs from its other script files (editor-*.js). */
// One line: tests/test_editor_v2.py reads this list.
const STUDIO_PARTS = ["studioCommit", "studioDraggable", "studioViewInit", "studioLoadVersions", "studioLoadRuns", "studioDecorate", "studioRenameStep", "studioFocusStep"];

/* If an older dashboard process can't serve the newer script files, say so instead of
 * showing an empty palette. */
function studioPartsMissing() {
  return STUDIO_PARTS.filter((name) => typeof window[name] !== "function");
}

/* The Examples and Runs tabs work even if the editor itself can't load. */
function studioNavInit() {
  document.getElementById("view-examples").onclick = () => studioGo("examples");
  document.getElementById("view-runs").onclick = () => studioGo("runs");
  document.getElementById("brand-home").onclick = (event) => { event.preventDefault(); studioGo("examples"); };
  // A link to a run (#run=…) opens on the run view, so mark the Runs tab.
  if (/run=/.test(location.hash)) studioNav("runs");
}

async function studioInit() {
  studioNavInit();
  const missing = studioPartsMissing();
  if (missing.length) {
    const button = document.getElementById("view-editor");
    button.hidden = false;
    const explain = () => studioMessage("Part of the editor didn't load (missing " + missing.join(", ")
      + "). The dashboard is probably older than these files: stop it (Ctrl+C), start it again "
      + "(miniflow ui or miniflow serve), then reload this page.", true);
    button.onclick = () => {
      try { studioShow(true); } catch { /* drawing the editor needs the missing parts */ }
      explain();
    };
    return;
  }
  try {
    const [catalog, , environments, subflows, templates] = await Promise.all([
      studioApi("/api/blocks"), studioList(), studioApi("/api/environments"),
      studioApi("/api/subflows"), studioApi("/api/templates"),
    ]);
    Studio.blocks = new Map(catalog.map((block) => [block.name, block]));
    Studio.environments = environments;
    Studio.subflows = new Map(subflows.map((flow) => [flow.name, flow]));
    document.getElementById("studio-environment").replaceChildren(
      new Option("Default", ""), ...environments.map((name) => new Option(name, name)));
    document.getElementById("studio-template").replaceChildren(...templates.map((item) =>
      new Option(`${item.name} — ${item.description}`, item.name)));
    document.getElementById("view-editor").hidden = false;
    document.getElementById("view-editor").onclick = () => studioShow(true);
    document.getElementById("studio-flow").onchange = (event) => studioOpen(event.target.value);
    document.getElementById("studio-new").onclick = studioNew;
    document.getElementById("studio-use-template").onclick = studioUseTemplate;
    document.getElementById("studio-search").oninput = studioPalette;
    document.getElementById("studio-validate").onclick = () => studioValidate(false);
    document.getElementById("studio-test").onclick = studioTest;
    document.getElementById("studio-save").onclick = () => studioValidate(true);
    document.getElementById("studio-run").onclick = studioRun;
    document.getElementById("studio-stop").onclick = studioStop;
    document.getElementById("studio-partial").onclick = studioPartial;
    document.getElementById("studio-undo").onclick = () => studioUndoRedo(-1);
    document.getElementById("studio-redo").onclick = () => studioUndoRedo(1);
    document.getElementById("studio-copy").onclick = studioCopy;
    document.getElementById("studio-paste").onclick = studioPaste;
    document.getElementById("studio-duplicate").onclick = studioDuplicate;
    document.getElementById("studio-tidy").onclick = studioTidy;
    document.getElementById("studio-zoom-in").onclick = () => studioZoomButton(1.25);
    document.getElementById("studio-zoom-out").onclick = () => studioZoomButton(0.8);
    document.getElementById("studio-fit").onclick = studioFit;
    document.getElementById("studio-run-data").onchange = () => studioLoadRuns();
    for (const name of STUDIO_PANELS) {
      document.getElementById(`studio-${name}-tab`).onclick = () => studioSelectPanel(name);
    }
    document.querySelector(".studio-section-tabs").onkeydown = studioPanelKeydown;
    studioViewInit();
    studioHistoryReset();
    if (location.hash === "#editor") studioShow(true);
  } catch { /* read-only dashboard has no editor */ }
}

studioInit();
