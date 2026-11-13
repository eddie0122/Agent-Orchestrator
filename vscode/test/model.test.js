"use strict";
const test = require("node:test");
const assert = require("node:assert");
const model = require("../src/model");

const task = (over) => ({
  id: "004-health", number: 4, title: "health", status: "planning", round: 0, worker: null,
  verdict: null, plan_written: true, job: null, next: null, blocked: null, attention: null,
  commit: null, files: [], ...over,
});

test("visibleTasks keeps unfinished tasks and the newest finished ones", () => {
  const tasks = [
    task({ id: "001-a", number: 1, status: "committed" }),
    task({ id: "002-b", number: 2, status: "committed" }),
    task({ id: "003-c", number: 3, status: "abandoned" }),
    task({ id: "004-d", number: 4, status: "awaiting_review" }),
  ];
  assert.deepStrictEqual(model.visibleTasks(tasks, 1).map((t) => t.number), [4, 3]);
  assert.deepStrictEqual(model.visibleTasks(tasks).map((t) => t.number), [4, 3, 2, 1]);
});

test("taskSummary describes each stage", () => {
  assert.strictEqual(model.taskSummary(task({ plan_written: false })), "planning");
  assert.strictEqual(model.taskSummary(task({ next: { phase: "implement" } })), "plan ready");
  assert.strictEqual(model.taskSummary(task({ status: "running", job: { phase: "implement", worker: "deepseek" } })),
    "implementing · deepseek");
  assert.strictEqual(model.taskSummary(task({ status: "awaiting_review", round: 2 })), "in review (round 2)");
  assert.strictEqual(model.taskSummary(task({ status: "awaiting_review", round: 1, next: { phase: "commit" } })),
    "in review (round 1)");
  assert.strictEqual(model.taskSummary(task({ status: "committed", commit: "abcdef123456" })), "committed abcdef1");
  assert.strictEqual(model.taskSummary(task({ status: "failed", attention: "failed" })), "failed: needs you");
});

test("taskItem marks final and active tasks for menus", () => {
  assert.strictEqual(model.taskItem(task({ status: "committed" })).contextValue, "task.final");
  const item = model.taskItem(task({ next: { phase: "fix", round: 2, worker: "glm" } }));
  assert.strictEqual(item.contextValue, "task.active");
  assert.strictEqual(item.label, "004 health");
  assert.match(item.tooltip, /next: fix round 2 → glm/);
});

test("workerItem shows the job a worker is running and its model", () => {
  const w = model.workerItem({ name: "deepseek", model: "opencode-go/deepseek-v4.1-flash", pid: 9, state: "running",
    queued: 1, job: { task: "004-health", phase: "fix", round: 2 } });
  assert.strictEqual(w.description, "fixing 004 r2 · deepseek-v4.1-flash · 1 queued");
  assert.strictEqual(w.contextValue, "worker.running");
  assert.strictEqual(model.workerItem({ name: "glm", model: "opencode-go/glm-5.3-flash", state: "idle", queued: 0 }).description,
    "waiting for a plan · glm-5.3-flash");
  assert.strictEqual(model.workerItem({ name: "glm", model: "opencode-go/glm-5.3-flash", state: "stopped", queued: 0 }).description,
    "stopped");
});

const modelInfo = () => ({
  planner: { model: "opus", choices: ["sonnet", "opus", "haiku"] },
  workers: {
    deepseek: { model: "opencode-go/deepseek-v4.1-flash" },
    glm: { model: "opencode-go/glm-5.3-flash" },
  },
  worker_choices: ["opencode-go/kimi-k3", "opencode-go/glm-5.3-flash"],
});

test("modelTargets lists the planner first, then the workers", () => {
  assert.deepStrictEqual(model.modelTargets(modelInfo()), [
    { label: "Planner (Claude)", description: "opus", target: "planner" },
    { label: "Worker: deepseek", description: "opencode-go/deepseek-v4.1-flash", target: "deepseek" },
    { label: "Worker: glm", description: "opencode-go/glm-5.3-flash", target: "glm" },
  ]);
});

test("modelChoices offers typing first, then the current model first and marked", () => {
  const info = modelInfo();
  const planner = model.modelChoices(info, "planner");
  assert.strictEqual(planner[0].typed, true);
  assert.deepStrictEqual(planner.slice(1), [
    { label: "opus", model: "opus", description: "current" },
    { label: "sonnet", model: "sonnet" },
    { label: "haiku", model: "haiku" },
  ]);
  // the worker's current model is not in `opencode models`: it is added first
  assert.deepStrictEqual(model.modelChoices(info, "deepseek"), [
    { label: "$(edit) Type a model name…", typed: true },
    { label: "opencode-go/deepseek-v4.1-flash", model: "opencode-go/deepseek-v4.1-flash", description: "current" },
    { label: "opencode-go/kimi-k3", model: "opencode-go/kimi-k3" },
    { label: "opencode-go/glm-5.3-flash", model: "opencode-go/glm-5.3-flash" },
  ]);
});

test("statusBar prefers running work, then tasks needing the user", () => {
  const workers = [{ name: "deepseek", state: "idle" }, { name: "glm", state: "stopped" }];
  assert.match(model.statusBar(null).text, /aaagent$/);
  const running = { workers, active_task: "004-health",
    tasks: [task({ status: "running", job: { phase: "commit", worker: "glm" } })] };
  assert.strictEqual(model.statusBar(running).text, "$(sync~spin) aaagent: 004 committing · glm");
  const needs = { workers, planner: { model: "opus" }, active_task: null,
    tasks: [task({ status: "failed", attention: "failed" })] };
  assert.strictEqual(model.statusBar(needs).text, "$(warning) aaagent: 004 needs you");
  assert.match(model.statusBar(needs).tooltip, /^planner: opus\n/);
  assert.match(model.statusBar(needs).tooltip, /workers up: deepseek/);
});

test("transitions notify on commit, attention, and finished rounds; first load is silent", () => {
  const before = { tasks: [task({ status: "running" }), task({ id: "005-x", number: 5, status: "running" })] };
  const after = { tasks: [
    task({ status: "committed", commit: "1234567890" }),
    task({ id: "005-x", number: 5, status: "awaiting_review", round: 1 }),
  ] };
  assert.deepStrictEqual(model.transitions(null, after), []);
  const ev = model.transitions(before, after);
  assert.deepStrictEqual(ev.map((e) => e.level), ["info", "status"]);
  assert.match(ev[0].message, /committed \(1234567\)/);
  const failed = { tasks: [task({ status: "failed", attention: "failed" }), after.tasks[1]] };
  assert.strictEqual(model.transitions(before, failed)[0].level, "warning");
  assert.deepStrictEqual(model.transitions(failed, failed), []);
});

test("isBusy follows running jobs and queues", () => {
  assert.strictEqual(model.isBusy({ tasks: [task({})], workers: [{ state: "idle", queued: 0 }] }), false);
  assert.strictEqual(model.isBusy({ tasks: [task({ status: "running" })], workers: [] }), true);
  assert.strictEqual(model.isBusy({ tasks: [], workers: [{ state: "idle", queued: 1 }] }), true);
});

test("mention and bracketed paste", () => {
  assert.strictEqual(model.mention("src/app.py", 10, 20), "@src/app.py#L10-20 ");
  assert.strictEqual(model.mention("src/app.py", 7, 7), "@src/app.py#L7 ");
  assert.strictEqual(model.mention("a b.py"), '@"a b.py" ');
  assert.strictEqual(model.bracketedPaste("x\ny"), "\x1b[200~x\ny\x1b[201~");
});

test("sniffImage reads the magic bytes of the four accepted image types", () => {
  const png = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00]);
  assert.strictEqual(model.sniffImage(png), "png");
  assert.strictEqual(model.sniffImage(new Uint8Array(png)), "png");
  assert.strictEqual(model.sniffImage(Buffer.from([0xff, 0xd8, 0xff, 0xe0])), "jpg");
  assert.strictEqual(model.sniffImage(Buffer.from("GIF87a....")), "gif");
  assert.strictEqual(model.sniffImage(Buffer.from("GIF89a....")), "gif");
  const webp = Buffer.alloc(12);
  webp.write("RIFF", 0);
  webp.write("WEBP", 8);
  assert.strictEqual(model.sniffImage(webp), "webp");
  assert.strictEqual(model.sniffImage(Buffer.from("RIFFxxxxNOPE")), null);
  assert.strictEqual(model.sniffImage(Buffer.from("hello")), null);
  assert.strictEqual(model.sniffImage(Buffer.alloc(0)), null);
});

test("attachmentName mirrors the CLI names", () => {
  const date = new Date(2026, 2, 4, 5, 6, 7); // local time
  assert.strictEqual(model.attachmentName("shot 1.PNG", "png", date), "20260304-050607-shot-1.png");
  assert.strictEqual(model.attachmentName("스크린샷.png", "png", date), "20260304-050607-image.png");
  assert.strictEqual(model.attachmentName("", "png", date), "20260304-050607-image.png");
  assert.strictEqual(model.attachmentName("clip.bmp", "jpeg", date), "20260304-050607-clip.jpeg");
  assert.strictEqual(model.attachmentName("pasted", "webp", date), "20260304-050607-pasted.webp");
  assert.strictEqual(model.attachmentName("a--b.png", "png", date), "20260304-050607-a-b.png");
  assert.strictEqual(model.attachmentName(`${"x".repeat(60)}.png`, "png", date),
    `20260304-050607-${"x".repeat(40)}.png`);
});

test("isImagePath accepts the image extensions in any case", () => {
  assert.strictEqual(model.isImagePath("src/shot.PNG"), true);
  assert.strictEqual(model.isImagePath("a/b.jpg"), true);
  assert.strictEqual(model.isImagePath("a/b.jpeg"), true);
  assert.strictEqual(model.isImagePath("a/b.gif"), true);
  assert.strictEqual(model.isImagePath("a/b.webp"), true);
  assert.strictEqual(model.isImagePath("a/b.txt"), false);
  assert.strictEqual(model.isImagePath("a/b"), false);
  assert.strictEqual(model.isImagePath(".png"), false);
});
