/* MiniFlow Studio: version history (list, what changed, restore).
 *
 * Every save keeps the version it replaces in .history/<flow>/ next to the flow.
 * "Changes" compares a version with the current file; "Restore" makes it current again
 * (the replaced version is kept too, so a restore can itself be undone).
 */
"use strict";

function studioShort(value) {
  if (value === undefined || value === null) return "—";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > 60 ? `${text.slice(0, 57)}…` : text;
}

function studioDiffView(diff) {
  const box = document.createElement("div");
  box.className = "studio-diff";
  const line = (cls, text) => { const p = document.createElement("p"); p.className = cls; p.textContent = text; box.append(p); };
  if (diff.added.length) line("added", `+ added since then: ${diff.added.join(", ")}`);
  if (diff.removed.length) line("removed", `− removed since then: ${diff.removed.join(", ")}`);
  for (const [step, fields] of Object.entries(diff.changed)) {
    for (const c of fields) line("changed", `~ ${step} · ${c.field}: ${studioShort(c.old)} → ${studioShort(c.new)}`);
  }
  for (const c of diff.flow) line("changed", `~ ${c.field}: ${studioShort(c.old)} → ${studioShort(c.new)}`);
  if (!box.children.length) line("muted", "Same as the current file.");
  return box;
}

async function studioLoadVersions() {
  const host = document.getElementById("studio-history");
  if (!host) return;
  if (!Studio.name || !(await studioFileExists())) {
    host.replaceChildren(Object.assign(document.createElement("p"), { className: "muted", textContent: "Save the flow to start its history." }));
    return;
  }
  let versions = [];
  try { versions = await studioApi(`/api/flows/${encodeURIComponent(Studio.name)}/versions`); }
  catch { versions = []; }
  if (!versions.length) {
    host.replaceChildren(Object.assign(document.createElement("p"), {
      className: "muted", textContent: "No earlier versions yet. Each save keeps the version it replaces.",
    }));
    return;
  }
  host.replaceChildren(...versions.map((v) => {
    const row = document.createElement("div");
    row.className = "studio-version";
    const when = new Date(v.saved_at * 1000);
    const info = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = v.saved_at ? when.toLocaleString() : v.id;
    const meta = document.createElement("small");
    meta.textContent = `${v.user || "unknown"} · ${v.steps} step${v.steps === 1 ? "" : "s"}${v.message ? ` · “${v.message}”` : ""}`;
    info.append(title, meta);
    const changes = Object.assign(document.createElement("button"), { type: "button", className: "btn", textContent: "Changes" });
    const restore = Object.assign(document.createElement("button"), { type: "button", className: "btn", textContent: "Restore" });
    const detail = document.createElement("div");
    changes.onclick = async () => {
      if (detail.childElementCount) { detail.replaceChildren(); return; }
      try {
        const diff = await studioApi(`/api/flows/${encodeURIComponent(Studio.name)}/versions/${v.id}/diff`);
        detail.replaceChildren(studioDiffView(diff));
      } catch (error) { studioMessage(String(error), true); }
    };
    restore.onclick = () => studioRestore(v);
    row.append(info, changes, restore, detail);
    return row;
  }));
}

async function studioFileExists() {
  try { return (await studioApi("/api/flows")).includes(Studio.name); } catch { return false; }
}

async function studioRestore(version) {
  const when = version.saved_at ? new Date(version.saved_at * 1000).toLocaleString() : version.id;
  const warning = Studio.dirty ? "\n\nYour unsaved changes in the editor will be replaced." : "";
  if (!window.confirm(`Restore the version from ${when}? The current version is kept in history.${warning}`)) return;
  try {
    const result = await studioApi("/api/flows/restore", { name: Studio.name, version: version.id });
    if (!result.valid) { studioMessage((result.problems || []).join("\n"), true); return; }
    Studio.flow = await studioApi(`/api/flows/${encodeURIComponent(Studio.name)}`);
    studioCommit();  // undoable in the editor too
    Studio.dirty = false;
    studioRender();
    await studioLoadVersions();
    studioMessage(`Restored the version from ${when}.`);
  } catch (error) { studioMessage(String(error), true); }
}
