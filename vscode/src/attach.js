"use strict";
// "Attach Image" webview and the saving of pasted/dropped images.
//
// The webview runs in the user's VSCode UI (their machine); its paste/drop events read local
// files and clipboard images and send them here as base64. This module runs in the extension
// host (the server, `extensionKind: workspace`), validates the bytes exactly like
// `aaagent attach`, and writes them into `<project>/.aaagent/attachments/`.

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const vscode = require("vscode");

const model = require("./model");

function attachmentsDir(project) {
  return path.join(project, ".aaagent", "attachments");
}

/** The image kind of `buffer`, or an Error with the CLI's message when it cannot be saved. */
function checkImage(buffer, name) {
  if (!buffer || !buffer.length) throw new Error(`${name} is empty`);
  if (buffer.length > model.MAX_IMAGE_BYTES) {
    const mb = (buffer.length / (1024 * 1024)).toFixed(1);
    throw new Error(`${name} is ${mb} MB; the limit is ${model.MAX_IMAGE_BYTES / (1024 * 1024)} MB`);
  }
  const kind = model.sniffImage(buffer);
  if (!kind) throw new Error(`${name} is not a PNG, JPEG, GIF or WebP image`);
  return kind;
}

/** Validate and write one image into the attachments folder. `{ path, rel }`. */
function saveAttachment(project, name, data) {
  const buffer = Buffer.isBuffer(data) ? data : Buffer.from(data || []);
  const kind = checkImage(buffer, name);
  const dir = attachmentsDir(project);
  fs.mkdirSync(dir, { recursive: true });
  const base = model.attachmentName(name, kind);
  const ext = path.extname(base);
  const stem = base.slice(0, base.length - ext.length);
  let target = path.join(dir, base);
  let count = 1;
  while (fs.existsSync(target)) {
    target = path.join(dir, `${stem}-${count}${ext}`);
    count += 1;
  }
  fs.writeFileSync(target, buffer);
  return { path: target, rel: path.relative(project, target).split(path.sep).join("/") };
}

function html(webview, nonce) {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${webview.cspSource} data: blob:; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>aaagent: Attach Image</title>
<style>
  body {
    font-family: var(--vscode-font-family);
    font-size: var(--vscode-font-size);
    color: var(--vscode-foreground);
    background: var(--vscode-editor-background);
    padding: 16px;
  }
  h1 { font-size: 1.2em; font-weight: 600; margin: 0 0 12px; }
  .drop {
    border: 2px dashed var(--vscode-panel-border, var(--vscode-focusBorder));
    border-radius: 6px;
    padding: 32px 16px;
    text-align: center;
    outline: none;
  }
  .drop:focus, .drop.over { border-color: var(--vscode-focusBorder); }
  .drop.over { background: var(--vscode-list-hoverBackground); }
  .drop p { margin: 0 0 8px; }
  .hint { color: var(--vscode-descriptionForeground); font-size: 0.9em; }
  kbd {
    font-family: var(--vscode-editor-font-family);
    border: 1px solid var(--vscode-panel-border, var(--vscode-foreground));
    border-radius: 3px;
    padding: 0 4px;
  }
  button {
    margin-top: 8px;
    color: var(--vscode-button-foreground);
    background: var(--vscode-button-background);
    border: none;
    border-radius: 2px;
    padding: 6px 12px;
    cursor: pointer;
    font-family: inherit;
    font-size: inherit;
  }
  button:hover { background: var(--vscode-button-hoverBackground); }
  button:focus { outline: 1px solid var(--vscode-focusBorder); }
  ul { list-style: none; margin: 16px 0 0; padding: 0; }
  li {
    display: flex;
    gap: 12px;
    align-items: center;
    padding: 8px;
    border-bottom: 1px solid var(--vscode-panel-border, transparent);
  }
  li img { width: 56px; height: 56px; object-fit: cover; border-radius: 3px; }
  .status { color: var(--vscode-descriptionForeground); font-size: 0.9em; word-break: break-all; }
  .status.error { color: var(--vscode-errorForeground); }
</style>
</head>
<body>
<h1>Attach Image</h1>
<div class="drop" id="drop" tabindex="0">
  <p>Paste a screenshot here (Ctrl+V / ⌘V), or hold Shift and drop image files</p>
  <p class="hint">Hold <kbd>Shift</kbd> while dropping from your computer, otherwise VSCode opens the file. Up to 5 MB: PNG, JPEG, GIF or WebP.</p>
  <button id="choose" type="button">Choose files…</button>
</div>
<input id="picker" type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple hidden>
<ul id="list"></ul>
<script nonce="${nonce}">
  const vscode = acquireVsCodeApi();
  const MAX_BYTES = ${model.MAX_IMAGE_BYTES};
  const drop = document.getElementById("drop");
  const picker = document.getElementById("picker");
  const list = document.getElementById("list");
  const pending = [];
  let nextId = 1;

  function row(id, name) {
    const item = document.createElement("li");
    const thumb = document.createElement("img");
    thumb.alt = "";
    const info = document.createElement("div");
    const title = document.createElement("div");
    title.textContent = name;
    const status = document.createElement("div");
    status.className = "status";
    status.textContent = "saving…";
    info.appendChild(title);
    info.appendChild(status);
    item.appendChild(thumb);
    item.appendChild(info);
    list.appendChild(item);
    return { id, item, thumb, status, name };
  }

  function add(file) {
    const name = file.name || "pasted.png";
    const id = nextId++;
    const entry = row(id, name);
    entry.thumb.src = URL.createObjectURL(file);
    if (file.size > MAX_BYTES) {
      entry.status.textContent = (file.size / (1024 * 1024)).toFixed(1) + " MB is too large; the limit is 5 MB";
      entry.status.className = "status error";
      return;
    }
    pending.push(entry);
    const reader = new FileReader();
    reader.onload = () => {
      const data = String(reader.result).replace(/^data:[^,]*,/, "");
      vscode.postMessage({ type: "image", id, name, data });
    };
    reader.onerror = () => {
      entry.status.textContent = "could not read the file";
      entry.status.className = "status error";
    };
    reader.readAsDataURL(file);
  }

  function take(id) {
    const i = pending.findIndex((e) => e.id === id);
    return i >= 0 ? pending.splice(i, 1)[0] : undefined;
  }

  window.addEventListener("message", (event) => {
    const message = event.data || {};
    if (message.type === "saved") {
      const entry = take(message.id);
      if (entry) entry.status.textContent = "added to chat: " + message.name;
    } else if (message.type === "error") {
      const entry = take(message.id);
      if (entry) {
        entry.status.textContent = message.message;
        entry.status.className = "status error";
      }
    }
  });

  document.addEventListener("paste", (event) => {
    const items = (event.clipboardData && event.clipboardData.items) || [];
    let found = false;
    for (const item of items) {
      if (item.type && item.type.indexOf("image/") === 0) {
        const file = item.getAsFile();
        if (file) { found = true; add(file); }
      }
    }
    if (found) event.preventDefault();
  });

  drop.addEventListener("dragover", (event) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    drop.classList.add("over");
  });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    drop.classList.remove("over");
    const files = event.dataTransfer && event.dataTransfer.files ? Array.from(event.dataTransfer.files) : [];
    for (const file of files) add(file);
  });

  document.getElementById("choose").addEventListener("click", () => picker.click());
  picker.addEventListener("change", () => {
    for (const file of Array.from(picker.files || [])) add(file);
    picker.value = "";
  });

  window.addEventListener("load", () => {
    window.focus();
    drop.focus();
  });
</script>
</body>
</html>
`;
}

class AttachPanel {
  constructor(panel, project, onAttached) {
    this.panel = panel;
    this.project = project;
    this.onAttached = onAttached;
    // One image at a time: without a chat open the first message starts it and waits for
    // Claude, so a second message must not send its mention before that has finished.
    this.queue = Promise.resolve();
    const nonce = crypto.randomBytes(16).toString("base64");
    panel.webview.html = html(panel.webview, nonce);
    this.messageListener = panel.webview.onDidReceiveMessage((message) => {
      this.queue = this.queue.then(() => this.receive(message)).catch(() => {});
      return this.queue;
    });
    this.disposeListener = panel.onDidDispose(() => this.dispose());
  }

  async receive(message) {
    if (!message || message.type !== "image") return;
    const name = message.name || "pasted.png";
    try {
      const saved = saveAttachment(this.project, name, Buffer.from(String(message.data || ""), "base64"));
      await this.onAttached(saved.rel);
      this.panel.webview.postMessage({ type: "saved", id: message.id, name: saved.rel });
    } catch (err) {
      this.panel.webview.postMessage({ type: "error", id: message.id, name, message: err.message });
    }
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    if (this.messageListener) this.messageListener.dispose();
    if (this.disposeListener) this.disposeListener.dispose();
    if (AttachPanel.current === this) AttachPanel.current = null;
  }

  /** Show the panel for `project`, reusing the open one. */
  static show(context, { project, onAttached }) {
    if (AttachPanel.current) {
      AttachPanel.current.project = project;
      AttachPanel.current.onAttached = onAttached;
      AttachPanel.current.panel.reveal();
      return AttachPanel.current.panel;
    }
    const panel = vscode.window.createWebviewPanel(
      "aaagent.attach",
      "aaagent: Attach Image",
      { viewColumn: vscode.ViewColumn.Beside, preserveFocus: false },
      { enableScripts: true, retainContextWhenHidden: true });
    AttachPanel.current = new AttachPanel(panel, project, onAttached);
    return panel;
  }
}

AttachPanel.current = null;

module.exports = { AttachPanel, saveAttachment, checkImage, attachmentsDir, html };
