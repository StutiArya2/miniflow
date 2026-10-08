/* MiniFlow Studio: local visual editor for the JSON flow format. */
"use strict";

const Studio = {
  blocks: new Map(), flow: { steps: {} }, name: "", selected: null,
  connecting: null, problems: [], dirty: false, visible: false,
  testIndex: 0,
};

function studioMessage(message, error = false) {
  const el = document.getElementById("studio-message");
  el.textContent = message;
  el.classList.toggle("error", error);
}

function studioShow(show) {
  Studio.visible = show;
  document.body.classList.toggle("studio-active", show);
  document.getElementById("studio").hidden = !show;
  document.getElementById("view-editor").hidden = show;
  document.getElementById("view-runs").hidden = !show;
  if (show) studioRender();
  else loadRuns();
}

async function studioApi(path, data) {
  const response = await fetch(path, data === undefined ? { cache: "no-store" } : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok && !Array.isArray(result.problems)) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
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
    document.getElementById("studio-name").value = name;
    studioShow(true);
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
  Studio.dirty = true;
  document.getElementById("studio-name").value = Studio.name;
  studioShow(true);
  studioMessage("Add a block from the palette to begin.");
}

function studioDefault(setting) {
  if (Object.hasOwn(setting, "default")) return setting.default;
  const kind = setting.types[0];
  return kind === "list" ? [] : kind === "dict" ? {} : kind === "bool" ? false
    : kind === "int" || kind === "float" ? 0 : "";
}

function studioAdd(blockName) {
  const spec = Studio.blocks.get(blockName);
  if (!spec) return;
  let name = blockName.replace(/[^A-Za-z0-9_]/g, "_");
  let index = 2;
  while (Object.hasOwn(Studio.flow.steps, name)) name = `${blockName}_${index++}`;
  const count = Object.keys(Studio.flow.steps).length;
  Studio.flow.steps[name] = {
    block: blockName, settings: Object.fromEntries(spec.settings.map((s) => [s.name, studioDefault(s)])),
    position: { x: 90 + (count % 4) * 250, y: 90 + Math.floor(count / 4) * 150 },
  };
  Studio.selected = name;
  Studio.dirty = true;
  studioRender();
  studioMessage(`${name} added. Configure it in the settings panel.`);
}

function studioPalette() {
  const query = document.getElementById("studio-search").value.trim().toLowerCase();
  const items = [...Studio.blocks.values()].filter((b) =>
    `${b.name} ${b.description}`.toLowerCase().includes(query));
  const list = document.getElementById("studio-blocks");
  list.replaceChildren(...items.map((b) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "studio-block";
    button.textContent = b.name;
    const small = document.createElement("small");
    small.textContent = `${b.kind} · ${b.description}`;
    button.append(small);
    button.onclick = () => studioAdd(b.name);
    return button;
  }));
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

function studioWires(positions) {
  const svg = document.getElementById("studio-wires");
  const edges = [];
  for (const [name, step] of Object.entries(Studio.flow.steps)) {
    const inferred = [...JSON.stringify(step.settings || {}).matchAll(/\{\{\s*([A-Za-z_]\w*)/g)]
      .map((match) => match[1]);
    for (const dep of new Set([...(step.after || []), ...inferred])) {
      if (dep in Studio.flow.steps && dep !== name) {
        const line = studioWire(dep, name, positions);
        if (line) edges.push(line);
      }
    }
  }
  svg.replaceChildren(...edges);
}

function studioConnect(target) {
  const source = Studio.connecting;
  Studio.connecting = null;
  if (!source || source === target) return;
  const after = Studio.flow.steps[target].after || [];
  if (!after.includes(source)) after.push(source);
  Studio.flow.steps[target].after = after;
  Studio.dirty = true;
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
    node.className = "studio-node" + (Studio.selected === name ? " selected" : "")
      + (Studio.problems.some((p) => p.includes(`step '${name}'`)) ? " invalid" : "")
      + (Studio.connecting === name ? " connecting" : "");
    node.style.left = `${pos.x}px`; node.style.top = `${pos.y}px`;
    const title = document.createElement("strong"); title.textContent = name;
    const kind = document.createElement("small"); kind.textContent = step.block || `Sub-flow: ${step.flow}`;
    const input = document.createElement("button"); input.className = "input-port";
    input.type = "button"; input.title = "Connect a preceding block"; input.textContent = "●";
    input.onclick = (event) => { event.stopPropagation(); studioConnect(name); };
    const output = document.createElement("button"); output.className = "output-port";
    output.type = "button"; output.title = "Connect to another block"; output.textContent = "+";
    output.onclick = (event) => { event.stopPropagation(); Studio.connecting = name; studioRender(); studioMessage(`Select the input port of the block after ${name}.`); };
    node.append(input, title, kind, output);
    node.onclick = () => { Studio.selected = name; studioRender(); };
    node.onpointerdown = (event) => {
      if (event.target instanceof HTMLButtonElement) return;
      const startX = event.clientX, startY = event.clientY, initial = { ...pos };
      node.setPointerCapture(event.pointerId);
      node.onpointermove = (move) => {
        if (!node.hasPointerCapture(move.pointerId)) return;
        step.position = { x: Math.max(10, initial.x + move.clientX - startX), y: Math.max(10, initial.y + move.clientY - startY) };
        node.style.left = `${step.position.x}px`; node.style.top = `${step.position.y}px`;
        positions[name] = step.position; studioWires(positions); Studio.dirty = true;
      };
      node.onpointerup = () => { node.onpointermove = null; };
    };
    return node;
  }));
  studioWires(positions);
  studioInspector();
  studioRenderTests();
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
      if (picker.value !== step) { values[picker.value] = values[step]; delete values[step]; Studio.dirty = true; studioRenderTests(); }
    };
    const field = document.createElement("textarea");
    field.setAttribute("aria-label", `${key} value for ${step}`);
    field.value = JSON.stringify(value, null, 2);
    field.onchange = () => { values[step] = studioTestValue(field.value); Studio.dirty = true; };
    const remove = document.createElement("button"); remove.type = "button";
    remove.className = "btn"; remove.textContent = "Remove";
    remove.onclick = () => { delete values[step]; Studio.dirty = true; studioRenderTests(); };
    row.append(picker, field, remove); group.append(row);
  }
  const add = document.createElement("button"); add.type = "button";
  add.className = "btn"; add.textContent = key === "inputs" ? "Add sample" : "Add expectation";
  add.onclick = () => {
    const next = names.find((name) => !Object.hasOwn(values, name));
    if (!next) { studioMessage("Add a step to the canvas first.", true); return; }
    values[next] = null; Studio.dirty = true; studioRenderTests();
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
    Studio.testIndex = cases.length - 1; Studio.dirty = true;
    document.getElementById("studio-test-panel").open = true;
    studioRenderTests();
  };
  toolbar.append(select, add);
  if (!cases.length) { host.replaceChildren(toolbar); return; }
  const current = cases[Studio.testIndex];
  const label = document.createElement("label"); label.textContent = "Test name ";
  const name = document.createElement("input"); name.value = current.name;
  name.setAttribute("aria-label", "Test name");
  name.onchange = () => { current.name = name.value; Studio.dirty = true; studioRenderTests(); };
  label.append(name); toolbar.append(label);
  const remove = document.createElement("button"); remove.type = "button"; remove.className = "btn danger"; remove.textContent = "Delete test";
  remove.onclick = () => { cases.splice(Studio.testIndex, 1); Studio.testIndex = 0; Studio.dirty = true; studioRenderTests(); };
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
    document.getElementById("studio-test-panel").open = true;
  } catch (error) { studioMessage(String(error), true); }
}

function studioValue(raw, types) {
  if (typeof raw !== "string" || raw.includes("{{")) return raw;
  if (types.some((t) => ["list", "dict", "int", "float", "bool"].includes(t))) {
    try { return JSON.parse(raw); } catch { return raw; }
  }
  return raw;
}

function studioInspector() {
  const host = document.getElementById("studio-settings");
  const name = Studio.selected, step = Studio.flow.steps[name];
  document.getElementById("studio-inspector-title").textContent = name ? name : "Settings";
  if (!step) { host.textContent = "Select a node on the canvas."; return; }
  const spec = Studio.blocks.get(step.block);
  const form = document.createElement("form"); form.onsubmit = (event) => event.preventDefault();
  if (spec) for (const setting of spec.settings) {
    const label = document.createElement("label");
    label.textContent = `${setting.name}${setting.required ? " *" : ""}`;
    const value = step.settings?.[setting.name] ?? studioDefault(setting);
    const field = setting.types.includes("list") || setting.types.includes("dict")
      ? document.createElement("textarea") : document.createElement("input");
    if (field instanceof HTMLInputElement) field.type = setting.secret ? "password" : "text";
    field.value = typeof value === "string" ? value : JSON.stringify(value);
    field.placeholder = setting.secret ? "{{ env.NAME }}" : "";
    field.onchange = () => { step.settings[setting.name] = studioValue(field.value, setting.types); Studio.dirty = true; };
    label.append(field);
    if (setting.help) { const hint = document.createElement("span"); hint.className = "hint"; hint.textContent = setting.help; label.append(hint); }
    form.append(label);
  }
  const after = document.createElement("label"); after.textContent = "Runs after";
  const picker = document.createElement("select");
  picker.append(new Option("Add dependency…", ""), ...Object.keys(Studio.flow.steps)
    .filter((other) => other !== name && !(step.after || []).includes(other)).map((other) => new Option(other, other)));
  picker.onchange = () => { if (picker.value) { (step.after ||= []).push(picker.value); Studio.dirty = true; studioRender(); } };
  after.append(picker); form.append(after);
  if (step.after?.length) {
    const list = document.createElement("p"); list.className = "hint";
    list.textContent = `Dependencies: ${step.after.join(", ")}`; form.append(list);
  }
  const remove = document.createElement("button"); remove.type = "button"; remove.className = "btn danger"; remove.textContent = "Delete block";
  remove.onclick = () => {
    delete Studio.flow.steps[name];
    for (const other of Object.values(Studio.flow.steps)) other.after = (other.after || []).filter((dep) => dep !== name);
    Studio.selected = null; Studio.dirty = true; studioRender();
  };
  form.append(remove); host.replaceChildren(form);
}

async function studioValidate(save = false) {
  Studio.name = document.getElementById("studio-name").value.trim();
  try {
    const result = await studioApi(`/api/flows/${save ? "save" : "validate"}`, { name: Studio.name, flow: Studio.flow });
    Studio.problems = result.problems || [];
    studioRender();
    studioMessage(result.valid ? (save ? `Saved ${Studio.name}.json` : "Workflow is valid.")
      : Studio.problems.join("\n"), !result.valid);
    if (save && result.valid) { Studio.dirty = false; await studioList(); await loadControl(); }
    return result.valid;
  } catch (error) { studioMessage(String(error), true); return false; }
}

async function studioRun() {
  if (!await studioValidate(true)) return;
  try {
    await studioApi("/api/control/run", { flow: Studio.name, fail_rate: 0 });
    studioShow(false);
    studioMessage(`${Studio.name} started.`);
    setTimeout(loadRuns, 500);
  } catch (error) { studioMessage(String(error), true); }
}

async function studioPartial() {
  if (!Studio.selected) { studioMessage("Select a step on the canvas first.", true); return; }
  const step = Studio.selected;
  if (!await studioValidate(true)) return;
  try {
    await studioApi("/api/control/partial", { flow: Studio.name, step });
    studioShow(false);
    setTimeout(loadRuns, 500);
  } catch (error) { studioMessage(String(error), true); }
}

async function studioInit() {
  try {
    const [catalog] = await Promise.all([studioApi("/api/blocks"), studioList()]);
    Studio.blocks = new Map(catalog.map((block) => [block.name, block]));
    document.getElementById("view-editor").hidden = false;
    document.getElementById("view-editor").onclick = () => studioShow(true);
    document.getElementById("view-runs").onclick = () => studioShow(false);
    document.getElementById("studio-flow").onchange = (event) => studioOpen(event.target.value);
    document.getElementById("studio-new").onclick = studioNew;
    document.getElementById("studio-search").oninput = studioPalette;
    document.getElementById("studio-validate").onclick = () => studioValidate(false);
    document.getElementById("studio-test").onclick = studioTest;
    document.getElementById("studio-save").onclick = () => studioValidate(true);
    document.getElementById("studio-run").onclick = studioRun;
    document.getElementById("studio-partial").onclick = studioPartial;
    if (location.hash === "#editor") studioShow(true);
  } catch { /* read-only dashboard has no editor */ }
}

studioInit();
