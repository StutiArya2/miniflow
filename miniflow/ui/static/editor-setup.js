/* MiniFlow Studio: set up a flow one question at a time, then open the editor.
 *
 *   Start ─┬─ Open a saved flow → Which flow?                → Environment → editor
 *          └─ Create a new flow → Name → Starter (or blank)  → Environment → editor
 *
 * The environment step is skipped when the folder has no environments. Each answer
 * slides the next question in; Back slides the previous one back. The editor's "Flows"
 * button returns here.
 */
"use strict";

const StudioSetup = { step: "start", trail: [], mode: null, flow: "", name: "", template: "", busy: false };
const SETUP_STEPS = { open: ["start", "open", "environment"], new: ["start", "name", "starter", "environment"] };
const SETUP_NAME = /^[A-Za-z][A-Za-z0-9_-]*$/;

function setupEl(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node[key] = value;
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  node.append(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

const setupReduced = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/* A big clickable choice: title, a line of explanation and an optional badge. */
function setupChoice(title, detail, onclick, { badge = "", disabled = false, current = false } = {}) {
  return setupEl("button", { type: "button", class: `setup-choice${current ? " current" : ""}`, onclick, disabled },
    setupEl("span", { class: "setup-choice-text" },
      setupEl("strong", {}, title),
      detail ? setupEl("span", {}, detail) : null),
    badge ? setupEl("span", { class: "setup-badge" }, badge) : null,
    setupEl("span", { class: "setup-arrow", "aria-hidden": "true" }, "→"));
}

function setupHead(title, detail) {
  return [setupEl("h2", { class: "setup-title", tabindex: "-1" }, title), detail ? setupEl("p", { class: "setup-detail" }, detail) : null];
}

/* ------------------------------------------------------------------- steps */
function setupStartStep() {
  const saved = [...document.getElementById("studio-flow").options].filter((o) => o.value).length;
  const resume = Studio.name && Studio.stage === "setup" && Object.keys(Studio.flow.steps || {}).length;
  return [
    ...setupHead("What would you like to do?", "Open a flow you've saved, or build a new one step by step."),
    setupEl("div", { class: "setup-choices" },
      resume ? setupChoice(`Continue editing ${Studio.name}`, Studio.dirty ? "You have unsaved changes." : "Go back to the editor.",
        () => studioStage("edit"), { current: true }) : null,
      setupChoice("Open a saved flow", saved ? `${saved} flow${saved === 1 ? "" : "s"} in this folder` : "No saved flows yet",
        () => { StudioSetup.mode = "open"; setupGo("open"); }, { disabled: !saved }),
      setupChoice("Create a new flow", "Name it, then start blank or from a starter.",
        () => { StudioSetup.mode = "new"; setupGo("name"); })),
  ];
}

function setupOpenStep() {
  const names = [...document.getElementById("studio-flow").options].map((o) => o.value).filter(Boolean);
  const list = setupEl("div", { class: "setup-choices setup-list" });
  const draw = (filter = "") => list.replaceChildren(...names
    .filter((name) => name.toLowerCase().includes(filter.toLowerCase()))
    .map((name) => setupChoice(name, Studio.subflows.get(name)?.description || "", () => {
      StudioSetup.flow = name;
      setupNext();
    }, { current: name === Studio.name })));
  draw();
  const search = names.length > 6 ? setupEl("input", {
    class: "setup-input", placeholder: "Search flows…", "aria-label": "Search flows", autocomplete: "off",
  }) : null;
  if (search) search.oninput = () => draw(search.value);
  return [...setupHead("Which flow?", "Pick the flow to open in the editor."), search, list];
}

function setupNameStep() {
  const input = setupEl("input", {
    id: "setup-name", class: "setup-input", value: StudioSetup.name || "", autocomplete: "off", spellcheck: "false",
    placeholder: "e.g. weekly_sales_report", "aria-describedby": "setup-name-hint",
  });
  const hint = setupEl("p", { id: "setup-name-hint", class: "setup-hint" }, "Letters, numbers, _ or -, starting with a letter.");
  const submit = (event) => {
    event.preventDefault();
    const name = input.value.trim();
    const taken = [...document.getElementById("studio-flow").options].some((o) => o.value === name);
    const problem = !name ? "Give the flow a name."
      : !SETUP_NAME.test(name) ? "Use letters, numbers, _ or -, and start with a letter."
        : taken ? `A flow called ${name} already exists. Choose another name, or go back and open it.` : "";
    hint.textContent = problem || "Letters, numbers, _ or -, starting with a letter.";
    hint.classList.toggle("error", !!problem);
    input.setAttribute("aria-invalid", String(!!problem));
    if (problem) { input.focus(); return; }
    StudioSetup.name = name;
    setupGo("starter");
  };
  return [
    ...setupHead("Name your flow", "This is also its file name, so keep it short."),
    setupEl("form", { class: "setup-form", onsubmit: submit, novalidate: true },
      setupEl("label", { class: "sr-only", for: "setup-name" }, "Flow name"), input, hint,
      setupEl("div", { class: "setup-actions" }, setupEl("button", { type: "submit", class: "btn primary" }, "Continue"))),
  ];
}

function setupStarterStep() {
  const templates = [...document.getElementById("studio-template").options];
  const pick = (template) => { StudioSetup.template = template; setupNext(); };
  return [
    ...setupHead("How do you want to start?", "A starter is a small working flow you can change."),
    setupEl("div", { class: "setup-choices setup-list" },
      setupChoice("Blank canvas", "Add blocks yourself.", () => pick(""), { current: StudioSetup.template === "" }),
      templates.map((option) => {
        const [title, ...rest] = option.textContent.split(" — ");
        return setupChoice(title, rest.join(" — "), () => pick(option.value), { badge: "Starter", current: StudioSetup.template === option.value });
      })),
  ];
}

function setupEnvironmentStep() {
  const current = document.getElementById("studio-environment").value;
  const choose = (value) => { document.getElementById("studio-environment").value = value; setupFinish(); };
  return [
    ...setupHead("Which environment?", "Environments hold settings such as API keys and folders. You can switch later from the top bar."),
    setupEl("div", { class: "setup-choices" },
      setupChoice("Default", "The flow's own settings.", () => choose(""), { current: current === "" }),
      Studio.environments.map((name) => setupChoice(name, `environments/${name}.json`, () => choose(name), { current: current === name }))),
  ];
}

const SETUP_RENDER = {
  start: setupStartStep, open: setupOpenStep, name: setupNameStep,
  starter: setupStarterStep, environment: setupEnvironmentStep,
};

/* ---------------------------------------------------------------- movement */
function setupProgress() {
  const box = document.getElementById("setup-progress");
  const steps = StudioSetup.mode ? SETUP_STEPS[StudioSetup.mode].filter((s) => s !== "environment" || Studio.environments.length) : ["start"];
  const index = Math.max(0, steps.indexOf(StudioSetup.step));
  box.replaceChildren(
    setupEl("span", { class: "setup-count" }, steps.length > 1 ? `Step ${index + 1} of ${steps.length}` : "Get started"),
    setupEl("span", { class: "setup-dots", "aria-hidden": "true" },
      steps.map((_, i) => setupEl("span", { class: i < index ? "done" : i === index ? "now" : "" }))));
  document.getElementById("setup-back").hidden = StudioSetup.step === "start";
}

/* Show `step`, sliding forward (1) or back (-1). */
function setupShow(step, direction = 1) {
  const host = document.getElementById("setup-step");
  StudioSetup.step = step;
  const draw = () => {
    host.classList.remove("leaving", "enter-forward", "enter-back");
    host.replaceChildren(...SETUP_RENDER[step]().filter(Boolean));
    setupProgress();
    if (!setupReduced()) { void host.offsetWidth; host.classList.add(direction < 0 ? "enter-back" : "enter-forward"); }
    (host.querySelector("input, .setup-choice:not([disabled])") || host.querySelector(".setup-title"))?.focus({ preventScroll: true });
  };
  if (setupReduced() || !host.childElementCount) { draw(); return; }
  host.dataset.direction = direction < 0 ? "back" : "forward";
  host.classList.add("leaving");
  setTimeout(draw, 170);
}

function setupGo(step) {
  StudioSetup.trail.push(StudioSetup.step);
  setupShow(step, 1);
}

function setupBack() {
  const previous = StudioSetup.trail.pop();
  if (!previous) return;
  if (previous === "start") StudioSetup.mode = null;
  setupShow(previous, -1);
}

/* After the flow (open) or the starter (new): ask for the environment if there are any. */
function setupNext() {
  if (Studio.environments.length) setupGo("environment"); else setupFinish();
}

async function setupFinish() {
  if (StudioSetup.busy) return;
  StudioSetup.busy = true;
  try {
    if (StudioSetup.mode === "open") await studioOpen(StudioSetup.flow);
    else if (StudioSetup.template) await studioUseTemplate(StudioSetup.template, StudioSetup.name);
    else studioNew(StudioSetup.name);
  } finally { StudioSetup.busy = false; }
}

/* Begin again at the first question (opening the editor tab, or "Flows"). */
function setupStart() {
  Object.assign(StudioSetup, { step: "start", trail: [], mode: null, flow: "", template: "", name: "" });
  setupShow("start", 1);
}

function studioSetupInit() {
  document.getElementById("setup-back").onclick = setupBack;
  document.getElementById("studio-setup").addEventListener("keydown", (event) => {
    if (event.key === "Escape" && StudioSetup.step !== "start") { event.preventDefault(); setupBack(); }
  });
}
