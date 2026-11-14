"use strict";
// Pure logic: turns `aaagent status --json` into what the UI shows. No `vscode` import,
// so it can be unit-tested with plain node.

const FINAL = new Set(["committed", "abandoned"]);

const PHASE_VERB = { implement: "implementing", fix: "fixing", commit: "committing" };

function isFinal(task) {
  return FINAL.has(task.status);
}

/** Tasks to show: all unfinished ones plus the most recent finished ones, newest first. */
function visibleTasks(tasks, keepFinished = 10) {
  const sorted = [...(tasks || [])].sort((a, b) => b.number - a.number);
  let finished = 0;
  return sorted.filter((t) => {
    if (!isFinal(t)) return true;
    finished += 1;
    return finished <= keepFinished;
  });
}

function short(text, max = 80) {
  const s = String(text || "").replace(/\s+/g, " ").trim();
  return s.length <= max ? s : s.slice(0, max - 1) + "…";
}

/** What a task is doing right now, in a few words. */
function taskSummary(task) {
  if (task.status === "running" || task.status === "retrying") {
    const job = task.job || {};
    const verb = PHASE_VERB[job.phase] || "working";
    return `${verb} · ${job.worker || task.worker || "?"}`;
  }
  if (task.attention === "failed") return "failed: needs you";
  if (task.attention) return `${task.attention}: needs you`;
  switch (task.status) {
    case "committed":
      return task.commit ? `committed ${task.commit.slice(0, 7)}` : "committed";
    case "abandoned":
      return "abandoned";
    case "planning":
      return task.plan_written ? "plan ready" : "planning";
    case "awaiting_review":
      return `in review (round ${task.round})`;
    default:
      break;
  }
  if (task.next) {
    if (task.next.phase === "commit") return "approved, ready to commit";
    if (task.next.phase === "fix") return `changes requested (round ${task.round})`;
    return `ready: ${task.next.phase}`;
  }
  return task.status;
}

/** Codicon id and optional theme colour for a task. */
function taskIcon(task) {
  if (task.status === "running" || task.status === "retrying") return { id: "sync~spin" };
  if (task.attention) return { id: "warning", color: "problemsWarningIcon.foreground" };
  if (task.status === "committed") return { id: "pass-filled", color: "testing.iconPassed" };
  if (task.status === "abandoned") return { id: "circle-slash" };
  if (task.status === "awaiting_review") return { id: "eye" };
  if (task.next && task.next.phase === "commit") return { id: "check" };
  if (task.next && task.next.phase === "fix") return { id: "edit" };
  return { id: "note" };
}

/** A timestamp in milliseconds from an ISO string (Python writes `+0100`; JS wants `+01:00`). */
function parseWhen(iso) {
  if (iso instanceof Date) return iso.getTime();
  if (typeof iso === "number") return iso;
  const s = String(iso == null ? "" : iso).trim();
  if (!s) return NaN;
  return Date.parse(s.replace(/([+-]\d{2})(\d{2})$/, "$1:$2"));
}

/** "just now", "5 min ago", "3 h ago", "2 days ago", else `YYYY-MM-DD`. Invalid → "". */
function ago(iso, now = Date.now()) {
  const when = parseWhen(iso);
  if (!Number.isFinite(when)) return "";
  const secs = Math.max(0, now - when) / 1000;
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days} day${days === 1 ? "" : "s"} ago`;
  const d = new Date(when);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** How many tasks a session created, as `"1 task"`, `"3 tasks"` or `""`. */
function taskCount(tasks) {
  const n = (tasks || []).length;
  if (!n) return "";
  return `${n} ${n === 1 ? "task" : "tasks"}`;
}

/** A chat session: an Opus conversation and the tasks it created. */
function sessionItem(session, { open = false } = {}) {
  const ids = session.tasks || [];
  const description = [open ? "open" : ago(session.last_active), taskCount(ids)];
  if (!session.available) description.push("gone");
  const title = session.title || "(no messages yet)";
  const lines = [title, `id: ${session.id}`, `started: ${session.started || ""}`,
    `last active: ${session.last_active || ""}`];
  if (ids.length) lines.push(`tasks: ${ids.join(", ")}`);
  return {
    id: `session:${session.id}`,
    label: title,
    description: description.filter(Boolean).join(" · "),
    tooltip: lines.join("\n"),
    icon: open ? { id: "comment-discussion", color: "testing.iconPassed" }
      : session.available ? { id: "history" } : { id: "circle-slash" },
    contextValue: open ? "session.open" : session.available ? "session.available" : "session.gone",
  };
}

/** Quick-pick entries for Restore Previous Session: every reopenable session but the open one. */
function sessionChoices(sessions, openId) {
  return (sessions || [])
    .filter((s) => s.available && s.id !== openId)
    .map((s) => ({
      label: s.title || "(no messages yet)",
      description: [ago(s.last_active), taskCount(s.tasks)].filter(Boolean).join(" · "),
      detail: s.short || String(s.id).slice(0, 8),
      session: s,
    }));
}

function taskItem(task) {
  const lines = [`${task.id}`, `status: ${task.status}`];
  if (task.session) lines.push(`session: ${String(task.session).slice(0, 8)}`);
  if (task.worker) lines.push(`worker: ${task.worker}`);
  if (task.verdict) lines.push(`last review: ${task.verdict}`);
  if (task.next) lines.push(`next: ${task.next.phase} round ${task.next.round} → ${task.next.worker}`);
  else if (task.blocked && !isFinal(task)) lines.push(`waiting: ${task.blocked}`);
  if (task.commit) lines.push(`commit: ${task.commit}`);
  return {
    id: `task:${task.id}`,
    label: `${String(task.number).padStart(3, "0")} ${task.title}`,
    description: taskSummary(task),
    tooltip: lines.join("\n"),
    icon: taskIcon(task),
    contextValue: isFinal(task) ? "task.final" : "task.active",
  };
}

function fileItem(task, file) {
  let description = "";
  let icon = { id: "file" };
  if (file.kind === "plan") {
    icon = { id: "list-ordered" };
    if (!task.plan_written) description = "being written";
  } else if (file.kind === "report") {
    icon = { id: "output" };
    description = file.status || "";
  } else if (file.kind === "review") {
    const approved = file.verdict === "approved";
    icon = approved ? { id: "pass", color: "testing.iconPassed" } : { id: "comment-discussion" };
    description = file.verdict ? file.verdict.replace("_", " ") : "no verdict";
  }
  return { id: `file:${task.id}:${file.name}`, label: file.name, description, icon, path: file.path,
    contextValue: `file.${file.kind}` };
}

/** `aaagent model --json` → the quick-pick list of things whose model can be changed. */
function modelTargets(info) {
  const planner = (info && info.planner) || {};
  const workers = (info && info.workers) || {};
  const out = [{ label: "Planner (Claude)", description: planner.model, target: "planner" }];
  for (const [name, w] of Object.entries(workers)) {
    out.push({ label: `Worker: ${name}`, description: w.model, target: name });
  }
  return out;
}

/** Model ids without the provider prefix: `opencode-go/kimi-k3` → `kimi-k3`. */
function shortModel(id) {
  const s = String(id || "");
  const slash = s.lastIndexOf("/");
  return slash >= 0 ? s.slice(slash + 1) : s;
}

/** Quick-pick entries for a target's models: a "type a name" entry, then current first. */
function modelChoices(info, target) {
  const planner = (info && info.planner) || {};
  const workers = (info && info.workers) || {};
  const current = target === "planner" ? planner.model : (workers[target] || {}).model;
  const list = target === "planner" ? (planner.choices || []) : ((info && info.worker_choices) || []);
  const names = [];
  for (const name of list) if (name && !names.includes(name)) names.push(name);
  if (current) {
    const i = names.indexOf(current);
    if (i >= 0) names.splice(i, 1);
    names.unshift(current);
  }
  const entries = names.map((name) => (name === current
    ? { label: name, model: name, description: "current" }
    : { label: name, model: name }));
  return [{ label: "$(edit) Type a model name…", typed: true }, ...entries];
}

/** `aaagent thinking --json` → the quick-pick list of things whose thinking level can be changed. */
function thinkingTargets(info) {
  const planner = (info && info.planner) || {};
  const workers = (info && info.workers) || {};
  const out = [{ label: "Planner (Claude)", description: `${planner.thinking || "default"} · ${planner.model}`,
    target: "planner" }];
  for (const [name, w] of Object.entries(workers)) {
    out.push({ label: `Worker: ${name}`, description: `${w.thinking || "default"} · ${w.model}`, target: name });
  }
  return out;
}

/** Quick-pick entries for a target's thinking levels: Default, then the choices. */
function thinkingChoices(info, target) {
  const planner = (info && info.planner) || {};
  const workers = (info && info.workers) || {};
  const entry = target === "planner" ? planner : (workers[target] || {});
  const current = entry.thinking || "";
  const entries = (entry.choices || [])
    .filter((level) => level)
    .map((level) => (level === current
      ? { label: level, description: "current", level }
      : { label: level, level }));
  return [{ label: "Default", description: "the model's own setting", level: "default" }, ...entries];
}

function workerItem(w) {
  let description;
  let icon;
  if (w.state === "running" && w.job) {
    description = `${PHASE_VERB[w.job.phase] || w.job.phase} ${w.job.task.split("-")[0]} r${w.job.round}`;
    icon = { id: "sync~spin" };
  } else if (w.state === "idle") {
    description = "waiting for a plan";
    icon = { id: "circle-filled", color: "testing.iconPassed" };
  } else {
    description = "stopped";
    icon = { id: "circle-outline" };
  }
  if (description !== "stopped" && w.model) {
    description += ` · ${shortModel(w.model)}`;
    if (w.thinking) description += ` · ${w.thinking}`;
  }
  if (w.queued) description += ` · ${w.queued} queued`;
  const thinking = w.thinking ? `thinking: ${w.thinking}\n` : "";
  return {
    id: `worker:${w.name}`,
    label: w.name,
    description,
    tooltip: `${w.name}: ${w.model}\n${thinking}${w.pid ? `pid ${w.pid}` : "not running"}`,
    icon,
    contextValue: `worker.${w.state}`,
  };
}

/** Status bar text: the most important thing going on. */
function statusBar(data) {
  if (!data) return { text: "$(hubot) aaagent", tooltip: "aaagent: not available here" };
  const tasks = data.tasks || [];
  const running = tasks.find((t) => t.status === "running" || t.status === "retrying");
  const needs = tasks.find((t) => t.attention && !isFinal(t));
  const active = tasks.find((t) => t.id === data.active_task);
  let text = "$(hubot) aaagent";
  if (running) {
    text = `$(sync~spin) aaagent: ${String(running.number).padStart(3, "0")} ${taskSummary(running)}`;
  } else if (needs) {
    text = `$(warning) aaagent: ${String(needs.number).padStart(3, "0")} needs you`;
  } else if (active) {
    text = `$(hubot) aaagent: ${String(active.number).padStart(3, "0")} ${taskSummary(active)}`;
  }
  const up = (data.workers || []).filter((w) => w.state !== "stopped").map((w) => w.name);
  const planner = data.planner && data.planner.model ? `planner: ${data.planner.model}\n` : "";
  const tooltip = `${planner}aaagent: ${up.length ? `workers up: ${up.join(", ")}` : "workers stopped"}\nClick to open the aaagent view`;
  return { text, tooltip };
}

/** Notifications for changes between two status snapshots. The first snapshot is silent. */
function transitions(prev, next) {
  if (!prev || !next) return [];
  const before = new Map((prev.tasks || []).map((t) => [t.id, t]));
  const out = [];
  for (const t of next.tasks || []) {
    const old = before.get(t.id);
    if (!old) continue;
    const label = `${String(t.number).padStart(3, "0")} ${t.title}`;
    if (t.status === "committed" && old.status !== "committed") {
      out.push({ level: "info", task: t.id, message: `aaagent: ${label} committed${t.commit ? ` (${t.commit.slice(0, 7)})` : ""}.` });
    } else if (t.attention && t.attention !== old.attention) {
      const why = t.attention === "failed" ? "stopped after both workers failed" : "reached the review round limit";
      out.push({ level: "warning", task: t.id, message: `aaagent: ${label} ${why}. Opus is waiting for your decision in the chat.` });
    } else if (t.status === "awaiting_review" && old.status !== "awaiting_review") {
      out.push({ level: "status", task: t.id, message: `aaagent: ${label}: worker finished round ${t.round}; Opus is reviewing.` });
    }
  }
  return out;
}

/** Whether to poll quickly: something is (or may soon be) moving. */
function isBusy(data) {
  if (!data) return false;
  return (data.tasks || []).some((t) => t.status === "running" || t.status === "retrying") ||
    (data.workers || []).some((w) => w.state === "running" || w.queued);
}

/** Wrap text so a terminal TUI receives it as one paste, not keystrokes. */
function bracketedPaste(text) {
  return `\x1b[200~${text}\x1b[201~`;
}

/** Claude Code @-mention for a file selection (lines are 1-based). */
function mention(relPath, startLine, endLine) {
  const p = relPath.includes(" ") ? `"${relPath}"` : relPath;
  if (!startLine) return `@${p} `;
  if (!endLine || endLine === startLine) return `@${p}#L${startLine} `;
  return `@${p}#L${startLine}-${endLine} `;
}

// -- images (must match lib/aaagent/attach.py) ---------------------------------

const IMAGE_EXTS = ["png", "jpg", "jpeg", "gif", "webp"];
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;

function startsWithBytes(buf, bytes, offset = 0) {
  if (!buf || buf.length < offset + bytes.length) return false;
  for (let i = 0; i < bytes.length; i++) if (buf[offset + i] !== bytes[i]) return false;
  return true;
}

const asciiBytes = (text) => Array.from(text, (c) => c.charCodeAt(0));
const PNG_SIG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

/** The image type of a Buffer/Uint8Array from its magic bytes, or null. `attach.py:sniff`. */
function sniffImage(buf) {
  if (!buf || !buf.length) return null;
  if (startsWithBytes(buf, PNG_SIG)) return "png";
  if (startsWithBytes(buf, [0xff, 0xd8, 0xff])) return "jpg";
  if (startsWithBytes(buf, asciiBytes("GIF87a")) || startsWithBytes(buf, asciiBytes("GIF89a"))) return "gif";
  if (startsWithBytes(buf, asciiBytes("RIFF")) && startsWithBytes(buf, asciiBytes("WEBP"), 8)) return "webp";
  return null;
}

const pad2 = (n) => String(n).padStart(2, "0");

/** The basename split the way Python's `Path` does: stem and the last suffix. */
function splitName(name) {
  const base = String(name || "").split(/[\\/]/).pop();
  const dot = base.lastIndexOf(".");
  if (dot <= 0 || base.endsWith(".")) return { stem: base, ext: "" };
  return { stem: base.slice(0, dot), ext: base.slice(dot) };
}

/** `<YYYYmmdd-HHMMSS>-<stem>.<ext>`, the same name `attach.py:safe_name` makes. */
function attachmentName(originalName, kind, date) {
  const d = date || new Date();
  const stamp = `${d.getFullYear()}${pad2(d.getMonth() + 1)}${pad2(d.getDate())}-` +
    `${pad2(d.getHours())}${pad2(d.getMinutes())}${pad2(d.getSeconds())}`;
  const parts = splitName(originalName);
  const stem = parts.stem.replace(/[^A-Za-z0-9._-]/g, "-").replace(/-+/g, "-").trim()
    .replace(/^-+|-+$/g, "").slice(0, 40).replace(/-+$/, "") || "image";
  const ext = parts.ext.replace(/^\./, "").toLowerCase();
  return `${stamp}-${stem}.${IMAGE_EXTS.includes(ext) ? ext : kind}`;
}

/** Whether the path has one of the image extensions we accept. */
function isImagePath(p) {
  const base = String(p || "").split(/[\\/]/).pop();
  const dot = base.lastIndexOf(".");
  if (dot <= 0) return false;
  return IMAGE_EXTS.includes(base.slice(dot + 1).toLowerCase());
}

module.exports = {
  visibleTasks, taskSummary, taskIcon, taskItem, fileItem, workerItem, statusBar, transitions,
  isBusy, bracketedPaste, mention, short, isFinal, modelTargets, modelChoices,
  thinkingTargets, thinkingChoices, ago, sessionItem, sessionChoices,
  IMAGE_EXTS, MAX_IMAGE_BYTES, sniffImage, attachmentName, isImagePath,
};
