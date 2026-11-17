"use strict";
// The extension side of the image bridge (`src/fetch.js`), with a faked user's computer.

const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const Module = require("module");

const origResolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  if (request === "vscode") return require.resolve("./vscode-stub");
  return origResolve.call(this, request, ...rest);
};

const stub = require("./vscode-stub");
const { LocalFetcher, localUri, expandHome } = require("../src/fetch");
const model = require("../src/model");

// The first bytes of a PNG; `sniffImage` only looks at the magic bytes.
const PNG = Buffer.concat([
  Buffer.from("89504e470d0a1a0a0000000d49484452", "hex"),
  Buffer.alloc(24),
]);

function makeProject(t) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "aaagent-fetch-"));
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
  const dir = path.join(tmp, "proj");
  fs.mkdirSync(path.join(dir, ".aaagent", "fetch"), { recursive: true });
  return dir;
}

function addRequest(dir, uuid, payload) {
  const file = path.join(dir, ".aaagent", "fetch", `${uuid}.request.json`);
  fs.writeFileSync(file, JSON.stringify(payload));
  return file;
}

const readResult = (dir, uuid) =>
  JSON.parse(fs.readFileSync(path.join(dir, ".aaagent", "fetch", `${uuid}.result.json`), "utf8"));

test("copies a requested image into .aaagent/attachments and answers the hook", async (t) => {
  const dir = makeProject(t);
  const request = addRequest(dir, "abc", { path: "/Users/hanul/Downloads/band.jpg", created: Date.now() / 1000 });
  const asked = [];
  const saved = [];
  const fetcher = new LocalFetcher({
    project: dir,
    readLocal: async (p) => { asked.push(p); return PNG; },
    onSaved: (rel, from) => saved.push([rel, from]),
  });

  const answer = await fetcher.handle(request);
  assert.strictEqual(answer.ok, true);
  assert.match(answer.rel, /^\.aaagent\/attachments\/\d{8}-\d{6}-band\.jpg$/);
  assert.deepStrictEqual(readResult(dir, "abc"), { ok: true, rel: answer.rel });
  assert.deepStrictEqual(fs.readFileSync(path.join(dir, answer.rel)), PNG);
  assert.deepStrictEqual(asked, ["/Users/hanul/Downloads/band.jpg"]);
  assert.deepStrictEqual(saved, [[answer.rel, "/Users/hanul/Downloads/band.jpg"]]);

  // The same file is answered once, even if the watcher fires twice.
  const added = fs.readdirSync(path.join(dir, ".aaagent", "attachments")).length;
  await fetcher.handle(request);
  assert.strictEqual(fs.readdirSync(path.join(dir, ".aaagent", "attachments")).length, added);
});

test("a read that fails is answered with the error and reported to the user", async (t) => {
  const dir = makeProject(t);
  const request = addRequest(dir, "def", { path: "/Users/hanul/x.png", created: Date.now() / 1000 });
  const failed = [];
  const fetcher = new LocalFetcher({
    project: dir,
    readLocal: async () => { throw new Error("x.png is 9.0 MB; the limit is 5 MB"); },
    onFailed: (from, message) => failed.push([from, message]),
  });

  const answer = await fetcher.handle(request);
  assert.strictEqual(answer.ok, false);
  assert.match(answer.error, /limit is 5 MB/);
  assert.deepStrictEqual(readResult(dir, "def"), { ok: false, error: "x.png is 9.0 MB; the limit is 5 MB" });
  assert.deepStrictEqual(failed, [["/Users/hanul/x.png", "x.png is 9.0 MB; the limit is 5 MB"]]);
  assert.ok(!fs.existsSync(path.join(dir, ".aaagent", "attachments")), "nothing is saved");
});

test("a request older than 60 s is ignored", async (t) => {
  const dir = makeProject(t);
  const request = addRequest(dir, "old", { path: "/Users/hanul/x.png", created: Date.now() / 1000 - 120 });
  let reads = 0;
  const fetcher = new LocalFetcher({ project: dir, readLocal: async () => { reads += 1; return PNG; } });

  assert.strictEqual(await fetcher.handle(request), null);
  assert.strictEqual(reads, 0);
  assert.ok(!fs.existsSync(path.join(dir, ".aaagent", "fetch", "old.result.json")));
});

test("scan() answers the requests written while the extension was not watching", async (t) => {
  const dir = makeProject(t);
  const now = Date.now() / 1000;
  addRequest(dir, "one", { path: "/Users/a/one.png", created: now });
  addRequest(dir, "two", { path: "/Users/a/two.png", created: now });
  const fetcher = new LocalFetcher({ project: dir, readLocal: async () => PNG });

  await fetcher.scan();
  assert.strictEqual(readResult(dir, "one").ok, true);
  assert.strictEqual(readResult(dir, "two").ok, true);
});

test("the default readLocal reads a file and expandHome keeps full paths", async (t) => {
  const dir = makeProject(t);
  const file = path.join(dir, "local.png");
  fs.writeFileSync(file, PNG);
  stub.env.remoteName = undefined;
  t.after(() => { stub.env.remoteName = undefined; });

  const fetcher = new LocalFetcher({ project: dir });
  assert.deepStrictEqual(await fetcher.readLocal(file), PNG);
  assert.strictEqual(expandHome(file), file);
  assert.strictEqual(expandHome("~/x.png"), path.join(os.homedir(), "x.png"));

  // A remote window does not know the client's home directory.
  stub.env.remoteName = "ssh-remote";
  assert.throws(() => expandHome("~/x.png"), /full path instead of ~/);
});

test("copies a Windows path of any file type into .aaagent/attachments", async (t) => {
  const dir = makeProject(t);
  const request = addRequest(dir, "win",
    { path: "c:/Users/hanul/Downloads/refresh-pages.js", created: Date.now() / 1000 });
  const text = Buffer.from("console.log('refresh');\n");
  const asked = [];
  const fetcher = new LocalFetcher({
    project: dir,
    readLocal: async (p) => { asked.push(p); return text; },
  });

  const answer = await fetcher.handle(request);
  assert.strictEqual(answer.ok, true);
  assert.match(answer.rel, /^\.aaagent\/attachments\/\d{8}-\d{6}-refresh-pages\.js$/);
  assert.deepStrictEqual(readResult(dir, "win"), { ok: true, rel: answer.rel });
  assert.deepStrictEqual(fs.readFileSync(path.join(dir, answer.rel)), text);
  assert.deepStrictEqual(asked, ["c:/Users/hanul/Downloads/refresh-pages.js"]);
});

test("a file that does not exist on the user's computer answers missing and does not warn", async (t) => {
  const dir = makeProject(t);
  const failed = [];
  const fetcher = new LocalFetcher({
    project: dir,
    readLocal: async (p) => {
      const err = new Error("no such file or directory");
      if (p.endsWith(".js")) err.code = "FileNotFound";
      else err.name = "EntryNotFound (FileSystemError)";
      throw err;
    },
    onFailed: (from, message) => failed.push([from, message]),
  });
  const win = addRequest(dir, "win",
    { path: "c:/Users/hanul/Downloads/refresh-pages.js", created: Date.now() / 1000 });
  const posix = addRequest(dir, "posix", { path: "/Users/hanul/notes.txt", created: Date.now() / 1000 });

  const missing = { ok: false, missing: true, error: "no such file or directory" };
  assert.deepStrictEqual(await fetcher.handle(win), missing);
  assert.deepStrictEqual(await fetcher.handle(posix), missing);
  assert.deepStrictEqual(readResult(dir, "win"), missing);
  assert.deepStrictEqual(readResult(dir, "posix"), missing);
  assert.deepStrictEqual(failed, [], "no warning popup for a path that was never a local file");
  assert.ok(!fs.existsSync(path.join(dir, ".aaagent", "attachments")));
});

test("a non-image over 5 MB is refused and reported", async (t) => {
  const dir = makeProject(t);
  const request = addRequest(dir, "big",
    { path: "C:\\Users\\a\\big.log", created: Date.now() / 1000 });
  const failed = [];
  const fetcher = new LocalFetcher({
    project: dir,
    readLocal: async () => Buffer.alloc(model.MAX_FILE_BYTES + 1),
    onFailed: (from, message) => failed.push([from, message]),
  });

  const answer = await fetcher.handle(request);
  assert.strictEqual(answer.ok, false);
  assert.match(answer.error, /big\.log is 5\.0 MB; the limit is 5 MB/);
  assert.deepStrictEqual(failed, [["C:\\Users\\a\\big.log", answer.error]]);
  assert.ok(!fs.existsSync(path.join(dir, ".aaagent", "attachments")), "nothing is saved");
});

test("localUri gives a Windows path a URI path", (t) => {
  t.after(() => { stub.env.remoteName = undefined; });
  stub.env.remoteName = "ssh-remote";
  assert.strictEqual(localUri("C:\\Users\\a\\b.js").path, "/C:/Users/a/b.js");
  assert.strictEqual(localUri("C:\\Users\\a\\b.js").scheme, "vscode-local");
  assert.strictEqual(localUri("c:/Users/a/b.js").path, "/c:/Users/a/b.js");
  stub.env.remoteName = undefined;
  assert.strictEqual(localUri("/Users/a/b.js").path, "/Users/a/b.js");
  assert.strictEqual(localUri("/Users/a/b.js").scheme, "file");
});
