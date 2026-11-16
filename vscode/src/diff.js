"use strict";
// Read-only documents showing a task's changes: aaagent-diff:/<task>.diff?<project>

const vscode = require("vscode");

const SCHEME = "aaagent-diff";

class DiffProvider {
  constructor(cli) {
    this.cli = cli;
    this._onDidChange = new vscode.EventEmitter();
    this.onDidChange = this._onDidChange.event;
  }

  static uri(project, task) {
    const commit = task.status === "committed" && task.commit ? task.commit : "";
    return vscode.Uri.from({
      scheme: SCHEME,
      path: `/${task.id}.diff`,
      query: new URLSearchParams({ project, commit }).toString(),
    });
  }

  async provideTextDocumentContent(uri) {
    const params = new URLSearchParams(uri.query);
    const project = params.get("project");
    const commit = params.get("commit");
    const task = uri.path.replace(/^\//, "").replace(/\.diff$/, "");
    try {
      // `aaagent diff` shows the task's commit for a committed task (in every mode, No Git
      // included) and its uncommitted changes otherwise; plain `git show` fails in No Git.
      const out = await this.cli.diff(project, task);
      const header = commit
        ? `# aaagent: changes committed by ${task}\n\n`
        : `# aaagent: uncommitted changes for ${task} (since the task started)\n\n`;
      return `${header}${out}`;
    } catch (err) {
      return `Could not load the changes for ${task}:\n${err.message}`;
    }
  }

  refresh(uri) {
    this._onDidChange.fire(uri);
  }
}

module.exports = { DiffProvider, SCHEME };
