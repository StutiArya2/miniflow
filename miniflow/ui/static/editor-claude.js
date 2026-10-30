"use strict";

/* Optional local Claude Code client. Conversation lives only in this dashboard process. */
const StudioClaude = { state: null, polling: false, connectTab: "inside", setup: null, rendered: "" };
const claudeEl = (id) => document.getElementById(id);

function claudeError(text) {
  const el = claudeEl("studio-claude-error");
  el.textContent = text || "";
  el.hidden = !text;
}

function claudeContext() {
  const selected = claudeEl("studio-flow").value;
  claudeEl("studio-claude-context").textContent = selected
    ? `Saved flow: ${selected}${Studio.dirty ? " · Unsaved edits are not included." : ""}`
    : "No saved flow selected. Claude can help you create one.";
  claudeEl("studio-claude-reload").disabled = !selected || !!StudioClaude.state?.active;
  return selected;
}

function claudeRender(state) {
  StudioClaude.state = state;
  const key = JSON.stringify(state.messages);
  const log = claudeEl("studio-claude-messages");
  if (key !== StudioClaude.rendered) {
    const nearEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    StudioClaude.rendered = key;
    if (!state.messages.length) {
      const empty = document.createElement("p");
      empty.className = "hint";
      empty.textContent = "Try: Explain this flow, or build a flow that reads a CSV and filters its rows.";
      log.replaceChildren(empty);
    } else {
      log.replaceChildren(...state.messages.map((message) => {
        const item = document.createElement("article");
        item.className = `claude-message claude-${message.role}`;
        const label = document.createElement("strong");
        label.textContent = message.role === "user" ? "You" : "Claude";
        const text = document.createElement("div");
        text.className = "claude-message-text";
        const content = message.text || (message.status === "working" ? "Thinking…"
          : message.status === "error" ? "This request could not be completed." : "");
        if (message.role === "assistant") text.append(claudeMarkdown(content));
        else text.textContent = content;
        item.append(label, text);
        if (message.tools?.length) {
          const tools = document.createElement("details");
          tools.className = "hint claude-tool-activity";
          const summary = document.createElement("summary");
          summary.textContent = `Activity · ${message.tools.length} actions`;
          const list = document.createElement("ul");
          message.tools.forEach((name) => {
            const item = document.createElement("li");
            item.textContent = name.replaceAll("_", " "); item.title = name; list.append(item);
          });
          tools.append(summary, list);
          item.append(tools);
        }
        if (message.status === "stopped") {
          const stopped = document.createElement("p");
          stopped.className = "hint"; stopped.textContent = "Stopped"; item.append(stopped);
        }
        return item;
      }));
    }
    if (nearEnd) log.scrollTop = log.scrollHeight;
  }
  claudeEl("studio-claude-send").disabled = state.active;
  claudeEl("studio-claude-new").disabled = state.active;
  claudeEl("studio-claude-stop").hidden = !state.active;
  claudeEl("studio-claude-state").textContent = state.active ? "Claude is working…" : "";
  claudeError(state.error);
  claudeContext();
}

async function claudePoll() {
  if (StudioClaude.polling) return;
  StudioClaude.polling = true;
  try {
    const previous = StudioClaude.state?.active;
    claudeRender(await studioApi("/api/agent/chat"));
    if (previous && !StudioClaude.state.active) {
      await studioList();
      await studioLoadDrafts();
    }
  } catch (error) { claudeError(error.message); }
  finally { StudioClaude.polling = false; }
}

async function claudeOpen() {
  claudeEl("studio-claude-pane").hidden = false;
  document.body.classList.add("claude-open");
  claudeEl("studio-ask-claude").setAttribute("aria-expanded", "true");
  claudeContext();
  await claudePoll();
  claudeEl("studio-claude-prompt").focus();
}

function claudeClose() {
  claudeEl("studio-claude-pane").hidden = true;
  document.body.classList.remove("claude-open");
  claudeEl("studio-ask-claude").setAttribute("aria-expanded", "false");
  claudeEl("studio-ask-claude").focus();
}

async function claudeSend(event) {
  event.preventDefault();
  const input = claudeEl("studio-claude-prompt");
  if (!input.value.trim()) return;
  claudeEl("studio-claude-send").disabled = true;
  claudeError("");
  try {
    claudeRender(await studioApi("/api/agent/send", { message: input.value, flow: claudeContext() }));
    input.value = "";
  } catch (error) {
    claudeError(error.message);
    claudeEl("studio-claude-send").disabled = !!StudioClaude.state?.active;
  }
}

function claudeConnectTab(name, focus = false) {
  StudioClaude.connectTab = name;
  for (const tab of ["inside", "code", "desktop"]) {
    const el = claudeEl(`claude-tab-${tab}`);
    el.setAttribute("aria-selected", String(tab === name));
    el.tabIndex = tab === name ? 0 : -1;
    claudeEl(`claude-connect-${tab}`).hidden = tab !== name;
  }
  if (focus) claudeEl(`claude-tab-${name}`).focus();
}

async function claudeCheck() {
  const check = claudeEl("claude-check"); check.disabled = true;
  try {
    const setup = await studioApi("/api/agent/setup");
    StudioClaude.setup = setup;
    claudeEl("claude-login-title").textContent = !setup.available ? "Install Claude Code to chat"
      : setup.login_active ? "Complete sign-in in your browser"
        : setup.needs_login ? "Your Claude login needs refreshing"
          : setup.login_found ? "Claude Code login found" : "Sign in to Claude";
    claudeEl("claude-login-detail").textContent = setup.login_error || (!setup.available
      ? "Install Claude Code using the instructions below, then check again."
      : setup.login_active ? "Return here after finishing browser sign-in, then check again."
        : setup.login_found && !setup.needs_login
          ? "Your local login is available. Send a message to verify it still works."
          : "Sign in to use your Claude account inside MiniFlow.");
    claudeEl("claude-login").disabled = !setup.available || setup.login_active;
    claudeEl("claude-login-command").value = setup.login_command;
    claudeEl("claude-code-command").value = setup.code_command;
    claudeEl("claude-desktop-config").value = JSON.stringify(setup.desktop, null, 2);
  } catch (error) { claudeEl("claude-connect-message").textContent = error.message; }
  finally { check.disabled = false; }
}

async function claudeConnect() {
  claudeEl("claude-connect-message").textContent = "";
  claudeConnectTab("inside");
  claudeEl("studio-claude-dialog").showModal();
  await claudeCheck();
}

async function claudeCopy(id) {
  const field = claudeEl(id);
  try {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
    await navigator.clipboard.writeText(field.value);
    claudeEl("claude-connect-message").textContent = "Copied.";
  } catch {
    field.focus(); field.select();
    claudeEl("claude-connect-message").textContent = "Text selected. Press ⌘C (Mac) or Ctrl+C to copy.";
  }
}

async function studioClaudeInit() {
  // Avoid an auth subprocess on startup; chat availability is a fast local route.
  try { StudioClaude.state = await studioApi("/api/agent/chat"); }
  catch { return; }
  claudeEl("studio-claude-actions").hidden = false;
  claudeEl("studio-ask-claude").onclick = claudeOpen;
  claudeEl("studio-connect-claude").onclick = claudeConnect;
  claudeEl("studio-claude-close").onclick = claudeClose;
  claudeEl("studio-claude-connect-close").onclick = () => claudeEl("studio-claude-dialog").close();
  claudeEl("studio-claude-form").onsubmit = claudeSend;
  claudeEl("studio-claude-stop").onclick = async () => {
    try { claudeRender(await studioApi("/api/agent/stop", { turn: StudioClaude.state.turn })); }
    catch (error) { claudeError(error.message); }
  };
  claudeEl("studio-claude-new").onclick = async () => {
    try { claudeRender(await studioApi("/api/agent/clear", {})); }
    catch (error) { claudeError(error.message); }
  };
  claudeEl("studio-claude-reload").onclick = async () => {
    const name = claudeContext();
    if (name) await studioOpen(name); // existing unsaved-change confirmation applies
  };
  claudeEl("claude-check").onclick = claudeCheck;
  claudeEl("claude-login").onclick = async () => {
    claudeEl("claude-login").disabled = true;
    try {
      await studioApi("/api/agent/login", {});
      claudeEl("claude-connect-message").textContent = "Finish sign-in in your browser, then check the connection.";
      await claudeCheck();
    } catch (error) {
      claudeEl("claude-connect-message").textContent = error.message;
      claudeEl("claude-login").disabled = false;
    }
  };
  for (const name of ["inside", "code", "desktop"]) {
    const tab = claudeEl(`claude-tab-${name}`);
    tab.onclick = () => claudeConnectTab(name);
    tab.onkeydown = (event) => {
      const names = ["inside", "code", "desktop"], index = names.indexOf(name);
      const next = event.key === "ArrowRight" ? (index + 1) % 3
        : event.key === "ArrowLeft" ? (index + 2) % 3 : event.key === "Home" ? 0 : event.key === "End" ? 2 : -1;
      if (next >= 0) { event.preventDefault(); claudeConnectTab(names[next], true); }
    };
  }
  document.querySelectorAll(".claude-copy").forEach((button) => { button.onclick = () => claudeCopy(button.dataset.copy); });
  claudeEl("studio-claude-prompt").onkeydown = (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter" && !StudioClaude.state?.active) claudeSend(event);
  };
  claudeEl("studio-claude-pane").onkeydown = (event) => {
    if (event.key === "Escape") { event.preventDefault(); claudeClose(); }
  };
  setInterval(() => {
    if (!claudeEl("studio-claude-pane").hidden || StudioClaude.state?.active) claudePoll();
  }, 750);
}
