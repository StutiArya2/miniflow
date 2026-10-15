/* MiniFlow: show the files a run wrote, right in the dashboard.
 *
 * CSV and Excel files appear as a spreadsheet (column letters, row numbers, sheet tabs),
 * Word files as a page, Markdown as formatted text, images and PDFs as themselves.
 * "Open in app" opens the file in the computer's default app (Excel, Word, Preview…).
 * Files come from GET /api/runs/<id>/outputs; only files a step reported can be shown.
 */
"use strict";

const Outputs = { lists: new Map(), keys: new Map(), loading: new Map(), previews: new Map(), chosen: new Map() };

function outEl(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node[key] = value;
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const OUTPUT_KIND_LABEL = {
  table: "Spreadsheet", document: "Document", markdown: "Markdown", text: "Text",
  html: "Web page", image: "Image", pdf: "PDF", other: "File",
};

function outputsSize(bytes) {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

const outputsUrl = (runId, task, what) =>
  `/api/runs/${encodeURIComponent(runId)}/outputs/${encodeURIComponent(task)}/${what}`;

/* The run's output files. `key` changes when more steps finish, which refetches. */
async function outputsFor(runId, key = "") {
  if (Outputs.keys.get(runId) === key && Outputs.lists.has(runId)) return Outputs.lists.get(runId);
  const pending = Outputs.loading.get(runId);
  if (pending?.key === key) return pending.promise;
  const promise = (async () => {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/outputs`, { cache: "no-store" });
    const list = response.ok ? await response.json() : [];
    Outputs.lists.set(runId, list);
    Outputs.keys.set(runId, key);
    for (const item of list) Outputs.previews.delete(`${runId}/${item.task}`); // files may be rewritten
    return list;
  })().finally(() => Outputs.loading.delete(runId));
  Outputs.loading.set(runId, { key, promise });
  return promise;
}

async function outputsPreview(runId, task) {
  const cacheKey = `${runId}/${task}`;
  if (!Outputs.previews.has(cacheKey)) {
    Outputs.previews.set(cacheKey, (async () => {
      const response = await fetch(outputsUrl(runId, task, "preview"), { cache: "no-store" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      return data;
    })());
  }
  try { return await Outputs.previews.get(cacheKey); }
  catch (error) { Outputs.previews.delete(cacheKey); throw error; }
}

/* ---------------------------------------------------------------- renderers */
function outputsColumn(n) {
  let out = "";
  for (let i = n + 1; i > 0; i = Math.floor((i - 1) / 26)) out = String.fromCharCode(65 + ((i - 1) % 26)) + out;
  return out;
}

const OUTPUT_NUMBER = /^-?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?%?$/;

function outputsSheet(sheet) {
  const width = Math.max(sheet.columns, 1);
  const head = outEl("tr", {}, outEl("th", { class: "xl-corner", scope: "col" }),
    Array.from({ length: width }, (_, i) => outEl("th", { scope: "col" }, outputsColumn(i))));
  const body = sheet.rows.map((row, r) => outEl("tr", {},
    outEl("th", { scope: "row" }, String(r + 1)),
    Array.from({ length: width }, (_, c) => {
      const value = row[c];
      const text = value === null || value === undefined ? "" : typeof value === "object" ? JSON.stringify(value) : String(value);
      const number = typeof value === "number" || (r > 0 && OUTPUT_NUMBER.test(text.trim()));
      const link = r > 0 && /^https?:\/\/\S+$/i.test(text.trim());
      const classes = [number ? "num" : "", link ? "link" : "", r === 0 ? "first" : ""].join(" ").trim() || null;
      // web addresses open in a new tab; nothing else in a cell is ever treated as HTML
      if (!link) return outEl("td", { class: classes }, text);
      // show the site's name to keep rows short; the full address is the tooltip and the target
      let site = text.trim();
      try { site = new URL(site).hostname.replace(/^www\./, ""); } catch { /* keep the text */ }
      return outEl("td", { class: classes },
        outEl("a", { href: text.trim(), target: "_blank", rel: "noopener noreferrer", title: text.trim() }, `${site} ↗`));
    })));
  return outEl("div", { class: "xl-scroll" }, outEl("table", { class: "xl-grid" }, outEl("thead", {}, head), outEl("tbody", {}, body)));
}

function outputsTable(data) {
  const box = outEl("div", { class: "xl" });
  const sheets = data.sheets.length ? data.sheets : [{ name: "Sheet1", rows: [], total: 0, columns: 0 }];
  const grid = outEl("div", { class: "xl-body" });
  const note = outEl("p", { class: "xl-note" });
  const tabs = outEl("div", { class: "xl-tabs", role: "tablist", "aria-label": "Sheets" });
  const show = (index) => {
    const sheet = sheets[index];
    grid.replaceChildren(sheet.rows.length ? outputsSheet(sheet) : outEl("p", { class: "muted xl-empty" }, "This sheet is empty."));
    note.textContent = sheet.total > sheet.rows.length
      ? `Showing the first ${sheet.rows.length.toLocaleString()} of ${sheet.total.toLocaleString()} rows. Open the file to see them all.`
      : `${sheet.total.toLocaleString()} row${sheet.total === 1 ? "" : "s"}`;
    [...tabs.children].forEach((tab, i) => tab.setAttribute("aria-selected", String(i === index)));
  };
  sheets.forEach((sheet, i) => tabs.append(outEl("button", { type: "button", role: "tab", onclick: () => show(i) }, sheet.name || `Sheet${i + 1}`)));
  box.append(grid, outEl("div", { class: "xl-foot" }, tabs, note));
  show(0);
  return box;
}

function outputsRuns(runs) {
  return runs.map((run) => {
    let node = document.createTextNode(run.text);
    if (run.underline) node = outEl("u", {}, node);
    if (run.italic) node = outEl("em", {}, node);
    if (run.bold) node = outEl("strong", {}, node);
    return node;
  });
}

function outputsDocument(data) {
  const page = outEl("article", { class: "doc-page" });
  let list = null;
  for (const block of data.blocks) {
    if (block.type === "paragraph" && block.list) {
      if (!list) { list = outEl("ul"); page.append(list); }
      list.append(outEl("li", {}, outputsRuns(block.runs)));
      continue;
    }
    list = null;
    if (block.type === "heading") page.append(outEl(`h${Math.min(Math.max(block.level, 1), 6)}`, {}, outputsRuns(block.runs)));
    else if (block.type === "table") {
      page.append(outEl("table", { class: "doc-table" }, outEl("tbody", {},
        block.rows.map((row, r) => outEl("tr", {}, row.map((cell) => outEl(r === 0 ? "th" : "td", {}, cell)))))));
    } else page.append(block.runs.length ? outEl("p", {}, outputsRuns(block.runs)) : outEl("p", { class: "doc-blank" }));
  }
  if (!data.blocks.length) page.append(outEl("p", { class: "muted" }, "This document is empty."));
  if (data.truncated) page.append(outEl("p", { class: "muted" }, "The document continues; open it to read the rest."));
  return outEl("div", { class: "doc-desk" }, page);
}

function outputsBody(runId, item, data) {
  const file = outputsUrl(runId, item.task, "file");
  const more = data.truncated ? outEl("p", { class: "muted" }, "Only the start of this file is shown; open it to see the rest.") : null;
  switch (data.kind) {
    case "table": return outputsTable(data);
    case "document": return outputsDocument(data);
    case "markdown": {
      const page = outEl("article", { class: "doc-page claude-prose" });
      page.append(typeof claudeMarkdown === "function" ? claudeMarkdown(data.text) : outEl("pre", {}, data.text));
      return outEl("div", { class: "doc-desk" }, page, more);
    }
    case "text": return outEl("div", { class: "out-text" }, outEl("pre", {}, data.text), more);
    case "html": {
      // no scripts, no access to the dashboard: the page is shown, not run
      const frame = outEl("iframe", { class: "out-frame", sandbox: "", title: item.name, referrerpolicy: "no-referrer" });
      frame.srcdoc = data.text;
      return frame;
    }
    case "image": return outEl("div", { class: "out-image" }, outEl("img", { src: file, alt: item.name }));
    case "pdf": return outEl("iframe", { class: "out-frame", src: file, title: item.name });
    default: return outEl("p", { class: "muted out-other" }, "This kind of file can't be shown here. Open it in its app or download it.");
  }
}

/* One file: a header with its name and actions, then its content. */
function outputsView(runId, item, { onExpand = null } = {}) {
  const status = outEl("span", { class: "out-status", role: "status" });
  const open = outEl("button", { type: "button", class: "btn", title: "Open in the app your computer uses for this file (Excel, Word, Preview…)" }, "Open in app");
  open.onclick = async () => {
    open.disabled = true; status.textContent = "";
    try {
      const response = await fetch(outputsUrl(runId, item.task, "open"), {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      });
      const data = await response.json();
      status.textContent = response.ok ? `Opened ${data.opened}.` : data.error;
    } catch (error) { status.textContent = String(error.message || error); }
    finally { open.disabled = false; }
  };
  const download = outEl("a", { class: "btn", href: `${outputsUrl(runId, item.task, "file")}?download=1`, download: item.name }, "Download");
  const expand = onExpand ? outEl("button", { type: "button", class: "btn", onclick: onExpand }, "Full screen") : null;
  const head = outEl("div", { class: "out-head" },
    outEl("div", { class: "out-title" },
      outEl("strong", {}, item.name),
      outEl("small", { title: item.path }, [OUTPUT_KIND_LABEL[item.kind] || "File", outputsSize(item.size), `from ${item.task}`].filter(Boolean).join(" · "))),
    outEl("div", { class: "out-actions" }, status, expand, item.exists ? open : null, item.exists ? download : null));
  const body = outEl("div", { class: "out-body" }, outEl("p", { class: "muted out-loading" }, "Loading…"));
  if (!item.exists) {
    body.replaceChildren(outEl("p", { class: "muted out-other" }, `This file is no longer at ${item.path}.`));
  } else {
    outputsPreview(runId, item.task)
      .then((data) => body.replaceChildren(outputsBody(runId, item, data)))
      .catch((error) => body.replaceChildren(outEl("p", { class: "error out-other" }, error.message)));
  }
  return outEl("section", { class: `out-view out-${item.kind}` }, head, body);
}

/* Several files: a tab per file, one shown at a time. */
function outputsPanel(runId, list, { expandable = true, start = null } = {}) {
  const box = outEl("div", { class: "out-panel" });
  const tabs = outEl("div", { class: "out-files", role: "tablist", "aria-label": "Files this run wrote" });
  const holder = outEl("div");
  const show = (task) => {
    const item = list.find((o) => o.task === task) || list[0];
    Outputs.chosen.set(runId, item.task);
    [...tabs.children].forEach((tab) => tab.setAttribute("aria-selected", String(tab.dataset.task === item.task)));
    holder.replaceChildren(outputsView(runId, item, { onExpand: expandable ? () => outputsOpen(runId, item.task) : null }));
  };
  for (const item of list) {
    const tab = outEl("button", { type: "button", role: "tab", class: `out-file kind-${item.kind}`, title: item.path, onclick: () => show(item.task) },
      outEl("span", { class: "out-file-icon", "aria-hidden": "true" }, (item.name.split(".").pop() || "").slice(0, 4).toUpperCase()), item.name);
    tab.dataset.task = item.task;
    tabs.append(tab);
  }
  if (list.length > 1) box.append(tabs);
  box.append(holder);
  show(start || Outputs.chosen.get(runId) || list[0].task);
  return box;
}

/* Full-screen viewer. */
async function outputsOpen(runId, task) {
  let dialog = document.getElementById("output-viewer");
  if (!dialog) {
    dialog = outEl("dialog", { id: "output-viewer", class: "output-viewer", "aria-label": "Output file" });
    dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
    document.body.append(dialog);
  }
  const close = outEl("button", { type: "button", class: "btn", onclick: () => dialog.close() }, "Close");
  const content = outEl("div", { class: "output-viewer-body" }, outEl("p", { class: "muted" }, "Loading…"));
  dialog.replaceChildren(outEl("header", {}, outEl("h2", {}, "Output"), close), content);
  if (!dialog.open) dialog.showModal();
  try {
    const list = await outputsFor(runId, Outputs.keys.get(runId) ?? "");
    content.replaceChildren(list.length ? outputsPanel(runId, list, { expandable: false, start: task })
      : outEl("p", { class: "muted" }, "This run didn't write any files."));
  } catch (error) { content.replaceChildren(outEl("p", { class: "error" }, String(error.message || error))); }
}

/* A button that opens one step's file. */
function outputsButton(runId, item, label = null) {
  return outEl("button", { type: "button", class: "btn out-open", title: item.path, onclick: () => outputsOpen(runId, item.task) },
    label || `View ${item.name}`);
}
