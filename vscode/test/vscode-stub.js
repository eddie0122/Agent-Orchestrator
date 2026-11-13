"use strict";
// Minimal stand-in for the `vscode` module: enough to run activate() and record what it does.

const path = require("path");

const record = {
  commands: new Map(), executed: [], terminals: [], messages: [], context: {}, treeViews: {},
  statusItems: [], contentProviders: {}, watchers: [], config: {}, opened: [],
};

class EventEmitter {
  constructor() { this.listeners = []; this.event = (fn) => { this.listeners.push(fn); return { dispose() {} }; }; }
  fire(v) { for (const l of this.listeners) l(v); }
}

class Uri {
  constructor({ scheme = "file", path: p = "", query = "" }) { this.scheme = scheme; this.path = p; this.query = query; }
  get fsPath() { return this.path; }
  static file(p) { return new Uri({ scheme: "file", path: p }); }
  static from(parts) { return new Uri(parts); }
  toString() { return `${this.scheme}:${this.path}${this.query ? "?" + this.query : ""}`; }
}

class TreeItem { constructor(label, collapsibleState) { this.label = label; this.collapsibleState = collapsibleState; } }
class ThemeIcon { constructor(id, color) { this.id = id; this.color = color; } }
class ThemeColor { constructor(id) { this.id = id; } }
class RelativePattern { constructor(base, pattern) { this.base = base; this.pattern = pattern; } }

function makeTerminal(options) {
  const t = { name: options.name, creationOptions: options, exitStatus: undefined, sent: [], shown: 0,
    sendText(text, addNewLine) { this.sent.push([text, addNewLine]); },
    show() { this.shown += 1; },
    dispose() { this.exitStatus = { code: 0 }; } };
  record.terminals.push(t);
  return t;
}

const vscode = {
  _record: record,
  EventEmitter, Uri, TreeItem, ThemeIcon, ThemeColor, RelativePattern,
  TreeItemCollapsibleState: { None: 0, Collapsed: 1, Expanded: 2 },
  StatusBarAlignment: { Left: 1, Right: 2 },
  TerminalLocation: { Panel: 1, Editor: 2 },
  ViewColumn: { Active: -1, Beside: -2 },
  commands: {
    registerCommand(id, fn) { record.commands.set(id, fn); return { dispose() {} }; },
    async executeCommand(id, ...args) {
      record.executed.push([id, ...args]);
      if (id === "setContext") record.context[args[0]] = args[1];
      else if (record.commands.has(id)) return record.commands.get(id)(...args);
      return undefined;
    },
  },
  window: {
    get terminals() { return record.terminals.filter((t) => t.exitStatus === undefined); },
    activeTextEditor: undefined,
    createTreeView(id, opts) { const v = { id, ...opts, dispose() {} }; record.treeViews[id] = v; return v; },
    createStatusBarItem() { const s = { show() { this.visible = true; }, dispose() {} }; record.statusItems.push(s); return s; },
    createTerminal: makeTerminal,
    showInformationMessage: async (m) => { record.messages.push(["info", m]); },
    showWarningMessage: async (m) => { record.messages.push(["warning", m]); return record.answer; },
    showErrorMessage: async (m) => { record.messages.push(["error", m]); },
    setStatusBarMessage: (m) => { record.messages.push(["status", m]); return { dispose() {} }; },
    showInputBox: async () => record.inputBox,
    showQuickPick: async (items) => items[0],
    showTextDocument: async (doc) => { record.opened.push(doc); },
    onDidChangeActiveTextEditor: () => ({ dispose() {} }),
  },
  workspace: {
    workspaceFolders: [],
    getWorkspaceFolder: () => undefined,
    getConfiguration: () => ({ get: (k, d) => (k in record.config ? record.config[k] : d) }),
    registerTextDocumentContentProvider(scheme, p) { record.contentProviders[scheme] = p; return { dispose() {} }; },
    createFileSystemWatcher(pattern) {
      const w = { pattern, onDidCreate() {}, onDidChange() {}, onDidDelete() {}, dispose() {} };
      record.watchers.push(w);
      return w;
    },
    openTextDocument: async (uri) => ({ uri, text: await record.contentProviders[uri.scheme].provideTextDocumentContent(uri) }),
    onDidChangeWorkspaceFolders: () => ({ dispose() {} }),
    onDidChangeConfiguration: () => ({ dispose() {} }),
  },
  languages: { setTextDocumentLanguage: async (doc, lang) => { doc.languageId = lang; return doc; } },
  setFolder(p) { vscode.workspace.workspaceFolders = [{ uri: Uri.file(p), name: path.basename(p), index: 0 }]; },
};

module.exports = vscode;
