"use strict";
// Read-only documents showing a task's changes: aaagent-diff:/<task>.diff?<project>

const cp = require("child_process");
const vscode = require("vscode");

const SCHEME = "aaagent-diff";

function git(cwd, args) {
  return new Promise((resolve, reject) => {
    cp.execFile("git", args, { cwd, maxBuffer: 32 * 1024 * 1024 }, (err, stdout, stderr) =>
      err ? reject(new Error(stderr || err.message)) : resolve(stdout));
  });
}

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
      if (commit) {
        return await git(project, ["show", "--stat", "--patch", "--format=commit %H%nAuthor: %an%nDate:   %ad%n%n    %s%n%n%b", commit]);
      }
      const out = await this.cli.diff(project, task);
      return `# aaagent: uncommitted changes for ${task} (since the task started)\n\n${out}`;
    } catch (err) {
      return `Could not load the changes for ${task}:\n${err.message}`;
    }
  }

  refresh(uri) {
    this._onDidChange.fire(uri);
  }
}

module.exports = { DiffProvider, SCHEME };
