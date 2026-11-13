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
  // The `.aaagent/**` refresh watcher plus the image-fetch watcher.
  assert.strictEqual(rec.watchers.length, 2);
  assert.match(rec.treeViews["aaagent.workers"].description, /^planner: /);

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

  // Open Chat: one terminal per worker (split), then the chat as an editor tab
  await rec.commands.get("aaagent.openChat")();
  const names = rec.terminals.map((x) => x.name);
  assert.deepStrictEqual(names, ["aaagent: deepseek", "aaagent: glm", "aaagent: chat"]);
  const [ds, glm, chat] = rec.terminals;
  assert.deepStrictEqual(ds.creationOptions.shellArgs, ["worker", "deepseek"]);
  assert.strictEqual(ds.creationOptions.shellPath, AAAGENT);
  assert.strictEqual(glm.creationOptions.location.parentTerminal, ds);
  assert.deepStrictEqual(chat.creationOptions.shellArgs, ["start", "--no-workers"]);
  assert.strictEqual(chat.creationOptions.location.viewColumn, stub.ViewColumn.Active);
  assert.strictEqual(chat.creationOptions.cwd, api.store.data.project);
  // Only the chat asks the hook to copy the user's images; worker terminals are plain.
  assert.strictEqual(chat.creationOptions.env.AAAGENT_VSCODE, "1");
  assert.strictEqual(ds.creationOptions.env.AAAGENT_VSCODE, undefined);

  // Open Chat again reuses terminals
  await rec.commands.get("aaagent.openChat")();
  assert.strictEqual(rec.terminals.length, 3);

  // New Chat while busy asks first; Cancel keeps the current chat
  api.store.data.tasks.push({ id: "999-busy", status: "running" });
  rec.answer = undefined;
  await rec.commands.get("aaagent.newChat")();
  assert.ok(rec.messages.some(([lvl, m]) => lvl === "warning" && /new chat/i.test(m)), JSON.stringify(rec.messages));
  assert.strictEqual(chat.exitStatus, undefined);
  api.store.data.tasks.pop();

  // New Chat: the open chat is closed and a fresh one takes its place
  await rec.commands.get("aaagent.newChat")();
  assert.ok(chat.exitStatus !== undefined, "the old chat is disposed");
  const liveChats = rec.terminals.filter((x) => x.name === "aaagent: chat" && x.exitStatus === undefined);
  assert.strictEqual(liveChats.length, 1);
  const newChat = liveChats[0];
  assert.notStrictEqual(newChat, chat);
  assert.deepStrictEqual(newChat.creationOptions.shellArgs, ["start", "--no-workers"]);
  assert.strictEqual(rec.terminals.length, 4);

  // Ask with a running chat: pasted, then Enter
  rec.inputBox = "add tests\nfor app.py";
  await rec.commands.get("aaagent.ask")();
  await tick(200);
  assert.deepStrictEqual(newChat.sent, [["\x1b[200~add tests\nfor app.py\x1b[201~", false], ["\r", false]]);

  // Ask with no chat: the request becomes the chat's first prompt
  newChat.dispose();
  rec.inputBox = "explain app.py";
  await rec.commands.get("aaagent.ask")();
  const chat2 = rec.terminals.at(-1);
  assert.deepStrictEqual(chat2.creationOptions.shellArgs, ["start", "--no-workers", "explain app.py"]);

  // Resume Last Chat: shows an open chat; with none open, starts one with --continue
  await rec.commands.get("aaagent.resumeChat")();
  assert.strictEqual(rec.terminals.at(-1), chat2);
  chat2.dispose();
  await rec.commands.get("aaagent.resumeChat")();
  const chat3 = rec.terminals.at(-1);
  assert.deepStrictEqual(chat3.creationOptions.shellArgs, ["start", "--no-workers", "--continue"]);

  // Select Model: the planner from the list, saved to the user config, applied to the open chat.
  const userConfig = path.join(tmp, "user-config.toml");
  const oldUserConfig = process.env.AAAGENT_USER_CONFIG;
  const oldOpencode = process.env.AAAGENT_OPENCODE_BIN;
  process.env.AAAGENT_USER_CONFIG = userConfig;
  process.env.AAAGENT_OPENCODE_BIN = path.join(REPO, "tests", "fake_opencode.py");
  t.after(() => {
    if (oldUserConfig === undefined) delete process.env.AAAGENT_USER_CONFIG;
    else process.env.AAAGENT_USER_CONFIG = oldUserConfig;
    if (oldOpencode === undefined) delete process.env.AAAGENT_OPENCODE_BIN;
    else process.env.AAAGENT_OPENCODE_BIN = oldOpencode;
  });
  rec.answer = "Apply to Current Chat";
  rec.quickPicks = [
    (items) => items.find((i) => i.target === "planner"),
    (items) => items.find((i) => i.model === "sonnet"),
    (items) => items.find((i) => i.project === false),
  ];
  await rec.commands.get("aaagent.selectModel")();
  assert.match(fs.readFileSync(userConfig, "utf8"), /model = "sonnet"/);
  assert.ok(rec.messages.some(([lvl, m]) => lvl === "info" && /planner model: sonnet/.test(m)),
    JSON.stringify(rec.messages));
  assert.deepStrictEqual(chat3.sent.at(-2), ["\x1b[200~/model sonnet\x1b[201~", false]);

  // Select Model: a worker's model typed by hand, saved to this project only.
  rec.answer = undefined;
  rec.quickPicks = [
    (items) => items.find((i) => i.target === "deepseek"),
    (items) => items.find((i) => i.typed),
    (items) => items.find((i) => i.project === true),
  ];
  rec.inputBoxes = ["opencode-go/kimi-k3"];
  await rec.commands.get("aaagent.selectModel")();
  assert.match(fs.readFileSync(path.join(project, ".aaagent", "config.toml"), "utf8"),
    /model = "opencode-go\/kimi-k3"/);
  assert.ok(rec.messages.some(([lvl, m]) => lvl === "info" && /deepseek model: opencode-go\/kimi-k3/.test(m)),
    JSON.stringify(rec.messages));

  // Attach Image: one panel, reused on a second call, with a nonce CSP and the file button.
  const panelsBefore = rec.panels.length;
  await rec.commands.get("aaagent.attachImage")();
  assert.strictEqual(rec.panels.length, panelsBefore + 1);
  const panel = rec.panels.at(-1);
  assert.strictEqual(panel.viewType, "aaagent.attach");
  assert.strictEqual(panel.title, "aaagent: Attach Image");
  assert.match(panel.webview.html, /Content-Security-Policy" content="default-src 'none';/);
  assert.match(panel.webview.html, /script-src 'nonce-[^']+';/);
  assert.match(panel.webview.html, /Choose files…/);
  await rec.commands.get("aaagent.attachImage")();
  assert.strictEqual(rec.panels.length, panelsBefore + 1);
  assert.strictEqual(panel.revealed, 1);

  // Paste: the bytes land in .aaagent/attachments with a CLI-compatible name and are typed
  // into the chat input (no Enter).
  const png = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d]);
  await panel.webview._handler({ type: "image", id: 1, name: "shot 1.png", data: png.toString("base64") });
  const attachDir = path.join(project, ".aaagent", "attachments");
  const saved = fs.readdirSync(attachDir);
  const shot = saved.find((f) => f.endsWith("-shot-1.png"));
  assert.ok(shot, JSON.stringify(saved));
  assert.match(shot, /^\d{8}-\d{6}-shot-1\.png$/);
  assert.deepStrictEqual(fs.readFileSync(path.join(attachDir, shot)), png);
  assert.deepStrictEqual(chat3.sent.at(-1),
    [`\x1b[200~@.aaagent/attachments/${shot} \x1b[201~`, false]);
  assert.ok(panel.webview.posted.some((m) => m.type === "saved" && m.id === 1 && m.name === `.aaagent/attachments/${shot}`),
    JSON.stringify(panel.webview.posted));

  // Not an image: an error goes back to the panel and nothing is written.
  const attachmentsCount = fs.readdirSync(attachDir).length;
  await panel.webview._handler({ type: "image", id: 2, name: "notes.txt", data: Buffer.from("hello").toString("base64") });
  assert.strictEqual(fs.readdirSync(attachDir).length, attachmentsCount);
  assert.ok(panel.webview.posted.some((m) => m.type === "error" && m.id === 2 && m.name === "notes.txt" && /not a PNG/.test(m.message)),
    JSON.stringify(panel.webview.posted));

  // Two images back to back without awaiting between them: messages are handled one at a
  // time, the pastes reach the chat in send order, both files are saved, and every reply
  // echoes the id the webview sent.
  const beforeBurst = chat3.sent.length;
  const firstMessage = panel.webview._handler({ type: "image", id: 21, name: "first.png", data: png.toString("base64") });
  const secondMessage = panel.webview._handler({ type: "image", id: 22, name: "second.png", data: png.toString("base64") });
  await Promise.all([firstMessage, secondMessage]);
  const burst = chat3.sent.slice(beforeBurst);
  assert.strictEqual(burst.length, 2, JSON.stringify(burst));
  assert.match(burst[0][0], /^\x1b\[200~@\.aaagent\/attachments\/\d{8}-\d{6}-first\.png \x1b\[201~$/);
  assert.match(burst[1][0], /^\x1b\[200~@\.aaagent\/attachments\/\d{8}-\d{6}-second\.png \x1b\[201~$/);
  const attachNames = fs.readdirSync(attachDir);
  assert.ok(attachNames.some((f) => f.endsWith("-first.png")), JSON.stringify(attachNames));
  assert.ok(attachNames.some((f) => f.endsWith("-second.png")), JSON.stringify(attachNames));
  const savedFor = (id) => panel.webview.posted.find((m) => m.type === "saved" && m.id === id);
  assert.match(savedFor(21).name, /-first\.png$/);
  assert.match(savedFor(22).name, /-second\.png$/);
  assert.deepStrictEqual(
    panel.webview.posted.filter((m) => m.type === "saved").map((m) => m.id).slice(-2), [21, 22],
    JSON.stringify(panel.webview.posted));

  // Attach to aaagent Chat: images in the workspace are mentioned in one paste; a non-image
  // and a file outside the project are skipped with a warning.
  const one = path.join(project, "one.png");
  const two = path.join(project, "two.jpg");
  const notes = path.join(project, "notes.txt");
  const outside = path.join(tmp, "outside.png");
  fs.writeFileSync(one, png);
  fs.writeFileSync(two, Buffer.from([0xff, 0xd8, 0xff, 0xe0]));
  fs.writeFileSync(notes, "hello\n");
  fs.writeFileSync(outside, png);
  const messagesBefore = rec.messages.length;
  await rec.commands.get("aaagent.attachFile")(stub.Uri.file(one),
    [stub.Uri.file(one), stub.Uri.file(two), stub.Uri.file(notes), stub.Uri.file(outside)]);
  assert.deepStrictEqual(chat3.sent.at(-1), ["\x1b[200~@one.png @two.jpg \x1b[201~", false]);
  const attachWarnings = rec.messages.slice(messagesBefore).filter(([lvl]) => lvl === "warning").map(([, m]) => m);
  assert.ok(attachWarnings.some((m) => /notes\.txt is not an image/.test(m)), JSON.stringify(attachWarnings));
  assert.ok(attachWarnings.some((m) => /outside the project/.test(m)), JSON.stringify(attachWarnings));

  // No uris array: the single uri is used.
  await rec.commands.get("aaagent.attachFile")(stub.Uri.file(two), []);
  assert.deepStrictEqual(chat3.sent.at(-1), ["\x1b[200~@two.jpg \x1b[201~", false]);

  // An image request the file watcher missed is picked up by the refresh's scan(): the image is
  // copied into .aaagent/attachments and the answer is written back for the chat hook.
  const fetchDir = path.join(project, ".aaagent", "fetch");
  fs.mkdirSync(fetchDir, { recursive: true });
  const localImage = path.join(tmp, "band.png");
  fs.writeFileSync(localImage, png);
  fs.writeFileSync(path.join(fetchDir, "beef.request.json"),
    JSON.stringify({ path: localImage, created: Date.now() / 1000 }));
  await api.refresh();
  // `scan()` runs alongside the refresh, not inside it.
  const resultFile = path.join(fetchDir, "beef.result.json");
  for (let i = 0; i < 100 && !fs.existsSync(resultFile); i++) await tick(20);
  const fetched = JSON.parse(fs.readFileSync(resultFile, "utf8"));
  assert.strictEqual(fetched.ok, true);
  assert.match(fetched.rel, /^\.aaagent\/attachments\/\d{8}-\d{6}-band\.png$/);
  assert.deepStrictEqual(fs.readFileSync(path.join(project, fetched.rel)), png);
  assert.ok(rec.messages.some(([lvl, m]) => lvl === "status" && /copied \d{8}-\d{6}-band\.png from your computer/.test(m)),
    JSON.stringify(rec.messages.slice(-5)));

  // A closed panel is forgotten: the next call opens a fresh one.
  panel.dispose();
  await rec.commands.get("aaagent.attachImage")();
  assert.strictEqual(rec.panels.length, panelsBefore + 2);
  rec.panels.at(-1).dispose();

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
