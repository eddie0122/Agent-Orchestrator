"use strict";
// The extension side of the local file bridge.
//
// Over Remote-SSH the extension host runs on the server, but `vscode.workspace.fs` can still
// read the user's own disk through the `vscode-local` URI scheme. Claude Code's
// `UserPromptSubmit` hook (`claude/hooks/local_images.py`) writes a request into
// `<project>/.aaagent/fetch/` for a file a prompt mentions; this module reads the request,
// copies the file into `.aaagent/attachments/` and writes the result back. The hook waits for
// the answer and tells Claude where the copy is.

const fs = require("fs");
const os = require("os");
const path = require("path");
const vscode = require("vscode");

const { saveFile } = require("./attach");
const model = require("./model");

const REQUEST_SUFFIX = ".request.json";
const STALE_SECONDS = 60;

/** The client's disk in a remote window, this machine's own disk in a local one. */
function localUri(p) {
  return vscode.Uri.from({ scheme: vscode.env.remoteName ? "vscode-local" : "file",
    path: model.localUriPath(p) });
}

/** `~/x` with the user's home. A remote window cannot know the client's home directory. */
function expandHome(p) {
  const text = String(p || "");
  if (text !== "~" && !text.startsWith("~/")) return text;
  const home = vscode.env.remoteName ? null : os.homedir();
  if (!home) throw new Error("use the full path instead of ~");
  return home + text.slice(1);
}

/** Read a file from the user's computer. Injected in tests. */
async function readLocalFile(p) {
  const target = expandHome(p);
  const uri = localUri(target);
  const name = path.posix.basename(model.localUriPath(target));
  const stat = await vscode.workspace.fs.stat(uri);
  if (stat.size > model.MAX_FILE_BYTES) {
    const mb = (stat.size / (1024 * 1024)).toFixed(1);
    throw new Error(`${name} is ${mb} MB; the limit is ${model.MAX_FILE_BYTES / (1024 * 1024)} MB`);
  }
  return Buffer.from(await vscode.workspace.fs.readFile(uri));
}

/** Whether a read failed because there is no such file on the user's computer. */
function isMissing(err) {
  if (!err) return false;
  const code = String(err.code || "");
  if (code === "FileNotFound" || code === "ENOENT") return true;
  return String(err.name || "").includes("EntryNotFound");
}

function writeAtomic(target, value) {
  const tmp = `${target}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(value));
  fs.renameSync(tmp, target);
}

/** Answers the requests a prompt hook leaves in `<project>/.aaagent/fetch/`. */
class LocalFetcher {
  constructor({ project, readLocal, onSaved, onFailed }) {
    this.project = project;
    this.readLocal = readLocal || readLocalFile;
    this.onSaved = onSaved;
    this.onFailed = onFailed;
    this.handled = new Set();
  }

  dir() {
    return path.join(this.project, ".aaagent", "fetch");
  }

  /** Handle one `*.request.json`. Returns the answer, or null for a stale request. */
  async handle(requestFile) {
    const name = path.basename(requestFile);
    if (this.handled.has(name)) return null;
    this.handled.add(name);
    const stem = name.endsWith(REQUEST_SUFFIX) ? name.slice(0, -REQUEST_SUFFIX.length) : name;

    let requested = "";
    let answer;
    try {
      const request = JSON.parse(fs.readFileSync(requestFile, "utf8"));
      requested = String((request && request.path) || "");
      const created = Number(request && request.created);
      if (created > 0 && Date.now() / 1000 - created > STALE_SECONDS) return null;
      if (!requested) throw new Error("the request has no path");
      const data = await this.readLocal(requested);
      const saved = saveFile(this.project, path.posix.basename(model.localUriPath(requested)), data);
      answer = { ok: true, rel: saved.rel };
    } catch (err) {
      const error = (err && err.message) || String(err);
      // A path that is not a local file at all: the hook stays quiet about it.
      answer = isMissing(err) ? { ok: false, missing: true, error } : { ok: false, error };
    }

    try {
      writeAtomic(path.join(this.dir(), `${stem}.result.json`), answer);
    } catch (err) {
      // The hook gives up and tells Claude to ask for an attachment instead.
    }
    if (answer.ok) {
      if (this.onSaved) this.onSaved(answer.rel, requested);
    } else if (!answer.missing && this.onFailed) {
      this.onFailed(requested, answer.error);
    }
    return answer;
  }

  /** Handle requests written before the watcher existed (e.g. the extension starting late). */
  async scan() {
    let names;
    try {
      names = fs.readdirSync(this.dir());
    } catch (err) {
      return;
    }
    for (const name of names) {
      if (name.endsWith(REQUEST_SUFFIX)) await this.handle(path.join(this.dir(), name));
    }
  }
}

module.exports = { LocalFetcher, localUri, expandHome, readLocalFile };
