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
  studioNav(show ? "editor" : Studio.section === "editor" ? "examples" : Studio.section);
  try { history.replaceState(null, "", show ? "#editor" : S.runId ? `#run=${S.runId}` : location.pathname); }
  catch { /* sandboxed */ }
  if (show) { studioRender(); studioLoadRuns(); studioScheduleCheck(); }
  else loadRuns();
}

/* Top navigation: Examples | Runs | Editor. The current section is marked for the eye and
 * for screen readers. */
function studioNav(section) {
  Studio.section = section;
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
  const target = section === "examples"
    ? document.getElementById("gallery")
    : document.getElementById("dashboard").hidden ? document.querySelector(".runs-panel") : document.getElementById("dashboard");
  if (section === "examples" && target) target.open = true;
  window.scrollTo({ top: 0 });
  if (target) target.scrollIntoView({ block: "start", behavior: "smooth" });
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

function studioAdd(blockName, at) {
  const spec = Studio.blocks.get(blockName);
  if (!spec) return;
  let name = blockName.replace(/[^A-Za-z0-9_]/g, "_");
  let index = 2;
  while (Object.hasOwn(Studio.flow.steps, name)) name = `${blockName}_${index++}`;
  const count = Object.keys(Studio.flow.steps).length;
  Studio.flow.steps[name] = {
    block: blockName, settings: Object.fromEntries(spec.settings.map((s) => [s.name, studioDefault(s)])),
    position: studioSlot(at, count),
  };
  Studio.selected = name;
  Studio.multi = new Set();
  studioCommit();
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
    studioDraggable(button, "block", b.name);
    return button;
  }));
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
    output.type = "button"; output.title = "Connect to another block"; output.textContent = "+";
    output.onclick = (event) => { event.stopPropagation(); Studio.connecting = name; studioRender(); studioMessage(`Select the input port of the block after ${name}.`); };
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
  studioRenderTests();
  studioRenderSchedules();
  studioRenderParams();
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
    const row = document.createElement("div"); row.className = "studio-test-row";
    const name = document.createElement("input"); name.value = schedule.name;
    name.setAttribute("aria-label", `Schedule ${index + 1} name`);
    name.onchange = () => { schedule.name = name.value.trim(); studioCommit(); };
    const type = document.createElement("select"); type.setAttribute("aria-label", `Schedule ${index + 1} type`);
    type.append(new Option("Every N seconds", "interval_seconds"), new Option("Daily at local time", "daily"));
    type.value = schedule.daily !== undefined ? "daily" : "interval_seconds";
    type.onchange = () => {
      delete schedule.daily; delete schedule.interval_seconds;
      if (type.value === "daily") schedule.daily = "09:00";
      else schedule.interval_seconds = 60;
      studioCommit(); studioRenderSchedules();
    };
    const value = document.createElement("input");
    value.setAttribute("aria-label", `Schedule ${index + 1} value`);
    value.type = type.value === "daily" ? "time" : "number";
    if (value.type === "number") { value.min = "1"; value.step = "1"; }
    value.value = String(type.value === "daily" ? schedule.daily : schedule.interval_seconds);
    value.onchange = () => {
      if (type.value === "daily") schedule.daily = value.value;
      else schedule.interval_seconds = Number(value.value);
      studioCommit();
    };
    const remove = document.createElement("button"); remove.className = "btn danger";
    remove.type = "button"; remove.textContent = "Remove";
    remove.onclick = () => { schedules.splice(index, 1); studioCommit(); studioRenderSchedules(); };
    row.append(name, type, value, remove);
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
    document.getElementById("studio-test-panel").open = true;
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
  const child = step.flow ? Studio.subflows.get(step.flow.replace(/\.json$/, "")) : null;
  const fields = spec ? spec.settings : child ? Object.entries(child.params || {}).map(([key, def]) => ({
    name: key, types: [def.type || "str"], required: !Object.hasOwn(def, "default"),
    ...(Object.hasOwn(def, "default") ? { default: def.default } : {}),
    help: def.help || "", secret: false,
  })) : [];
  const form = document.createElement("form"); form.onsubmit = (event) => event.preventDefault();
  step.settings ||= {};
  for (const setting of fields) {
    const label = document.createElement("label");
    label.textContent = `${setting.name}${setting.required ? " *" : ""}`;
    const value = step.settings?.[setting.name] ?? studioDefault(setting);
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
    field.onchange = () => { step.settings[setting.name] = studioValue(field.value, setting.types); studioCommit(); };
    label.append(field);
    if (!(field instanceof HTMLSelectElement)) {
      const source = document.createElement("select");
      source.setAttribute("aria-label", `Use value for ${setting.name}`);
      source.append(new Option("Use value from…", ""),
        ...Object.keys(Studio.flow.params || {}).map((key) => new Option(`Parameter: ${key}`, `{{ params.${key} }}`)),
        ...Object.keys(Studio.flow.steps).filter((key) => key !== name)
          .map((key) => new Option(`Step: ${key}`, `{{ ${key} }}`)));
      source.onchange = () => {
        if (!source.value) return;
        field.value = source.value;
        step.settings[setting.name] = source.value;
        studioCommit();
      };
      label.append(source);
    }
    if (setting.help) { const hint = document.createElement("span"); hint.className = "hint"; hint.textContent = setting.help; label.append(hint); }
    form.append(label);
  }
  const after = document.createElement("label"); after.textContent = "Runs after";
  const picker = document.createElement("select");
  picker.append(new Option("Add dependency…", ""), ...Object.keys(Studio.flow.steps)
    .filter((other) => other !== name && !(step.after || []).includes(other)).map((other) => new Option(other, other)));
  picker.onchange = () => { if (picker.value) { (step.after ||= []).push(picker.value); studioCommit(); studioRender(); } };
  after.append(picker); form.append(after);
  if (step.after?.length) {
    const list = document.createElement("p"); list.className = "hint";
    list.textContent = `Dependencies: ${step.after.join(", ")}`; form.append(list);
  }
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
    await studioApi("/api/control/partial", {
      flow: Studio.name, step, environment: document.getElementById("studio-environment").value,
    });
    studioShow(false);
    setTimeout(loadRuns, 500);
  } catch (error) { studioMessage(String(error), true); }
}

/* Functions the editor needs from its other script files (editor-*.js). */
const STUDIO_PARTS = ["studioCommit", "studioDraggable", "studioViewInit", "studioLoadVersions", "studioLoadRuns", "studioDecorate"];

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
    document.getElementById("studio-history-panel").ontoggle = (event) => {
      if (event.target.open) studioLoadVersions();
    };
    studioViewInit();
    studioHistoryReset();
    if (location.hash === "#editor") studioShow(true);
  } catch { /* read-only dashboard has no editor */ }
}

studioInit();
