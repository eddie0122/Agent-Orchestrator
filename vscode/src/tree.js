"use strict";
// Tree views: Workers and Tasks.

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

module.exports = { WorkersProvider, TasksProvider };
