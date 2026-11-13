"use strict";
// aaagent for VSCode: sidebar with workers and tasks, chat and worker terminals, status bar.

const fs = require("fs");
const os = require("os");
const path = require("path");
const vscode = require("vscode");

const { Cli } = require("./src/cli");
const { DiffProvider, SCHEME } = require("./src/diff");
const model = require("./src/model");
const { Terminals } = require("./src/terminals");
const { TasksProvider, WorkersProvider } = require("./src/tree");

const STATE_MESSAGES = {
  noFolder: "Open a folder to use aaagent.",
  noCli: "The aaagent command was not found. Run install.sh in the aaagent folder, or set aaagent.path.",
  noRepo: "This folder is not a git repository.",
  error: "aaagent status failed.",
};

function activate(context) {
  const config = () => vscode.workspace.getConfiguration("aaagent");
  const cli = new Cli(() => config().get("path", ""));
  const store = { data: null, error: null, state: "loading" };
  const workers = new WorkersProvider(store);
  const tasks = new TasksProvider(store);
  const terminals = new Terminals(cli, store);
  const diffs = new DiffProvider(cli);
  const sub = (d) => context.subscriptions.push(d);

  const workersView = vscode.window.createTreeView("aaagent.workers", { treeDataProvider: workers });
  const tasksView = vscode.window.createTreeView("aaagent.tasks", { treeDataProvider: tasks, showCollapseAll: true });
  sub(workersView);
  sub(tasksView);
  sub(vscode.workspace.registerTextDocumentContentProvider(SCHEME, diffs));

  const statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  statusItem.command = "workbench.view.extension.aaagent";
  sub(statusItem);

  // -- project + refresh ------------------------------------------------------

  function currentFolder() {
    const folders = vscode.workspace.workspaceFolders || [];
    if (!folders.length) return null;
    const editor = vscode.window.activeTextEditor;
    if (editor && editor.document.uri.scheme === "file") {
      const f = vscode.workspace.getWorkspaceFolder(editor.document.uri);
      if (f) return f.uri.fsPath;
    }
    return folders[0].uri.fsPath;
  }

  let watcher = null;
  let watchedProject = null;
  function watch(project) {
    if (project === watchedProject) return;
    if (watcher) watcher.dispose();
    watchedProject = project;
    watcher = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(vscode.Uri.file(project), ".aaagent/**"));
    watcher.onDidCreate(scheduleRefresh);
    watcher.onDidChange(scheduleRefresh);
    watcher.onDidDelete(scheduleRefresh);
  }
  sub({ dispose: () => watcher && watcher.dispose() });

  function notify(events) {
    const level = config().get("notifications", "all");
    if (level === "off") return;
    for (const ev of events) {
      if (ev.level === "status") {
        if (level === "all") vscode.window.setStatusBarMessage(ev.message, 8000);
      } else if (ev.level === "warning") {
        vscode.window.showWarningMessage(ev.message, "Open Chat").then((pick) => {
          if (pick) vscode.commands.executeCommand("aaagent.startChat");
        });
      } else {
        vscode.window.showInformationMessage(ev.message, "Show Changes").then((pick) => {
          const t = store.data && store.data.tasks.find((x) => x.id === ev.task);
          if (pick && t) vscode.commands.executeCommand("aaagent.showDiff", { type: "task", task: t });
        });
      }
    }
  }

  function render() {
    workers.refresh();
    tasks.refresh();
    const sb = model.statusBar(store.data);
    statusItem.text = sb.text;
    statusItem.tooltip = store.data ? sb.tooltip : (STATE_MESSAGES[store.state] || sb.tooltip);
    statusItem.show();
    const needs = store.data ? store.data.tasks.filter((t) => t.attention && !model.isFinal(t)).length : 0;
    tasksView.badge = needs ? { value: needs, tooltip: `${needs} task(s) need you` } : undefined;
    tasksView.message = store.state === "error" && store.error
      ? `aaagent status failed: ${model.short(store.error.message, 200)}`
      : undefined;
  }

  // One refresh at a time; a request during a refresh queues exactly one more, and every
  // caller's promise settles after the data it asked for is loaded.
  let running = null;
  let queued = null;
  function refresh() {
    if (!running) {
      running = load().finally(() => { running = null; });
      return running;
    }
    if (!queued) queued = running.then(() => { queued = null; return refresh(); });
    return queued;
  }

  async function load() {
    {
      const folder = currentFolder();
      let state;
      if (!folder) {
        store.data = null;
        state = "noFolder";
      } else {
        try {
          const data = await cli.status(folder);
          const prev = store.data && store.data.project === data.project ? store.data : null;
          store.data = data;
          store.error = null;
          state = "ready";
          watch(data.project);
          notify(model.transitions(prev, data));
        } catch (err) {
          store.data = null;
          store.error = err;
          state = err.kind === "notFound" ? "noCli" : err.kind === "noRepo" ? "noRepo" : "error";
        }
      }
      store.state = state;
      await vscode.commands.executeCommand("setContext", "aaagent.state", state);
      render();
    }
  }

  let debounce = null;
  function scheduleRefresh() {
    if (disposed) return;
    clearTimeout(debounce);
    debounce = setTimeout(refresh, 300);
  }

  let pollTimer = null;
  let disposed = false;
  function poll() {
    if (disposed) return;
    const fast = model.isBusy(store.data);
    pollTimer = setTimeout(async () => {
      await refresh();
      poll();
    }, fast ? 3000 : 15000);
  }
  sub({ dispose: () => { disposed = true; clearTimeout(pollTimer); clearTimeout(debounce); } });

  sub(vscode.workspace.onDidChangeWorkspaceFolders(scheduleRefresh));
  sub(vscode.window.onDidChangeActiveTextEditor(() => {
    if (store.data && currentFolder() && !currentFolder().startsWith(store.data.project)) scheduleRefresh();
  }));
  sub(vscode.workspace.onDidChangeConfiguration((e) => {
    if (e.affectsConfiguration("aaagent")) scheduleRefresh();
  }));

  // -- helpers for commands ---------------------------------------------------

  function project() {
    if (store.data) return store.data.project;
    vscode.window.showErrorMessage(STATE_MESSAGES[store.state] || "aaagent is not ready yet.");
    return null;
  }

  async function pickTask(filter, placeHolder) {
    const list = ((store.data && store.data.tasks) || []).filter(filter).sort((a, b) => b.number - a.number);
    if (!list.length) {
      vscode.window.showInformationMessage("aaagent: no matching tasks.");
      return null;
    }
    const pick = await vscode.window.showQuickPick(
      list.map((t) => ({ label: model.taskItem(t).label, description: model.taskSummary(t), task: t })),
      { placeHolder });
    return pick ? pick.task : null;
  }

  async function pickWorker(placeHolder) {
    const list = (store.data && store.data.workers) || [];
    const pick = await vscode.window.showQuickPick(
      list.map((w) => ({ label: w.name, description: model.workerItem(w).description, worker: w })),
      { placeHolder });
    return pick ? pick.worker : null;
  }

  const taskOf = (node) => (node && node.task) || null;
  const workerOf = (node) => (node && node.name ? node : null);

  function command(id, fn) {
    sub(vscode.commands.registerCommand(id, async (...args) => {
      try {
        await fn(...args);
      } catch (err) {
        vscode.window.showErrorMessage(`aaagent: ${err.message}`);
      } finally {
        scheduleRefresh();
      }
    }));
  }

  // -- commands -----------------------------------------------------------------

  command("aaagent.refresh", () => refresh());

  command("aaagent.startChat", async () => {
    const p = project();
    if (p) await terminals.startChat(p);
  });

  command("aaagent.resumeChat", async () => {
    const p = project();
    if (p) await terminals.startChat(p, undefined, { resume: true });
  });

  command("aaagent.ask", async () => {
    const p = project();
    if (!p) return;
    const text = await vscode.window.showInputBox({
      title: "Ask Opus",
      prompt: "Opus plans the change, a worker implements it, Opus reviews, a worker commits.",
      placeHolder: "e.g. add a /health endpoint with a test",
      ignoreFocusOut: true,
    });
    if (!text || !text.trim()) return;
    if (terminals.chat()) await terminals.send(text.trim(), { submit: true });
    else await terminals.startChat(p, text.trim());
  });

  command("aaagent.sendSelection", async () => {
    const p = project();
    const editor = vscode.window.activeTextEditor;
    if (!p || !editor) return;
    const rel = path.relative(p, editor.document.uri.fsPath);
    if (rel.startsWith("..")) {
      vscode.window.showWarningMessage("aaagent: this file is outside the project.");
      return;
    }
    const sel = editor.selection;
    const ref = sel.isEmpty
      ? model.mention(rel)
      : model.mention(rel, sel.start.line + 1, sel.end.character === 0 && sel.end.line > sel.start.line ? sel.end.line : sel.end.line + 1);
    if (!terminals.chat()) {
      await terminals.startChat(p);
      await new Promise((r) => setTimeout(r, 5000));
    }
    await terminals.send(ref, { submit: false });
  });

  command("aaagent.showDiff", async (node) => {
    const p = project();
    if (!p) return;
    const task = taskOf(node) || await pickTask((t) => t.round > 0 || t.commit, "Show changes for which task?");
    if (!task) return;
    const uri = DiffProvider.uri(p, task);
    diffs.refresh(uri);
    const doc = await vscode.workspace.openTextDocument(uri);
    await vscode.languages.setTextDocumentLanguage(doc, "diff");
    await vscode.window.showTextDocument(doc, { preview: true });
  });

  command("aaagent.openTaskFolder", async (node) => {
    const task = taskOf(node) || await pickTask(() => true, "Reveal which task folder?");
    if (task) await vscode.commands.executeCommand("revealInExplorer", vscode.Uri.file(task.path));
  });

  command("aaagent.abandonTask", async (node) => {
    const p = project();
    if (!p) return;
    const task = taskOf(node) || await pickTask((t) => !model.isFinal(t), "Abandon which task?");
    if (!task) return;
    const ok = await vscode.window.showWarningMessage(
      `Abandon ${model.taskItem(task).label}?`,
      { modal: true, detail: "The task stops here. Its uncommitted changes stay in the working tree for you to keep or discard." },
      "Abandon");
    if (ok !== "Abandon") return;
    const out = await cli.exec(p, ["abandon", task.id]);
    vscode.window.showInformationMessage(model.short(out, 300));
  });

  command("aaagent.startWorkers", async () => {
    const p = project();
    if (p) await terminals.ensureWorkers(p, { reveal: true });
  });

  command("aaagent.showWorker", async (node) => {
    const p = project();
    if (!p) return;
    const w = workerOf(node) || await pickWorker("Show which worker?");
    if (w) await terminals.showWorker(p, w.name);
  });

  command("aaagent.stopWorker", async (node) => {
    const p = project();
    if (!p) return;
    const w = workerOf(node) || await pickWorker("Stop which worker?");
    if (!w) return;
    if (w.state === "running") {
      const ok = await vscode.window.showWarningMessage(
        `${w.name} is in the middle of a job. Stop it anyway?`,
        { modal: true, detail: "The job fails and aaagent hands it to the other worker." }, "Stop");
      if (ok !== "Stop") return;
    }
    await cli.exec(p, ["stop", w.name]);
  });

  command("aaagent.stopWorkers", async () => {
    const p = project();
    if (p) await cli.exec(p, ["stop"]);
  });

  command("aaagent.doctor", async () => {
    const bin = cli.require();
    const term = vscode.window.createTerminal({
      name: "aaagent: doctor", shellPath: "/bin/sh",
      shellArgs: ["-c", `"$0" doctor; printf '\\nPress Enter to close.'; read _`, bin],
      cwd: currentFolder() || os.homedir(),
    });
    term.show();
  });

  command("aaagent.openConfig", async () => {
    const file = path.join(os.homedir(), ".config", "aaagent", "config.toml");
    if (!fs.existsSync(file)) {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, "# aaagent user config. Every option and its default is in config.default.toml\n# in the aaagent folder.\n");
    }
    await vscode.window.showTextDocument(vscode.Uri.file(file));
  });

  refresh().then(poll);
  return { store, refresh }; // exposed for tests
}

function deactivate() {}

module.exports = { activate, deactivate };
