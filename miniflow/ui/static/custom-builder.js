/* Separate custom block builder. Definitions are saved in the user-wide library. */
"use strict";

const Custom = { definition: null, revision: null, selected: null, dirty: false, catalog: [] };
const CUSTOM_TYPES = ["str", "int", "float", "bool", "list", "dict"];

function customBlank(kind = "visual") {
  return { name: "", description: "", kind, inputs: {},
    outputs: kind === "visual" ? { result: "" } : ["result"],
    ...(kind === "visual" ? { steps: {} } : { code: "return {'result': None}", execution: "io" }) };
}

function customMessage(message, error = false) {
  const host = document.getElementById("custom-message");
  host.textContent = message;
  host.classList.toggle("error", error);
}

function customChanged() { Custom.dirty = true; }

function customInputValue(raw, type) {
  if (type === "str" && !raw.startsWith('"')) return raw;
  try { return JSON.parse(raw); } catch { return raw; }
}

function customField(labelText, value, onChange, { textarea = false, placeholder = "" } = {}) {
  const label = document.createElement("label"); label.textContent = labelText;
  const input = document.createElement(textarea ? "textarea" : "input");
  input.value = value; input.placeholder = placeholder;
  input.onchange = () => onChange(input.value);
  label.append(input); return label;
}

function customRenderInputs() {
  const host = document.getElementById("custom-inputs"); host.replaceChildren();
  for (const [key, spec] of Object.entries(Custom.definition.inputs)) {
    const row = document.createElement("div"); row.className = "custom-input-row";
    const name = document.createElement("input"); name.value = key; name.setAttribute("aria-label", "Input name");
    name.onchange = () => {
      const next = name.value.trim();
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(next) || (next !== key && next in Custom.definition.inputs)) {
        customMessage("Input names must be unique and use letters, numbers or _.", true); name.value = key; return;
      }
      Custom.definition.inputs = Object.fromEntries(Object.entries(Custom.definition.inputs)
        .map(([item, value]) => [item === key ? next : item, value]));
      customChanged(); customRender();
    };
    const type = document.createElement("select");
    type.append(...CUSTOM_TYPES.map((item) => new Option(item, item))); type.value = spec.type || "str";
    type.setAttribute("aria-label", `Type of ${key}`);
    type.onchange = () => { spec.type = type.value; delete spec.default; customChanged(); customRenderInputs(); };
    const optional = document.createElement("label"); optional.className = "custom-check";
    const optionalBox = document.createElement("input"); optionalBox.type = "checkbox";
    optionalBox.checked = Object.hasOwn(spec, "default");
    optionalBox.onchange = () => { if (optionalBox.checked) spec.default = spec.type === "str" ? "" : null; else delete spec.default; customChanged(); customRenderInputs(); };
    optional.append(optionalBox, "Default");
    const def = document.createElement("input"); def.value = Object.hasOwn(spec, "default") ? JSON.stringify(spec.default) : "";
    def.placeholder = "Value"; def.disabled = !optionalBox.checked; def.setAttribute("aria-label", `Default for ${key}`);
    def.onchange = () => { spec.default = customInputValue(def.value, spec.type || "str"); customChanged(); };
    const help = document.createElement("input"); help.value = spec.help || ""; help.placeholder = "Help text";
    help.setAttribute("aria-label", `Help for ${key}`);
    help.onchange = () => { spec.help = help.value; customChanged(); };
    const remove = document.createElement("button"); remove.type = "button"; remove.className = "btn danger"; remove.textContent = "Remove";
    remove.onclick = () => { delete Custom.definition.inputs[key]; customChanged(); customRender(); };
    row.append(name, type, optional, def, help, remove);
    host.append(row);
  }
}

function customRenderOutputs() {
  const host = document.getElementById("custom-outputs"); host.replaceChildren();
  const visual = Custom.definition.kind === "visual";
  const names = visual ? Object.keys(Custom.definition.outputs) : Custom.definition.outputs;
  for (const key of names) {
    const row = document.createElement("div"); row.className = "custom-output-row";
    const name = document.createElement("input"); name.value = key; name.setAttribute("aria-label", "Output name");
    name.onchange = () => {
      const next = name.value.trim();
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(next) || (next !== key && names.includes(next))) {
        customMessage("Output names must be unique and use letters, numbers or _.", true); name.value = key; return;
      }
      if (visual) Custom.definition.outputs = Object.fromEntries(Object.entries(Custom.definition.outputs).map(([item, value]) => [item === key ? next : item, value]));
      else Custom.definition.outputs = names.map((item) => item === key ? next : item);
      customChanged(); customRenderOutputs();
    };
    row.append(name);
    if (visual) {
      const value = document.createElement("input"); value.value = Custom.definition.outputs[key];
      value.placeholder = "{{ step }}"; value.setAttribute("aria-label", `Value for output ${key}`);
      value.onchange = () => { Custom.definition.outputs[key] = value.value; customChanged(); };
      const source = document.createElement("select"); source.setAttribute("aria-label", `Source for ${key}`);
      source.append(new Option("Use step result…", ""), ...Object.keys(Custom.definition.steps).map((step) => new Option(step, `{{ ${step} }}`)));
      source.onchange = () => { if (source.value) { Custom.definition.outputs[key] = source.value; value.value = source.value; customChanged(); } };
      row.append(value, source);
    }
    const remove = document.createElement("button"); remove.type = "button"; remove.className = "btn danger"; remove.textContent = "Remove";
    remove.onclick = () => {
      if (visual) delete Custom.definition.outputs[key];
      else Custom.definition.outputs = names.filter((item) => item !== key);
      customChanged(); customRenderOutputs();
    };
    row.append(remove); host.append(row);
  }
}

function customRenderCanvas() {
  const host = document.getElementById("custom-canvas"); host.replaceChildren();
  const steps = Custom.definition.steps;
  const wires = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  wires.classList.add("custom-wires"); wires.setAttribute("aria-hidden", "true"); host.append(wires);
  for (const [name, step] of Object.entries(steps)) {
    const button = document.createElement("button"); button.type = "button";
    button.className = `custom-node${Custom.selected === name ? " selected" : ""}`;
    button.dataset.step = name;
    const title = document.createElement("strong"); title.textContent = name;
    const type = document.createElement("small"); type.textContent = step.block;
    const deps = document.createElement("small"); deps.textContent = (step.after || []).length ? `← ${step.after.join(", ")}` : "Starts here";
    button.append(title, type, deps); button.onclick = () => { Custom.selected = name; customRenderCanvas(); customRenderStep(); };
    host.append(button);
  }
  if (!Object.keys(steps).length) host.append(Object.assign(document.createElement("p"), { className: "hint", textContent: "Add a built-in block to start your custom block." }));
  requestAnimationFrame(() => {
    if (!host.isConnected || Custom.definition.steps !== steps) return;
    wires.replaceChildren();
    const rect = host.getBoundingClientRect();
    for (const [name, step] of Object.entries(steps)) {
      const target = [...host.querySelectorAll(".custom-node")].find((node) => node.dataset.step === name);
      const references = [...JSON.stringify(step.settings || {}).matchAll(/\{\{\s*([A-Za-z_]\w*)/g)].map((match) => match[1]);
      for (const dep of new Set([...(step.after || []), ...references])) {
        const source = [...host.querySelectorAll(".custom-node")].find((node) => node.dataset.step === dep);
        if (!source || !target) continue;
        const a = source.getBoundingClientRect(), b = target.getBoundingClientRect();
        const x1 = a.right - rect.left, y1 = a.top + a.height / 2 - rect.top;
        const x2 = b.left - rect.left, y2 = b.top + b.height / 2 - rect.top;
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", `M ${x1} ${y1} C ${x1 + 25} ${y1}, ${x2 - 25} ${y2}, ${x2} ${y2}`);
        wires.append(path);
      }
    }
  });
}

function customRenderStep() {
  const host = document.getElementById("custom-step-settings"); host.replaceChildren();
  const name = Custom.selected, step = Custom.definition.steps[name];
  if (!step) return;
  const spec = Custom.catalog.find((item) => item.name === step.block);
  const header = document.createElement("h3"); header.textContent = `${name} · ${step.block}`; host.append(header);
  host.append(customField("Step name", name, (next) => {
    next = next.trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(next) || (next !== name && next in Custom.definition.steps)) {
      customMessage("Step names must be unique and use letters, numbers or _.", true); customRenderStep(); return;
    }
    Custom.definition.steps = Object.fromEntries(Object.entries(Custom.definition.steps).map(([key, value]) => [key === name ? next : key, value]));
    for (const other of Object.values(Custom.definition.steps)) other.after = (other.after || []).map((item) => item === name ? next : item);
    Custom.selected = next; customChanged(); customRender();
  }));
  const dependency = document.createElement("select"); dependency.setAttribute("aria-label", "Add dependency");
  dependency.append(new Option("Runs after…", ""), ...Object.keys(Custom.definition.steps)
    .filter((other) => other !== name && !(step.after || []).includes(other)).map((other) => new Option(other, other)));
  dependency.onchange = () => { if (dependency.value) { (step.after ||= []).push(dependency.value); customChanged(); customRender(); } };
  host.append(dependency);
  for (const dep of step.after || []) {
    const remove = document.createElement("button"); remove.type = "button"; remove.className = "btn";
    remove.textContent = `× ${dep}`; remove.onclick = () => { step.after = step.after.filter((item) => item !== dep); customChanged(); customRender(); };
    host.append(remove);
  }
  for (const setting of spec?.settings || []) {
    const value = step.settings[setting.name];
    const raw = typeof value === "string" ? value : JSON.stringify(value);
    host.append(customField(`${setting.name}${setting.required ? " *" : ""}`, raw, (next) => {
      step.settings[setting.name] = studioValue(next, setting.types); customChanged(); customRenderCanvas();
    }, { textarea: setting.types.some((type) => ["list", "dict"].includes(type)), placeholder: setting.help || "" }));
    const source = document.createElement("select"); source.setAttribute("aria-label", `Use value for ${setting.name}`);
    source.append(new Option("Use value from…", ""),
      ...Object.keys(Custom.definition.inputs).map((input) => new Option(`Input: ${input}`, `{{ params.${input} }}`)),
      ...Object.keys(Custom.definition.steps).filter((item) => item !== name).map((item) => new Option(`Step: ${item}`, `{{ ${item} }}`)));
    source.onchange = () => { if (source.value) { step.settings[setting.name] = source.value; customChanged(); customRenderCanvas(); customRenderStep(); } };
    host.append(source);
  }
  const remove = document.createElement("button"); remove.type = "button"; remove.className = "btn danger"; remove.textContent = "Remove step";
  remove.onclick = () => { delete Custom.definition.steps[name]; Custom.selected = null; customChanged(); customRender(); };
  host.append(remove);
}

function customRender() {
  if (!Custom.definition) return;
  const def = Custom.definition, visual = def.kind === "visual";
  document.getElementById("custom-name").value = def.name;
  document.getElementById("custom-name").disabled = !!Custom.revision;
  document.getElementById("custom-description").value = def.description;
  document.getElementById("custom-kind").value = def.kind;
  document.getElementById("custom-kind").disabled = !!Custom.revision;
  document.getElementById("custom-visual").hidden = !visual;
  document.getElementById("custom-python").hidden = visual;
  document.getElementById("custom-output-hint").textContent = visual
    ? "Use a step result such as {{ filter }} for a no-code output."
    : "Return an object containing each output name from your Python code.";
  if (visual) { customRenderCanvas(); customRenderStep(); }
  else {
    document.getElementById("custom-code").value = def.code;
    document.getElementById("custom-execution").value = def.execution;
  }
  customRenderInputs(); customRenderOutputs();
}

async function customRefreshList(selected = "") {
  const list = await studioApi("/api/custom-blocks");
  Studio.customBlocks = new Map(list.map((block) => [block.name, block]));
  const picker = document.getElementById("custom-list");
  picker.replaceChildren(new Option("New block…", ""), ...list.map((block) => new Option(block.name, block.name)));
  picker.value = selected;
  studioPalette();
}

function customNew(kind = "visual") {
  if (Custom.dirty && !window.confirm("Discard unsaved custom block changes?")) return;
  Custom.definition = customBlank(kind); Custom.revision = null; Custom.selected = null; Custom.dirty = false;
  document.getElementById("custom-test-settings").value = "{}";
  document.getElementById("custom-test-expected").value = "{}";
  document.getElementById("custom-list").value = "";
  customMessage("Choose a name, add inputs and outputs, then save your block."); customRender();
}

async function customOpen(name) {
  if (!name) { customNew(); return; }
  if (Custom.dirty && !window.confirm("Discard unsaved custom block changes?")) {
    document.getElementById("custom-list").value = Custom.revision ? Custom.definition.name : ""; return;
  }
  try {
    const definition = await studioApi(`/api/custom-blocks/${encodeURIComponent(name)}`);
    Custom.revision = definition.revision;
    Custom.definition = { ...definition }; delete Custom.definition.revision;
    Custom.selected = null; Custom.dirty = false;
    document.getElementById("custom-test-settings").value = "{}";
    document.getElementById("custom-test-expected").value = "{}";
    customMessage(`Editing ${name}. Changes apply to future runs after saving.`); customRender();
  } catch (error) { customMessage(String(error), true); }
}

function customReadMeta() {
  Custom.definition.name = document.getElementById("custom-name").value.trim();
  Custom.definition.description = document.getElementById("custom-description").value.trim();
  if (Custom.definition.kind === "python") {
    Custom.definition.code = document.getElementById("custom-code").value;
    Custom.definition.execution = document.getElementById("custom-execution").value;
  }
}

async function customValidate(save = false) {
  customReadMeta();
  try {
    const result = await studioApi(`/api/custom-blocks/${save ? "save" : "validate"}`, {
      definition: Custom.definition, expected_revision: Custom.revision,
    });
    if (save) {
      Custom.revision = result.revision; Custom.dirty = false;
      await customRefreshList(result.name); customRender();
      customMessage(`${result.name} saved. It is now available in every workflow.`);
    } else customMessage("Block is valid and ready to save.");
    return true;
  } catch (error) { customMessage(String(error), true); return false; }
}

async function customTest() {
  if (Custom.dirty || !Custom.revision) { customMessage("Save the block before testing it.", true); return; }
  try {
    const settings = JSON.parse(document.getElementById("custom-test-settings").value);
    const expected = JSON.parse(document.getElementById("custom-test-expected").value);
    const reports = await studioApi("/api/custom-blocks/test", { name: Custom.definition.name, settings, expected });
    const result = reports[0];
    customMessage(result.passed ? "Test passed." : `Test failed: ${result.problems.join("; ")}`, !result.passed);
  } catch (error) { customMessage(String(error), true); }
}

function customInit(catalog, customBlocks) {
  Custom.catalog = catalog.filter((block) => block.group !== "Plugins");
  Custom.definition = customBlank();
  const addType = document.getElementById("custom-add-type");
  addType.replaceChildren(...Custom.catalog.map((block) => new Option(`${block.name} — ${block.description}`, block.name)));
  document.getElementById("custom-list").replaceChildren(new Option("New block…", ""),
    ...customBlocks.map((block) => new Option(block.name, block.name)));
  document.getElementById("custom-list").onchange = (event) => customOpen(event.target.value);
  document.getElementById("custom-new").onclick = () => customNew();
  document.getElementById("custom-name").oninput = (event) => { Custom.definition.name = event.target.value; customChanged(); };
  document.getElementById("custom-description").oninput = (event) => { Custom.definition.description = event.target.value; customChanged(); };
  document.getElementById("custom-kind").onchange = (event) => {
    const next = event.target.value;
    customNew(next);
    event.target.value = Custom.definition.kind;
  };
  document.getElementById("custom-add-input").onclick = () => {
    let number = 1; while (`input_${number}` in Custom.definition.inputs) number++;
    Custom.definition.inputs[`input_${number}`] = { type: "str" }; customChanged(); customRenderInputs();
  };
  document.getElementById("custom-add-output").onclick = () => {
    let number = 1; const outputs = Custom.definition.outputs;
    while (Custom.definition.kind === "visual" ? `output_${number}` in outputs : outputs.includes(`output_${number}`)) number++;
    if (Custom.definition.kind === "visual") outputs[`output_${number}`] = "";
    else outputs.push(`output_${number}`);
    customChanged(); customRenderOutputs();
  };
  document.getElementById("custom-add-step").onclick = () => {
    const type = addType.value, spec = Custom.catalog.find((item) => item.name === type);
    if (!spec) return;
    let name = type, number = 2; while (name in Custom.definition.steps) name = `${type}_${number++}`;
    const settings = Object.fromEntries(spec.settings.map((item) => [item.name, studioDefault(item)]));
    Custom.definition.steps[name] = { block: type, settings, ...(Custom.selected ? { after: [Custom.selected] } : {}) };
    Custom.selected = name; customChanged(); customRender();
  };
  document.getElementById("custom-code").oninput = (event) => { Custom.definition.code = event.target.value; customChanged(); };
  document.getElementById("custom-execution").onchange = (event) => { Custom.definition.execution = event.target.value; customChanged(); };
  document.getElementById("custom-validate").onclick = () => customValidate(false);
  document.getElementById("custom-save").onclick = () => customValidate(true);
  document.getElementById("custom-test").onclick = customTest;
  customRender();
}
