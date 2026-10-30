"use strict";

/* Pending blocks: block drafts an agent wrote, waiting for a person to approve them.
 * Nothing in a draft runs until it is approved here (or with `miniflow approve-block`). */
const StudioDrafts = { items: [] };

async function studioLoadDrafts() {
  const button = document.getElementById("studio-drafts");
  let drafts;
  try { drafts = await studioApi("/api/blocks/drafts"); }
  catch { button.hidden = true; return; } // a read-only dashboard has no drafts
  const changed = JSON.stringify(drafts) !== JSON.stringify(StudioDrafts.items);
  StudioDrafts.items = drafts;
  button.hidden = drafts.length === 0;
  button.textContent = `Pending blocks (${drafts.length})`;
  const dialog = document.getElementById("studio-drafts-dialog");
  if (dialog.open) {
    if (!drafts.length) dialog.close(); else if (changed) studioRenderDrafts();
  }
}

function studioDraftError(text) {
  const error = document.getElementById("studio-drafts-error");
  error.textContent = text || "";
  error.hidden = !text;
}

function studioRenderDrafts() {
  document.getElementById("studio-drafts-list").replaceChildren(...StudioDrafts.items.map((draft) => {
    const card = document.createElement("article");
    card.className = "studio-draft";
    const title = document.createElement("h3");
    title.textContent = draft.name;
    const about = document.createElement("p");
    about.className = "hint";
    about.textContent = `Drafted ${new Date(draft.drafted_at * 1000).toLocaleString()} · `
      + (draft.imports.length ? `imports ${draft.imports.join(", ")}` : "no imports");
    const checks = document.createElement("ul");
    checks.className = "studio-draft-checks";
    for (const check of draft.checks) {
      const item = document.createElement("li");
      item.className = check.ok ? "ok" : "bad";
      item.textContent = `${check.ok ? "✓" : "✗"} ${check.check}${check.detail ? `: ${check.detail}` : ""}`;
      checks.append(item);
    }
    const code = document.createElement("pre");
    code.className = "studio-draft-source";
    code.textContent = draft.source;
    const approve = document.createElement("button");
    approve.type = "button";
    approve.className = "btn primary";
    approve.textContent = "Approve";
    approve.disabled = !draft.ok;
    if (!draft.ok) approve.title = "Discard it, or ask the agent to fix the failed checks";
    approve.onclick = () => studioDraftAction("approve", draft);
    const discard = document.createElement("button");
    discard.type = "button";
    discard.className = "btn";
    discard.textContent = "Discard";
    discard.onclick = () => studioDraftAction("discard", draft);
    const actions = document.createElement("div");
    actions.className = "studio-draft-actions";
    actions.append(discard, approve);
    card.append(title, about, checks, code, actions);
    return card;
  }));
}

async function studioDraftAction(action, draft) {
  const question = action === "approve"
    ? `Approve ${draft.name}? This code will run on your computer with your permissions.`
    : `Discard the draft ${draft.name}?`;
  if (!window.confirm(question)) return;
  studioDraftError("");
  try {
    const result = await studioApi(`/api/blocks/drafts/${action}`, { name: draft.name, sha256: draft.sha256 });
    if (action === "approve") {
      Studio.blocks = new Map(result.blocks.map((block) => [block.name, block]));
      studioPalette();
      studioMessage(`Approved ${draft.name}. It is in the Plugins group.`);
    } else {
      studioMessage(`Discarded the draft ${draft.name}.`);
    }
  } catch (error) {
    studioDraftError(String(error.message || error));
  }
  await studioLoadDrafts();
}

function studioDraftsInit() {
  const dialog = document.getElementById("studio-drafts-dialog");
  document.getElementById("studio-drafts").onclick = () => {
    studioDraftError("");
    studioRenderDrafts();
    dialog.showModal();
  };
  document.getElementById("studio-drafts-close").onclick = () => dialog.close();
  studioLoadDrafts();
  // Drafts arrive from an agent at any time; check while the Editor is open.
  setInterval(() => { if (Studio.visible) studioLoadDrafts(); }, 15000);
}
