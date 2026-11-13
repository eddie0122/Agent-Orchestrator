"use strict";
// Runs activate() against a stub `vscode` module and a real temporary project, using the real
// aaagent CLI from this repo. No model is called: tasks are created and reviewed by hand.

const test = require("node:test");
const assert = require("node:assert");
const cp = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const Module = require("module");

const stub = require("./vscode-stub");
const origResolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  if (request === "vscode") return require.resolve("./vscode-stub");
  return origResolve.call(this, request, ...rest);
};

const REPO = path.resolve(__dirname, "..", "..");
const AAAGENT = path.join(REPO, "bin", "aaagent");
const pkg = require("../package.json");

function sh(cwd, cmd, args) {
  return cp.execFileSync(cmd, args, { cwd, encoding: "utf8",
    env: { ...process.env, GIT_AUTHOR_NAME: "t", GIT_AUTHOR_EMAIL: "t@example.invalid",
      GIT_COMMITTER_NAME: "t", GIT_COMMITTER_EMAIL: "t@example.invalid", AAAGENT_PROJECT: "" } });
}

const tick = (ms = 50) => new Promise((r) => setTimeout(r, ms));

test("manifest: every command in a menu is declared, and every declared command is registered", async () => {
  const declared = new Set(pkg.contributes.commands.map((c) => c.command));
  for (const [where, items] of Object.entries(pkg.contributes.menus)) {
    for (const item of items) assert.ok(declared.has(item.command), `${where} uses undeclared ${item.command}`);
  }
  for (const w of pkg.contributes.viewsWelcome) {
    for (const m of w.contents.matchAll(/command:(aaagent\.[A-Za-z]+)/g)) assert.ok(declared.has(m[1]), m[1]);
  }
  const views = pkg.contributes.views.aaagent.map((v) => v.id);
  assert.deepStrictEqual(views, ["aaagent.workers", "aaagent.tasks"]);
  assert.ok(fs.existsSync(path.join(__dirname, "..", pkg.contributes.viewsContainers.activitybar[0].icon)));
  // registration is checked after activation below
  test.declared = declared;
});

test("activate() against a real project", async (t) => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "aaagent-ext-"));
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
  const project = path.join(tmp, "proj");
  fs.mkdirSync(project);
  sh(project, "git", ["init", "-q"]);
  fs.writeFileSync(path.join(project, "README.md"), "hi\n");
  sh(project, "git", ["add", "."]);
  sh(project, "git", ["commit", "-q", "-m", "init"]);

  const rec = stub._record;
  rec.config = { path: AAAGENT };
  stub.setFolder(project);

  const ext = require("../extension");
  const ctx = { subscriptions: [] };
  const api = ext.activate(ctx);
  t.after(() => ctx.subscriptions.forEach((d) => d.dispose && d.dispose()));
  await api.refresh();

  // every declared command is registered
  const declared = new Set(pkg.contributes.commands.map((c) => c.command));
  for (const id of declared) assert.ok(rec.commands.has(id), `not registered: ${id}`);

  assert.strictEqual(rec.context["aaagent.state"], "ready");
  assert.strictEqual(api.store.data.project, fs.realpathSync(project));
  const workersTree = rec.treeViews["aaagent.workers"].treeDataProvider;
  const tasksTree = rec.treeViews["aaagent.tasks"].treeDataProvider;
  assert.deepStrictEqual(workersTree.getChildren().map((w) => w.name), ["deepseek", "glm"]);
  assert.strictEqual(workersTree.getTreeItem(workersTree.getChildren()[0]).description, "stopped");
  assert.deepStrictEqual(tasksTree.getChildren(), []);
  assert.strictEqual(rec.watchers.length, 1);

  // a task with a plan, a (hand-written) report and a review
  sh(project, AAAGENT, ["new", "health endpoint"]);
  const taskDir = path.join(project, ".aaagent", "tasks", "001-health-endpoint");
  fs.writeFileSync(path.join(taskDir, "plan.md"), "---\ntask: 001\n---\n# Goal\nx\n");
  fs.writeFileSync(path.join(project, "app.py"), "print('ok')\n");
  fs.writeFileSync(path.join(taskDir, "report-1.md"), "---\nround: 1\nstatus: done\n---\n");
  fs.writeFileSync(path.join(taskDir, "review-1.md"), "---\nverdict: changes_requested\nround: 1\n---\n");
  await api.refresh();

  const nodes = tasksTree.getChildren();
  assert.strictEqual(nodes.length, 1);
  const item = tasksTree.getTreeItem(nodes[0]);
  assert.strictEqual(item.label, "001 health endpoint");
  assert.strictEqual(item.contextValue, "task.active");
  const kids = tasksTree.getChildren(nodes[0]);
  assert.deepStrictEqual(kids.map((k) => (k.file ? k.file.name : k.type)), ["plan.md", "report-1.md", "review-1.md", "changes"]);
  const reviewItem = tasksTree.getTreeItem(kids[2]);
  assert.strictEqual(reviewItem.description, "changes requested");
  assert.strictEqual(reviewItem.command.command, "vscode.open");

  // diff document via the content provider
  await rec.commands.get("aaagent.showDiff")(nodes[0]);
  const doc = rec.opened.at(-1);
  assert.strictEqual(doc.languageId, "diff");
  assert.match(doc.text, /\+print\('ok'\)/);

  // status bar
  assert.match(rec.statusItems[0].text, /aaagent/);

  // Start Chat: one terminal per worker (split), then the chat as an editor tab
  await rec.commands.get("aaagent.startChat")();
  const names = rec.terminals.map((x) => x.name);
  assert.deepStrictEqual(names, ["aaagent: deepseek", "aaagent: glm", "aaagent: chat"]);
  const [ds, glm, chat] = rec.terminals;
  assert.deepStrictEqual(ds.creationOptions.shellArgs, ["worker", "deepseek"]);
  assert.strictEqual(ds.creationOptions.shellPath, AAAGENT);
  assert.strictEqual(glm.creationOptions.location.parentTerminal, ds);
  assert.deepStrictEqual(chat.creationOptions.shellArgs, ["start", "--no-workers"]);
  assert.strictEqual(chat.creationOptions.location.viewColumn, stub.ViewColumn.Active);
  assert.strictEqual(chat.creationOptions.cwd, api.store.data.project);

  // Start Chat again reuses terminals
  await rec.commands.get("aaagent.startChat")();
  assert.strictEqual(rec.terminals.length, 3);

  // Ask with a running chat: pasted, then Enter
  rec.inputBox = "add tests\nfor app.py";
  await rec.commands.get("aaagent.ask")();
  await tick(200);
  assert.deepStrictEqual(chat.sent, [["\x1b[200~add tests\nfor app.py\x1b[201~", false], ["\r", false]]);

  // Ask with no chat: the request becomes the chat's first prompt
  chat.dispose();
  rec.inputBox = "explain app.py";
  await rec.commands.get("aaagent.ask")();
  const chat2 = rec.terminals.at(-1);
  assert.deepStrictEqual(chat2.creationOptions.shellArgs, ["start", "--no-workers", "explain app.py"]);

  // Resume Last Chat: shows an open chat; with none open, starts one with --continue
  await rec.commands.get("aaagent.resumeChat")();
  assert.strictEqual(rec.terminals.at(-1), chat2);
  chat2.dispose();
  await rec.commands.get("aaagent.resumeChat")();
  assert.deepStrictEqual(rec.terminals.at(-1).creationOptions.shellArgs, ["start", "--no-workers", "--continue"]);

  // abandon (confirmed)
  rec.answer = "Abandon";
  await rec.commands.get("aaagent.abandonTask")(nodes[0]);
  await api.refresh();
  assert.strictEqual(api.store.data.tasks[0].status, "abandoned");
  assert.strictEqual(tasksTree.getTreeItem(tasksTree.getChildren()[0]).contextValue, "task.final");
  assert.ok(!rec.messages.some(([lvl]) => lvl === "error"), JSON.stringify(rec.messages));
});

test("not a git repository and missing CLI show the right welcome state", async (t) => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "aaagent-ext-"));
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
  const rec = stub._record;
  delete require.cache[require.resolve("../extension")];
  const ext = require("../extension");

  stub.setFolder(tmp);
  rec.config = { path: AAAGENT };
  const ctx = { subscriptions: [] };
  const api = ext.activate(ctx);
  t.after(() => ctx.subscriptions.forEach((d) => d.dispose && d.dispose()));
  await api.refresh();
  assert.strictEqual(rec.context["aaagent.state"], "noRepo");

  rec.config = { path: path.join(tmp, "missing-aaagent") };
  await api.refresh();
  assert.strictEqual(rec.context["aaagent.state"], "noCli");
});
