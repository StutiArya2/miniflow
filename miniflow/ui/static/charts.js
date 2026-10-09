"use strict";
/* The run view's two charts, drawn as plain SVG from the server's layout:
 *
 * - Graph: layered DAG. Long edges are routed through the gaps between boxes,
 *   the selected or hovered step lights up everything upstream and downstream
 *   of it, and the view pans (drag), zooms (Ctrl/pinch + wheel, buttons) and
 *   shows a minimap when it doesn't fit.
 * - Gantt: one row per worker or per step, one bar per attempt, hover cards,
 *   the critical path on demand, and a zoomable time axis.
 *
 * Loaded before app.js; everything here runs only after app.js has defined
 * its helpers (S, $, h, s, select, …).
 */

const NODE_W = 184, NODE_H = 52, COL_GAP = 72, ROW_GAP = 28, PAD = 24;
const G = { k: 1, x: 0, y: 0, width: 0, height: 0, fitted: false, focus: null, hover: null };

function truncate(text, max) {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function readPref(key, fallback) {
  try { return localStorage.getItem(key) || fallback; } catch { return fallback; }
}

function writePref(key, value) {
  try { localStorage.setItem(key, value); } catch { /* storage blocked */ }
}

// ------------------------------------------------------------------- graph
const colX = (col) => PAD + col * (NODE_W + COL_GAP);

function nodePos(name) {
  const p = S.layout[name];
  const offset = ((S._maxRows - S._rowsPerCol[p.col]) * (NODE_H + ROW_GAP)) / 2;
  return { x: colX(p.col), y: PAD + offset + p.row * (NODE_H + ROW_GAP) };
}

// An edge that skips columns would cut straight through the boxes in between.
// Route it through the nearest gap in each column it crosses instead, spreading
// edges that share a gap so they don't sit on top of each other.
function routeEdge(from, to, columns, gapUse) {
  const a = nodePos(from), b = nodePos(to);
  const ca = S.layout[from].col, cb = S.layout[to].col;
  const y1 = a.y + NODE_H / 2, y2 = b.y + NODE_H / 2;
  const points = [{ x: a.x + NODE_W, y: y1 }];
  for (let c = ca + 1; c < cb; c++) {
    const want = y1 + ((y2 - y1) * (c - ca)) / (cb - ca);
    const boxes = columns[c];
    let y = want;
    if (boxes.some((r) => want > r.y0 - 8 && want < r.y1 + 8)) {
      const gaps = [boxes[0].y0 - ROW_GAP / 2];
      for (let i = 1; i < boxes.length; i++) gaps.push((boxes[i - 1].y1 + boxes[i].y0) / 2);
      gaps.push(boxes[boxes.length - 1].y1 + ROW_GAP / 2);
      let best = 0;
      gaps.forEach((g, i) => { if (Math.abs(g - want) < Math.abs(gaps[best] - want)) best = i; });
      const key = `${c}:${best}`;
      const n = gapUse.get(key) || 0;
      gapUse.set(key, n + 1);
      y = gaps[best] + (n % 2 ? -1 : 1) * Math.ceil(n / 2) * 5;
    }
    points.push({ x: colX(c) - 12, y }, { x: colX(c) + NODE_W + 12, y });
  }
  points.push({ x: b.x - 2, y: y2 });
  let d = `M${points[0].x},${points[0].y}`;
  for (let i = 1; i < points.length; i++) {
    const p = points[i - 1], q = points[i];
    if (Math.abs(p.y - q.y) < 0.5) d += ` L${q.x},${q.y}`;
    else {
      const mx = (p.x + q.x) / 2;
      d += ` C${mx},${p.y} ${mx},${q.y} ${q.x},${q.y}`;
    }
  }
  return d;
}

function buildGraph() {
  const svg = $("graph");
  svg.replaceChildren();
  S.nodes.clear();
  S.edges = [];
  G.focus = null;
  G.hover = null;
  const names = Object.keys(S.graph.tasks);
  if (!names.length) return;

  const cols = Math.max(...names.map((n) => S.layout[n].col)) + 1;
  S._rowsPerCol = Array(cols).fill(0);
  for (const n of names) S._rowsPerCol[S.layout[n].col] += 1;
  S._maxRows = Math.max(...S._rowsPerCol);
  G.width = PAD * 2 + cols * NODE_W + (cols - 1) * COL_GAP;
  G.height = PAD * 2 + S._maxRows * NODE_H + (S._maxRows - 1) * ROW_GAP;

  const columns = Array.from({ length: cols }, () => []);
  for (const n of names) {
    const { y } = nodePos(n);
    columns[S.layout[n].col].push({ y0: y, y1: y + NODE_H });
  }
  for (const c of columns) c.sort((p, q) => p.y0 - q.y0);

  svg.append(s("defs", {},
    s("marker", { id: "arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "6", markerHeight: "6", orient: "auto-start-reverse" },
      s("path", { d: "M0,1 L9,5 L0,9 z", class: "arrow" })),
    s("marker", { id: "arrow-lit", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "6", markerHeight: "6", orient: "auto-start-reverse" },
      s("path", { d: "M0,1 L9,5 L0,9 z", class: "arrow lit" }))));

  const root = s("g", { id: "graph-root" });
  const edgeLayer = s("g", {});
  const nodeLayer = s("g", {});
  root.append(edgeLayer, nodeLayer);
  svg.append(root);

  const gapUse = new Map();
  for (const [name, info] of Object.entries(S.graph.tasks)) {
    for (const up of info.after) {
      if (!(up in S.graph.tasks)) continue;
      const el = s("path", { d: routeEdge(up, name, columns, gapUse), class: "edge", "marker-end": "url(#arrow)" });
      edgeLayer.append(el);
      S.edges.push({ from: up, to: name, el });
    }
  }

  for (const [name, info] of Object.entries(S.graph.tasks)) {
    const { x, y } = nodePos(name);
    const g = s("g", {
      class: "node", transform: `translate(${x},${y})`, tabindex: "0", role: "button",
      onclick: (e) => { e.stopPropagation(); select(name); },
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(name); } },
      onmouseenter: () => { G.hover = name; focusGraph(name); },
      onmouseleave: () => { G.hover = null; focusGraph(S.selected); },
    });
    if (info.kind === "cpu") {
      g.append(s("rect", { class: "back", x: 4, y: -4, width: NODE_W, height: NODE_H, rx: 8 }));
    }
    const rx = info.kind === "async" ? NODE_H / 2 : 8;
    const inset = info.kind === "async" ? 20 : 16;
    g.append(
      s("rect", { class: "box", width: NODE_W, height: NODE_H, rx }),
      s("circle", { class: "dot", cx: inset + 3, cy: 34, r: 3.5 }),
      s("text", { class: "name", x: inset, y: 21 }, truncate(name, 21)),
      s("text", { class: "sub", x: inset + 12, y: 38 }, ""),
      s("text", { class: "badge", x: NODE_W - inset + 4, y: 21, "text-anchor": "end" }, ""),
      s("circle", { class: "port", cx: 0, cy: NODE_H / 2, r: 3 }),
      s("circle", { class: "port", cx: NODE_W, cy: NODE_H / 2, r: 3 }),
      s("title", {}, info.description ? `${name}\n${info.description}` : name),
    );
    nodeLayer.append(g);
    S.nodes.set(name, g);
  }

  // Size the canvas to the graph (within limits), then fit it when it's visible.
  const wrap = $("pane-graph");
  wrap.style.height = `${Math.round(Math.min(Math.max(G.height + 48, 280), window.innerHeight * 0.72))}px`;
  G.fitted = false;
  if (!wrap.hidden) fitGraph(true);
  updateGraph();
}

function updateGraph() {
  if (!S.graph) return;
  for (const [name, g] of S.nodes) {
    const t = S.tasks.get(name);
    const info = S.graph.tasks[name];
    const state = t ? t.state : "PENDING";
    g.dataset.state = state;
    g.dataset.optional = info.optional ? "1" : "";
    let sub = STATE_WORDS[state] || state.toLowerCase();
    if (t && t.attempts > 1) sub += ` · try ${t.attempts}`;
    g.querySelector(".sub").textContent = sub;
    g.querySelector(".badge").textContent = t && t.from_cache ? "reused" : "";
    g.setAttribute("aria-label", `${name}, ${info.kind} task, ${state.toLowerCase()}`);
  }
  focusGraph(G.hover || S.selected);
  drawMinimap();
  renderStepList();
}

// Everything a step depends on, and everything that depends on it.
function lineage(name) {
  const up = new Set(), down = new Set();
  const walkUp = (n) => { for (const u of S.graph.tasks[n]?.after || []) if (u in S.graph.tasks && !up.has(u)) { up.add(u); walkUp(u); } };
  const walkDown = (n) => { for (const e of S.edges) if (e.from === n && !down.has(e.to)) { down.add(e.to); walkDown(e.to); } };
  walkUp(name);
  walkDown(name);
  return { up, down };
}

function focusGraph(name) {
  G.focus = name;
  const svg = $("graph");
  const on = !!(name && S.graph && name in S.graph.tasks);
  svg.classList.toggle("focus", on);
  const { up, down } = on ? lineage(name) : { up: new Set(), down: new Set() };
  const upLine = new Set([...up, name]), downLine = new Set([...down, name]);
  for (const [n, g] of S.nodes) {
    let cls = `node n-${g.dataset.state || "PENDING"}`;
    if (g.dataset.optional) cls += " optional";
    if (S.selected === n) cls += " selected";
    if (on && (upLine.has(n) || downLine.has(n))) cls += " lit";
    g.setAttribute("class", cls);
  }
  for (const e of S.edges) {
    const src = S.tasks.get(e.from), dst = S.tasks.get(e.to);
    let cls = "edge";
    if (src && src.state === "SUCCESS") cls += " done";
    if (dst && dst.state === "SKIPPED") cls += " skipped";
    const lit = on && ((upLine.has(e.from) && upLine.has(e.to)) || (downLine.has(e.from) && downLine.has(e.to)));
    if (lit) cls += " lit";
    e.el.setAttribute("class", cls);
    e.el.setAttribute("marker-end", lit ? "url(#arrow-lit)" : "url(#arrow)");
  }
}

// ---------------------------------------------------------- pan and zoom
function applyView() {
  const root = $("graph-root");
  if (root) root.setAttribute("transform", `translate(${G.x},${G.y}) scale(${G.k})`);
  $("graph-zoom").textContent = `${Math.round(G.k * 100)}%`;
  drawMinimap();
}

// readable=true (the first view of a run) keeps text legible on tall graphs:
// fit the width, but no smaller than 75%, and start at the top. The Fit
// button always shows the whole graph.
function fitGraph(readable) {
  const wrap = $("pane-graph");
  const W = wrap.clientWidth, H = wrap.clientHeight;
  if (!W || !G.width) return;
  const all = Math.min(1, (W - 32) / G.width, (H - 32) / G.height);
  G.k = readable === true ? Math.max(all, Math.min(0.75, (W - 32) / G.width)) : all;
  G.x = (W - G.width * G.k) / 2;
  G.y = G.k === all ? (H - G.height * G.k) / 2 : 8;
  G.fitted = true;
  applyView();
}

function zoomGraph(factor, cx, cy) {
  const wrap = $("pane-graph");
  if (cx === undefined) { cx = wrap.clientWidth / 2; cy = wrap.clientHeight / 2; }
  const k = Math.min(2.5, Math.max(0.2, G.k * factor));
  G.x = cx - ((cx - G.x) * k) / G.k;
  G.y = cy - ((cy - G.y) * k) / G.k;
  G.k = k;
  applyView();
}

function initGraphView() {
  const wrap = $("pane-graph");
  let drag = null;
  wrap.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || e.target.closest(".node, .chart-float, .minimap")) return;
    drag = { x: e.clientX, y: e.clientY, gx: G.x, gy: G.y, moved: false };
    wrap.setPointerCapture(e.pointerId);
  });
  wrap.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) { drag.moved = true; wrap.classList.add("panning"); }
    G.x = drag.gx + dx;
    G.y = drag.gy + dy;
    applyView();
  });
  const end = () => {
    if (drag && !drag.moved && S.selected) select(null);  // a click on empty canvas clears the selection
    drag = null;
    wrap.classList.remove("panning");
  };
  wrap.addEventListener("pointerup", end);
  wrap.addEventListener("pointercancel", end);
  wrap.addEventListener("wheel", (e) => {
    if (!e.ctrlKey && !e.metaKey) return;  // plain wheel scrolls the page
    e.preventDefault();
    const r = wrap.getBoundingClientRect();
    zoomGraph(Math.exp(-e.deltaY * 0.0025), e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  $("graph-zoom-in").addEventListener("click", () => zoomGraph(1.25));
  $("graph-zoom-out").addEventListener("click", () => zoomGraph(0.8));
  $("graph-fit").addEventListener("click", fitGraph);
  // The graph is often built while its page is still hidden (width 0): fit it
  // once it has a size, and keep the minimap in step with resizes.
  if ("ResizeObserver" in window) {
    new ResizeObserver(() => { if (!G.fitted) fitGraph(true); else drawMinimap(); }).observe(wrap);
  }

  const mini = $("minimap");
  const jump = (e) => {
    const r = mini.getBoundingClientRect();
    const m = G.mini;
    if (!m) return;
    const gx = (e.clientX - r.left - m.ox) / m.scale, gy = (e.clientY - r.top - m.oy) / m.scale;
    G.x = wrap.clientWidth / 2 - gx * G.k;
    G.y = wrap.clientHeight / 2 - gy * G.k;
    applyView();
  };
  let miniDrag = false;
  mini.addEventListener("pointerdown", (e) => { miniDrag = true; mini.setPointerCapture(e.pointerId); jump(e); });
  mini.addEventListener("pointermove", (e) => { if (miniDrag) jump(e); });
  mini.addEventListener("pointerup", () => { miniDrag = false; });
}

// A small overview with the visible area outlined; shown only when the graph overflows.
function drawMinimap() {
  const mini = $("minimap");
  const wrap = $("pane-graph");
  const W = wrap.clientWidth, H = wrap.clientHeight;
  const overflow = G.width * G.k > W + 1 || G.height * G.k > H + 1 || G.x > W || G.y > H || G.x + G.width * G.k < 0;
  mini.hidden = !S.graph || !W || !overflow;
  if (mini.hidden) return;
  const MW = 168, MH = 104;
  const scale = Math.min((MW - 12) / G.width, (MH - 12) / G.height);
  const ox = (MW - G.width * scale) / 2, oy = (MH - G.height * scale) / 2;
  G.mini = { scale, ox, oy };
  mini.setAttribute("viewBox", `0 0 ${MW} ${MH}`);
  const parts = [];
  for (const [name, g] of S.nodes) {
    const p = nodePos(name);
    parts.push(s("rect", {
      class: `mini-node m-${g.dataset.state || "PENDING"}`,
      x: ox + p.x * scale, y: oy + p.y * scale, width: NODE_W * scale, height: NODE_H * scale, rx: 1.5,
    }));
  }
  parts.push(s("rect", {
    class: "mini-view",
    x: ox + (-G.x / G.k) * scale, y: oy + (-G.y / G.k) * scale,
    width: (W / G.k) * scale, height: (H / G.k) * scale, rx: 2,
  }));
  mini.replaceChildren(...parts);
}

// ------------------------------------------------------------------- gantt
const T = {
  mode: readPref("miniflow-gantt-mode", "step"),   // "step" or "worker"
  critical: readPref("miniflow-gantt-critical", "") === "1",
  zoom: 1,
};

function laneName(a) {
  return a.worker || "timed out";
}

// Friendlier names for the worker lanes; the raw worker id stays in the tooltip.
function laneTitle(name) {
  let m;
  if ((m = name.match(/^thread-.*?(\d+)$/))) return `I/O thread ${Number(m[1]) + 1}`;
  if ((m = name.match(/^pid-(\d+)$/))) return `CPU process ${m[1]}`;
  if (name === "loop") return "Async loop";
  if (name === "running") return "Running now";
  if (name === "timed out") return "Timed out";
  return name;
}

function laneOrder(name) {
  if (name.startsWith("thread-")) return [0, name];
  if (name.startsWith("pid-")) return [1, name];
  if (name === "loop") return [2, name];
  if (name === "running") return [4, name];
  return [3, name];
}

function niceStep(span, target) {
  const raw = span / target;
  const pow = 10 ** Math.floor(Math.log10(raw));
  for (const m of [1, 2, 5, 10]) if (raw <= m * pow) return m * pow;
  return 10 * pow;
}

// The chain of steps that decided when the run finished: start from the step
// that finished last and keep following the upstream step that finished last.
function criticalPath() {
  const ran = (n) => { const t = S.tasks.get(n); return t && t.finished_at && t.started_at ? t : null; };
  let current = null;
  for (const n of Object.keys(S.graph.tasks)) {
    const t = ran(n);
    if (t && (!current || t.finished_at > ran(current).finished_at)) current = n;
  }
  const path = [];
  while (current) {
    path.unshift(current);
    let next = null;
    for (const u of S.graph.tasks[current].after) {
      const t = ran(u);
      if (t && (!next || t.finished_at > ran(next).finished_at)) next = u;
    }
    current = next;
  }
  return path;
}

function ganttBars(now) {
  const bars = S.attempts
    .filter((a) => a.started_at && a.finished_at)
    .map((a) => ({ task: a.task, attempt: a.attempt, state: a.state, start: a.started_at, end: a.finished_at, worker: laneName(a), error: a.error }));
  if (!S.run.finished_at) {
    for (const t of S.tasks.values()) {
      if (t.state === "RUNNING") {
        bars.push({ task: t.task, attempt: t.attempts + 1, state: "RUNNING", start: t.updated_at, end: now, worker: "running" });
      }
    }
  }
  return bars;
}

function renderTimeline() {
  if ($("pane-timeline").hidden) return;
  const svg = $("timeline");
  svg.replaceChildren();
  hideGanttTip();
  for (const b of document.querySelectorAll("#gantt-mode [data-mode]")) b.setAttribute("aria-pressed", String(b.dataset.mode === T.mode));
  $("gantt-critical").setAttribute("aria-pressed", String(T.critical));
  if (!S.run) return;
  const now = nowServer();
  const bars = ganttBars(now);
  const host = svg.parentElement;
  host.querySelector(".timeline-empty")?.remove();
  if (!bars.length) {
    svg.setAttribute("width", 0);
    svg.setAttribute("height", 0);
    host.append(h("div", { class: "timeline-empty" }, S.run.finished_at ? "No step ran in this run (everything was reused)." : "Waiting for the first attempt to start…"));
    return;
  }

  // Parallel speedup: time spent inside steps divided by the real time the run took.
  const wall = (S.run.finished_at || now) - S.run.started_at;
  const busy = bars.reduce((sum, b) => sum + (b.end - b.start), 0);
  const crit = T.critical && S.graph ? criticalPath() : [];
  const critSet = new Set(crit);
  if (T.critical && crit.length) {
    const first = S.tasks.get(crit[0]), last = S.tasks.get(crit[crit.length - 1]);
    $("timeline-hint").textContent = `Critical path: ${crit.length} step${crit.length === 1 ? "" : "s"}, ${fmtDur(last.finished_at - first.started_at)} of the ${fmtDur(wall)} run`;
  } else {
    $("timeline-hint").textContent = wall > 0 && busy > 0
      ? `Parallel speedup ×${(busy / wall).toFixed(1)}: ${fmtDur(busy)} of step work in ${fmtDur(wall)}`
      : "What ran when";
  }

  // Rows: one per step (in graph order, steps that never ran included) or one per worker.
  let rows;
  if (T.mode === "step" && S.graph) {
    rows = Object.keys(S.graph.tasks)
      .sort((a, b) => (S.layout[a].col - S.layout[b].col) || (S.layout[a].row - S.layout[b].row))
      .map((n) => ({ key: n, label: humanize(n), title: n, state: (S.tasks.get(n) || { state: "PENDING" }).state }));
  } else {
    rows = [...new Set(bars.map((b) => b.worker))]
      .sort((a, b) => { const [ka, na] = laneOrder(a), [kb, nb] = laneOrder(b); return ka - kb || na.localeCompare(nb, undefined, { numeric: true }); })
      .map((w) => ({ key: w, label: laneTitle(w), title: w }));
  }
  const rowOf = (b) => (T.mode === "step" && S.graph ? b.task : b.worker);
  const index = new Map(rows.map((r, i) => [r.key, i]));

  const LABEL_W = T.mode === "step" ? 190 : 150, ROW_H = T.mode === "step" ? 30 : 34, BAR_H = 16, AXIS_H = 28;
  const t0 = S.run.started_at;
  const t1 = Math.max(S.run.finished_at || now, ...bars.map((b) => b.end));
  const span = Math.max(t1 - t0, 0.001);
  const avail = Math.max(host.clientWidth - 2, 600);
  const plotW = Math.max(avail - LABEL_W - 16, 300) * T.zoom;
  const width = LABEL_W + plotW + 16;
  const height = AXIS_H + rows.length * ROW_H + 6;
  const x = (t) => LABEL_W + ((t - t0) / span) * plotW;
  const rowY = (key) => AXIS_H + index.get(key) * ROW_H;
  $("gantt-zoom").textContent = `${Math.round(T.zoom * 100)}%`;

  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  svg.classList.toggle("critical", crit.length > 0);
  svg.append(s("defs", {},
    s("pattern", { id: "stripes", width: 8, height: 8, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" },
      s("rect", { width: 8, height: 8, fill: "var(--running-line)", opacity: 0.55 }),
      s("rect", { width: 4, height: 8, fill: "var(--running-line)" }))));

  rows.forEach((row, i) => {
    const y = AXIS_H + i * ROW_H;
    if (i > 0) svg.append(s("line", { class: "lane-sep", x1: 0, x2: width, y1: y, y2: y }));
    const selected = T.mode === "step" && S.selected === row.key;
    if (selected) svg.append(s("rect", { class: "row-selected", x: 0, y, width, height: ROW_H }));
    const label = s("g", { class: `lane${T.mode === "step" ? " clickable" : ""}`, onclick: T.mode === "step" ? () => select(row.key) : null });
    if (row.state) label.append(s("circle", { class: `lane-dot m-${row.state}`, cx: 4, cy: y + ROW_H / 2, r: 3.5 }));
    label.append(s("text", { class: "lane-label", x: row.state ? 16 : 0, y: y + ROW_H / 2 + 4 }, truncate(row.label, 24), s("title", {}, row.title)));
    svg.append(label);
    if (row.state === "SKIPPED") {
      svg.append(s("text", { class: "row-note", x: LABEL_W, y: y + ROW_H / 2 + 4 }, "skipped"));
    }
  });

  const step = niceStep(span, Math.max(2, Math.floor(plotW / 110)));
  for (let t = 0; t <= span + 1e-9; t += step) {
    const xx = x(t0 + t);
    svg.append(
      s("line", { class: "tick", x1: xx, x2: xx, y1: AXIS_H - 6, y2: height }),
      s("text", { class: "tick-label", x: xx, y: 14, "text-anchor": t === 0 ? "start" : "middle" }, fmtDur(t)));
  }

  // Backoff connectors between consecutive attempts of the same step.
  const byTask = new Map();
  for (const b of bars) {
    if (!byTask.has(b.task)) byTask.set(b.task, []);
    byTask.get(b.task).push(b);
  }
  for (const list of byTask.values()) {
    list.sort((a, b) => a.attempt - b.attempt);
    for (let i = 1; i < list.length; i++) {
      const a = list[i - 1], b = list[i];
      const ya = rowY(rowOf(a)) + ROW_H / 2, yb = rowY(rowOf(b)) + ROW_H / 2;
      const xa = x(a.end), xb = x(b.start);
      svg.append(s("path", { class: "backoff", d: `M${xa},${ya} C${(xa + xb) / 2},${ya} ${(xa + xb) / 2},${yb} ${xb},${yb}` }));
    }
  }

  // Critical path links: end of one step to the start of the next.
  for (let i = 1; i < crit.length; i++) {
    const a = (byTask.get(crit[i - 1]) || []).at(-1), b = (byTask.get(crit[i]) || [])[0];
    if (!a || !b) continue;
    const xa = x(a.end), ya = rowY(rowOf(a)) + ROW_H / 2, xb = x(b.start), yb = rowY(rowOf(b)) + ROW_H / 2;
    svg.append(s("path", { class: "crit-link", d: `M${xa},${ya} C${xa + 12},${ya} ${xb - 12},${yb} ${xb},${yb}` }));
  }

  for (const b of bars) {
    const bx = x(b.start), bw = Math.max(x(b.end) - bx, 3);
    const y = rowY(rowOf(b)) + (ROW_H - BAR_H) / 2;
    let cls = `bar ${b.state}`;
    if (S.selected === b.task) cls += " selected";
    if (critSet.has(b.task)) cls += " crit";
    svg.append(s("rect", {
      class: cls, x: bx, y, width: bw, height: BAR_H, rx: Math.min(4, bw / 2),
      onclick: () => select(b.task),
      onmouseenter: (e) => showGanttTip(b, e),
      onmousemove: (e) => moveGanttTip(e),
      onmouseleave: hideGanttTip,
    }));
    const label = T.mode === "step" ? (b.attempt > 1 ? `try ${b.attempt}` : fmtDur(b.end - b.start)) : (b.attempt > 1 ? `${b.task} #${b.attempt}` : b.task);
    if (bw > label.length * 6.4 + 12) {
      svg.append(s("text", { class: "bar-label", x: bx + 6, y: y + BAR_H / 2 + 3.5 }, label));
    } else if (T.mode === "step" && b.attempt === byTask.get(b.task).length) {
      svg.append(s("text", { class: "bar-after", x: bx + bw + 6, y: y + BAR_H / 2 + 3.5 }, label));
    }
  }
  if (!S.run.finished_at) {
    const xn = x(now);
    svg.append(s("line", { class: "now", x1: xn, x2: xn, y1: AXIS_H - 6, y2: height }));
  }
}

function showGanttTip(b, e) {
  const tip = $("gantt-tip");
  const t0 = S.run.started_at;
  tip.replaceChildren(
    h("strong", {}, humanize(b.task)),
    h("div", { class: "tip-line" }, pill(b.state), `try ${b.attempt} · ${fmtDur(b.end - b.start)}${b.state === "RUNNING" ? " so far" : ""}`),
    h("div", { class: "tip-line muted" }, `${laneTitle(b.worker)} · started +${fmtDur(b.start - t0)}`),
    b.error ? h("div", { class: "tip-error" }, firstLine(b.error)) : null,
  );
  tip.hidden = false;
  moveGanttTip(e);
}

function moveGanttTip(e) {
  const tip = $("gantt-tip");
  if (tip.hidden) return;
  const host = $("pane-timeline").getBoundingClientRect();
  let left = e.clientX - host.left + 14, top = e.clientY - host.top + 14;
  if (left + tip.offsetWidth > host.width - 8) left = e.clientX - host.left - tip.offsetWidth - 14;
  tip.style.left = `${Math.max(4, left)}px`;
  tip.style.top = `${top}px`;
}

function hideGanttTip() {
  $("gantt-tip").hidden = true;
}

function initGantt() {
  for (const b of document.querySelectorAll("#gantt-mode [data-mode]")) {
    b.addEventListener("click", () => { T.mode = b.dataset.mode; writePref("miniflow-gantt-mode", T.mode); renderTimeline(); });
  }
  $("gantt-critical").addEventListener("click", () => {
    T.critical = !T.critical;
    writePref("miniflow-gantt-critical", T.critical ? "1" : "");
    renderTimeline();
  });
  const zoomTo = (z) => { T.zoom = Math.min(16, Math.max(1, z)); renderTimeline(); };
  $("gantt-zoom-in").addEventListener("click", () => zoomTo(T.zoom * 1.5));
  $("gantt-zoom-out").addEventListener("click", () => zoomTo(T.zoom / 1.5));
  $("gantt-fit").addEventListener("click", () => zoomTo(1));
  $("timeline-scroll").addEventListener("wheel", (e) => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    zoomTo(T.zoom * Math.exp(-e.deltaY * 0.003));
  }, { passive: false });
}
