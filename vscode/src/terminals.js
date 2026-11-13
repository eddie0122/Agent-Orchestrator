"use strict";
// The chat and worker terminals. Each runs `aaagent` directly (no shell), so nothing needs quoting.

const vscode = require("vscode");
const model = require("./model");

const CHAT_NAME = "aaagent: chat";
const workerName = (name) => `aaagent: ${name}`;

function findTerminal(name) {
  return vscode.window.terminals.find((t) => t.name === name && t.exitStatus === undefined);
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

class Terminals {
  constructor(cli, store) {
    this.cli = cli;
    this.store = store;
  }

  chat() {
    return findTerminal(CHAT_NAME);
  }

  workerTerminal(name) {
    return findTerminal(workerName(name));
  }

  env(project) {
    return { AAAGENT_PROJECT: project };
  }

  /**
   * One terminal per worker, side by side in the panel. A worker that is busy in the
   * background keeps running; its terminal follows the log instead.
   */
  async ensureWorkers(project, { reveal = false } = {}) {
    const bin = this.cli.require();
    const workers = (this.store.data && this.store.data.workers) || [];
    let parent = null;
    let stoppedIdle = false;
    for (const w of workers) {
      const existing = this.workerTerminal(w.name);
      if (existing) {
        parent = parent || existing;
        continue;
      }
      let args = ["worker", w.name];
      if (w.pid && w.state === "running") {
        args = ["logs", "-f", "-n", "40", w.name];
      } else if (w.pid) {
        await this.cli.exec(project, ["stop", w.name]).catch(() => undefined);
        stoppedIdle = true;
      }
      const options = {
        name: workerName(w.name),
        shellPath: bin,
        shellArgs: args,
        cwd: project,
        env: this.env(project),
        iconPath: new vscode.ThemeIcon("server-process"),
        isTransient: false,
        location: parent ? { parentTerminal: parent } : vscode.TerminalLocation.Panel,
      };
      if (stoppedIdle) await sleep(400);
      const term = vscode.window.createTerminal(options);
      parent = parent || term;
    }
    if (reveal && parent) parent.show(true);
    return parent;
  }

  /**
   * Open (or show) the chat. A first prompt is passed as an argument, which is reliable at startup.
   * resume: reopen this project's last aaagent chat (`aaagent start --continue`).
   */
  async startChat(project, firstPrompt, { resume = false } = {}) {
    const existing = this.chat();
    if (existing) {
      existing.show(false);
      if (firstPrompt) await this.send(firstPrompt, { submit: true });
      return existing;
    }
    const bin = this.cli.require();
    await this.ensureWorkers(project);
    const where = vscode.workspace.getConfiguration("aaagent").get("chatLocation", "editor");
    const args = ["start", "--no-workers"];
    if (resume) args.push("--continue");
    if (firstPrompt) args.push(firstPrompt);
    const term = vscode.window.createTerminal({
      name: CHAT_NAME,
      shellPath: bin,
      shellArgs: args,
      cwd: project,
      env: this.env(project),
      iconPath: new vscode.ThemeIcon("hubot"),
      location: where === "panel"
        ? vscode.TerminalLocation.Panel
        : { viewColumn: vscode.ViewColumn.Active, preserveFocus: false },
    });
    term.show(false);
    return term;
  }

  /** Type text into the running chat. Multi-line text arrives as one paste. */
  async send(text, { submit }) {
    const term = this.chat();
    if (!term) return false;
    term.show(false);
    term.sendText(model.bracketedPaste(text), false);
    if (submit) {
      await sleep(150);
      term.sendText("\r", false);
    }
    return true;
  }

  async showWorker(project, name) {
    const existing = this.workerTerminal(name);
    if (existing) {
      existing.show(false);
      return existing;
    }
    const parent = await this.ensureWorkers(project);
    const term = this.workerTerminal(name) || parent;
    if (term) term.show(false);
    return term;
  }

  disposeWorkerTerminal(name) {
    const t = this.workerTerminal(name);
    if (t) t.dispose();
  }
}

module.exports = { Terminals, CHAT_NAME, workerName };
