"use strict";
// Finds and runs the `aaagent` command.

const cp = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

class CliError extends Error {
  constructor(message, { code, stderr, kind } = {}) {
    super(message);
    this.code = code;
    this.stderr = stderr;
    this.kind = kind; // "notFound" | "noRepo" | "failed"
  }
}

function isExecutable(p) {
  try {
    fs.accessSync(p, fs.constants.X_OK);
    return fs.statSync(p).isFile();
  } catch {
    return false;
  }
}

/** Resolve the aaagent binary: explicit setting, ~/.local/bin, then PATH. */
function findAaagent(configured) {
  const home = os.homedir();
  if (configured) {
    const p = configured.startsWith("~") ? path.join(home, configured.slice(1)) : configured;
    return isExecutable(p) ? p : null;
  }
  const local = path.join(home, ".local", "bin", "aaagent");
  if (isExecutable(local)) return local;
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    if (dir && isExecutable(path.join(dir, "aaagent"))) return path.join(dir, "aaagent");
  }
  return null;
}

function cleanEnv() {
  const env = { ...process.env };
  delete env.AAAGENT_PROJECT; // let aaagent find the project from cwd
  return env;
}

function run(bin, args, cwd, { timeout = 20000 } = {}) {
  return new Promise((resolve, reject) => {
    cp.execFile(bin, args, { cwd, env: cleanEnv(), timeout, maxBuffer: 32 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) {
          const msg = (stderr || err.message || "").trim();
          const kind = /not inside a git repository/.test(msg) ? "noRepo" : "failed";
          reject(new CliError(msg || `aaagent ${args.join(" ")} failed`, { code: err.code, stderr, kind }));
        } else {
          resolve(stdout);
        }
      });
  });
}

class Cli {
  constructor(getConfiguredPath) {
    this.getConfiguredPath = getConfiguredPath;
  }

  get bin() {
    return findAaagent(this.getConfiguredPath());
  }

  require() {
    const bin = this.bin;
    if (!bin) throw new CliError("aaagent command not found", { kind: "notFound" });
    return bin;
  }

  async status(cwd) {
    const out = await run(this.require(), ["status", "--json"], cwd);
    return JSON.parse(out);
  }

  diff(cwd, task) {
    return run(this.require(), ["diff", task], cwd, { timeout: 60000 });
  }

  exec(cwd, args, opts) {
    return run(this.require(), args, cwd, opts);
  }
}

module.exports = { Cli, CliError, findAaagent };
