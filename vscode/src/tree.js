"use strict";
// Tree views: Workers, Sessions and Tasks.

const vscode = require("vscode");
const model = require("./model");

function toTreeItem(spec, collapsible = vscode.TreeItemCollapsibleState.None) {
  const item = new vscode.TreeItem(spec.label, collapsible);
  item.id = spec.id;
  item.description = spec.description;
  item.tooltip = spec.tooltip;
  item.contextValue = spec.contextValue;
  if (spec.icon) {
    item.iconPath = spec.icon.color
      ? new vscode.ThemeIcon(spec.icon.id, new vscode.ThemeColor(spec.icon.color))
      : new vscode.ThemeIcon(spec.icon.id);
  }
  return item;
}

class WorkersProvider {
  constructor(store) {
    this.store = store;
    this._onDidChange = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._onDidChange.event;
  }

  refresh() {
    this._onDidChange.fire();
  }

  getTreeItem(node) {
    const item = toTreeItem(model.workerItem(node));
    item.command = { command: "aaagent.showWorker", title: "Show Worker", arguments: [node] };
    return item;
  }

  getChildren(node) {
    if (node) return [];
    return (this.store.data && this.store.data.workers) || [];
  }
}

class TasksProvider {
  constructor(store) {
    this.store = store;
    this._onDidChange = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._onDidChange.event;
  }

  refresh() {
    this._onDidChange.fire();
  }

  getTreeItem(node) {
    if (node.type === "task") {
      const t = node.task;
      const expanded = !model.isFinal(t) && t.id === (this.store.data && this.store.data.active_task);
      return toTreeItem(model.taskItem(t), expanded
        ? vscode.TreeItemCollapsibleState.Expanded
        : vscode.TreeItemCollapsibleState.Collapsed);
    }
    if (node.type === "file") {
      const spec = model.fileItem(node.task, node.file);
      const item = toTreeItem(spec);
      item.resourceUri = vscode.Uri.file(node.file.path);
      item.command = { command: "vscode.open", title: "Open", arguments: [vscode.Uri.file(node.file.path)] };
      return item;
    }
    // changes
    const item = toTreeItem({
      id: `changes:${node.task.id}`, label: "Changes", description: "diff since the task started",
      icon: { id: "diff" }, contextValue: "changes",
    });
    item.command = { command: "aaagent.showDiff", title: "Show Diff", arguments: [{ type: "task", task: node.task }] };
    return item;
  }

  getChildren(node) {
    const data = this.store.data;
    if (!data) return [];
    if (!node) {
      const keep = vscode.workspace.getConfiguration("aaagent").get("finishedTasksShown", 10);
      return model.visibleTasks(data.tasks, keep).map((task) => ({ type: "task", task }));
    }
    if (node.type === "task") {
      const files = node.task.files.map((file) => ({ type: "file", task: node.task, file }));
      return node.task.round > 0 || node.task.status === "committed"
        ? [...files, { type: "changes", task: node.task }]
        : files;
    }
    return [];
  }
}

class SessionsProvider {
  constructor(store, { isOpen = () => false, tasks }) {
    this.store = store;
    this.isOpen = isOpen;
    this.tasks = tasks;
    this._onDidChange = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._onDidChange.event;
  }

  refresh() {
    this._onDidChange.fire();
  }

  getTreeItem(node) {
    if (node.type === "session") {
      const s = node.session;
      const open = this.isOpen(s);
      const hasTasks = !!(s.tasks && s.tasks.length);
      const collapsible = !hasTasks
        ? vscode.TreeItemCollapsibleState.None
        : open ? vscode.TreeItemCollapsibleState.Expanded
          : vscode.TreeItemCollapsibleState.Collapsed;
      return toTreeItem(model.sessionItem(s, { open }), collapsible);
    }
    // A task (or its files and Changes) under a session: the Tasks view builds the item.
    // A tree's ids must be unique, so the session id goes in front.
    const item = this.tasks.getTreeItem(node);
    item.id = `s:${node.sid}:${item.id}`;
    return item;
  }

  getChildren(node) {
    const data = this.store.data;
    if (!data) return [];
    if (!node) return (data.sessions || []).map((session) => ({ type: "session", session }));
    if (node.type === "session") {
      const byId = new Map((data.tasks || []).map((task) => [task.id, task]));
      return (node.session.tasks || [])
        .map((id) => byId.get(id))
        .filter(Boolean)
        .map((task) => ({ type: "task", task, sid: node.session.id }));
    }
    return this.tasks.getChildren(node).map((child) => ({ ...child, sid: node.sid }));
  }
}

module.exports = { WorkersProvider, TasksProvider, SessionsProvider };
