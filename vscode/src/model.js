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

function taskItem(task) {
  const lines = [`${task.id}`, `status: ${task.status}`];
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
  if (w.queued) description += ` · ${w.queued} queued`;
  return {
    id: `worker:${w.name}`,
    label: w.name,
    description,
    tooltip: `${w.name}: ${w.model}\n${w.pid ? `pid ${w.pid}` : "not running"}`,
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
  const tooltip = `aaagent: ${up.length ? `workers up: ${up.join(", ")}` : "workers stopped"}\nClick to open the aaagent view`;
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

module.exports = {
  visibleTasks, taskSummary, taskIcon, taskItem, fileItem, workerItem, statusBar, transitions,
  isBusy, bracketedPaste, mention, short, isFinal,
};
