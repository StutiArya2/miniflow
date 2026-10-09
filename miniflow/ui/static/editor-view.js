/* MiniFlow Studio: zoom, pan, fit to screen, box selection and "Tidy up".
 *
 * The canvas is moved with a CSS transform, so zooming never changes the step
 * positions saved in the flow file. Wheel = zoom at the pointer, drag empty space =
 * pan, Shift+drag empty space = select a box of steps.
 */
"use strict";

const StudioView = { zoom: 1, x: 0, y: 0, min: 0.25, max: 2 };
const STUDIO_NODE_W = 180, STUDIO_NODE_H = 74;

function studioApplyView() {
  const canvas = document.getElementById("studio-canvas");
  canvas.style.transform = `translate(${StudioView.x}px, ${StudioView.y}px) scale(${StudioView.zoom})`;
  const label = document.getElementById("studio-zoom-level");
  if (label) label.textContent = `${Math.round(StudioView.zoom * 100)}%`;
}

/* Pointer position in canvas coordinates. */
function studioCanvasPoint(event) {
  const rect = document.querySelector(".studio-canvas-scroll").getBoundingClientRect();
  return {
    x: (event.clientX - rect.left - StudioView.x) / StudioView.zoom,
    y: (event.clientY - rect.top - StudioView.y) / StudioView.zoom,
  };
}

function studioZoomAt(factor, cx, cy) {
  const zoom = Math.min(StudioView.max, Math.max(StudioView.min, StudioView.zoom * factor));
  // keep the canvas point under (cx, cy) where it is
  StudioView.x = cx - ((cx - StudioView.x) * zoom) / StudioView.zoom;
  StudioView.y = cy - ((cy - StudioView.y) * zoom) / StudioView.zoom;
  StudioView.zoom = zoom;
  studioApplyView();
}

function studioZoomButton(factor) {
  const box = document.querySelector(".studio-canvas-scroll").getBoundingClientRect();
  studioZoomAt(factor, box.width / 2, box.height / 2);
}

function studioFit() {
  const names = Object.keys(Studio.flow.steps || {});
  const box = document.querySelector(".studio-canvas-scroll").getBoundingClientRect();
  if (!names.length || !box.width) { StudioView.zoom = 1; StudioView.x = 0; StudioView.y = 0; studioApplyView(); return; }
  const pos = names.map((name, i) => studioPosition(name, i));
  const left = Math.min(...pos.map((p) => p.x)), top = Math.min(...pos.map((p) => p.y));
  const right = Math.max(...pos.map((p) => p.x + STUDIO_NODE_W)), bottom = Math.max(...pos.map((p) => p.y + STUDIO_NODE_H));
  const pad = 40;
  const zoom = Math.min(1.25, (box.width - 2 * pad) / (right - left), (box.height - 2 * pad) / (bottom - top));
  StudioView.zoom = Math.min(StudioView.max, Math.max(StudioView.min, zoom));
  StudioView.x = (box.width - (right - left) * StudioView.zoom) / 2 - left * StudioView.zoom;
  StudioView.y = (box.height - (bottom - top) * StudioView.zoom) / 2 - top * StudioView.zoom;
  studioApplyView();
}

/* The steps a step depends on: its "after" list plus every {{ step }} it uses. */
function studioDeps(name) {
  const step = Studio.flow.steps[name] || {};
  const used = [...JSON.stringify(step.settings || {}).matchAll(/\{\{\s*([A-Za-z_]\w*)/g)].map((m) => m[1]);
  return [...new Set([...(step.after || []), ...used])].filter((dep) => dep !== name && dep in Studio.flow.steps);
}

async function studioTidy() {
  const names = Object.keys(Studio.flow.steps || {});
  if (!names.length) return;
  try {
    const layout = await studioApi("/api/layout", {
      graph: { tasks: Object.fromEntries(names.map((name) => [name, { after: studioDeps(name) }])) },
    });
    for (const name of names) {
      const cell = layout[name];
      if (cell) Studio.flow.steps[name].position = { x: 60 + cell.col * 250, y: 60 + cell.row * 120 };
    }
    studioCommit();
    studioRender();
    studioFit();
    studioMessage("Arranged the steps by dependency. Undo with Ctrl+Z.");
  } catch (error) { studioMessage(String(error), true); }
}

/* ---------------------------------------------------------- drag and drop */
const STUDIO_DRAG = "application/x-miniflow-block";

/* Make a palette button draggable onto the canvas (clicking it still adds it). */
function studioDraggable(button, kind, name) {
  button.draggable = true;
  button.title = "Drag onto the canvas, or click to add";
  button.ondragstart = (event) => {
    event.dataTransfer.setData(STUDIO_DRAG, JSON.stringify({ kind, name }));
    event.dataTransfer.setData("text/plain", name);
    event.dataTransfer.effectAllowed = "copy";
  };
}

function studioDropInit(host) {
  host.addEventListener("dragover", (event) => {
    if (!event.dataTransfer?.types.includes(STUDIO_DRAG)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    host.classList.add("dropping");
  });
  host.addEventListener("dragleave", (event) => {
    if (!host.contains(event.relatedTarget)) host.classList.remove("dropping");
  });
  host.addEventListener("drop", (event) => {
    host.classList.remove("dropping");
    let item;
    try { item = JSON.parse(event.dataTransfer.getData(STUDIO_DRAG)); } catch { return; }
    event.preventDefault();
    const p = studioCanvasPoint(event);
    const at = { x: p.x - STUDIO_NODE_W / 2, y: p.y - STUDIO_NODE_H / 2 };  // centre the step on the pointer
    if (item.kind === "flow") studioAddFlow(item.name, at);
    else studioAdd(item.name, at);
    studioMessage(`${Studio.selected} added where you dropped it.`);
  });
}

/* --------------------------------------------------------------- pointer */
function studioBackground(target) {
  return target.id === "studio-canvas" || target.id === "studio-nodes" || target.closest?.("#studio-wires")
    || target.classList?.contains("studio-canvas-scroll");
}

function studioViewInit() {
  const host = document.querySelector(".studio-canvas-scroll");
  studioDropInit(host);
  host.addEventListener("wheel", (event) => {
    if (!Studio.visible) return;
    event.preventDefault();
    const rect = host.getBoundingClientRect();
    studioZoomAt(Math.exp(-event.deltaY * 0.0015), event.clientX - rect.left, event.clientY - rect.top);
  }, { passive: false });

  host.addEventListener("pointerdown", (event) => {
    if (!studioBackground(event.target) || event.button !== 0) return;
    const start = { x: event.clientX, y: event.clientY, vx: StudioView.x, vy: StudioView.y };
    const box = event.shiftKey ? document.createElement("div") : null;
    const origin = studioCanvasPoint(event);
    if (box) { box.className = "studio-select-box"; document.getElementById("studio-canvas").append(box); }
    host.setPointerCapture(event.pointerId);
    host.onpointermove = (move) => {
      if (!box) {
        StudioView.x = start.vx + move.clientX - start.x;
        StudioView.y = start.vy + move.clientY - start.y;
        studioApplyView();
        return;
      }
      const p = studioCanvasPoint(move);
      Object.assign(box.style, {
        left: `${Math.min(p.x, origin.x)}px`, top: `${Math.min(p.y, origin.y)}px`,
        width: `${Math.abs(p.x - origin.x)}px`, height: `${Math.abs(p.y - origin.y)}px`,
      });
    };
    host.onpointerup = (up) => {
      host.onpointermove = null;
      host.onpointerup = null;
      if (!box) {
        if (Math.abs(up.clientX - start.x) + Math.abs(up.clientY - start.y) < 3) {
          Studio.selected = null; Studio.multi = new Set(); studioRender();  // a click on empty space
        }
        return;
      }
      const p = studioCanvasPoint(up);
      const [x0, x1] = [Math.min(p.x, origin.x), Math.max(p.x, origin.x)];
      const [y0, y1] = [Math.min(p.y, origin.y), Math.max(p.y, origin.y)];
      box.remove();
      const names = Object.keys(Studio.flow.steps);
      Studio.multi = new Set(names.filter((name, i) => {
        const pos = studioPosition(name, i);
        return pos.x < x1 && pos.x + STUDIO_NODE_W > x0 && pos.y < y1 && pos.y + STUDIO_NODE_H > y0;
      }));
      Studio.selected = [...Studio.multi][0] || null;
      studioRender();
      if (Studio.multi.size) studioMessage(`${Studio.multi.size} steps selected: Ctrl+C to copy, Delete to remove.`);
    };
  });
  studioApplyView();
}
