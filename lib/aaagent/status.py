"""Project status as data, and the pure view logic the terminal dashboard draws from.

`status_data` is what `aaagent status --json` prints (the VSCode extension reads it too).
The helpers below mirror vscode/src/model.js so both UIs describe a task the same way.
Nothing here touches the terminal, so it can be unit-tested directly.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, tasks, worker
from .config import Config
from .tasks import FINAL_STATUSES, FlowError

PHASE_VERB = {"implement": "implementing", "fix": "fixing", "commit": "committing"}


def status_data(project: Path, cfg: Config) -> dict:
    """Machine-readable status, used by the VSCode extension and the dashboard."""
    workers = []
    for name in cfg.worker_names():
        pid = worker.worker_pid(project, name)
        job = worker.running_job(project, name)
        queued = len(worker.pending_jobs(project, name)) if worker.queue_dir(project, name).is_dir() else 0
        workers.append({
            "name": name, "model": cfg.worker_model(name), "pid": pid,
            "state": "running" if job else ("idle" if pid else "stopped"),
            "job": {k: job[k] for k in ("task", "phase", "round", "created")} if job else None,
            "queued": queued, "log": str(worker.log_file(project, name)),
        })
    out_tasks = []
    for task in tasks.all_tasks(project):
        state = task.load()
        status = state.get("status", "planning")
        nxt, blocked = None, None
        try:
            step = tasks.next_step(task, cfg)
            nxt = {"phase": step.phase, "round": step.round, "worker": step.worker}
        except FlowError as exc:
            blocked = str(exc)
        reviews = task.reviews()
        files = [{"name": "plan.md", "path": str(task.plan), "kind": "plan"}]
        for n in sorted(set(task.reports()) | set(reviews)):
            if task.report(n).is_file():
                files.append({"name": task.report(n).name, "path": str(task.report(n)), "kind": "report",
                              "status": tasks.frontmatter(task.report(n)).get("status")})
            if task.review(n).is_file():
                files.append({"name": task.review(n).name, "path": str(task.review(n)), "kind": "review",
                              "verdict": task.verdict(n)})
        latest_review = max(reviews, default=0)
        attention = None
        if status == "failed":
            attention = "failed"
        elif blocked and "max_review_rounds" in blocked:
            attention = "review limit reached"
        job = state.get("current_job")
        out_tasks.append({
            "id": task.id, "number": int(task.id.split("-", 1)[0]),
            "title": task.id.split("-", 1)[1].replace("-", " ") if "-" in task.id else task.id,
            "status": status, "round": task.latest_report(), "worker": state.get("worker"),
            "verdict": task.verdict(latest_review) if latest_review else None,
            "plan_written": tasks.plan_is_written(task),
            "job": {k: job.get(k) for k in ("phase", "round", "worker")} if job else None,
            "next": nxt, "blocked": blocked, "attention": attention,
            "commit": state.get("commit"), "created": state.get("created"),
            "updated": task.state_file.stat().st_mtime if task.state_file.exists() else None,
            "path": str(task.path), "files": files,
        })
    return {"version": __version__, "project": str(project), "workers": workers,
            "tasks": out_tasks, "active_task": tasks.active_task(project)}


# -- what to say about a task or worker ----------------------------------------

def is_final(task: dict) -> bool:
    return task.get("status") in FINAL_STATUSES


def is_running(task: dict) -> bool:
    return task.get("status") in ("running", "retrying")


def task_label(task: dict) -> str:
    return f"{task['number']:03d} {task['title']}"


def visible_tasks(all_tasks: list[dict], keep_finished: int = 10) -> list[dict]:
    """All unfinished tasks plus the most recent finished ones, newest first."""
    out, finished = [], 0
    for task in sorted(all_tasks or [], key=lambda t: -t["number"]):
        if is_final(task):
            finished += 1
            if finished > keep_finished:
                continue
        out.append(task)
    return out


def task_summary(task: dict) -> str:
    """What a task is doing right now, in a few words."""
    status = task.get("status")
    if is_running(task):
        job = task.get("job") or {}
        return f"{PHASE_VERB.get(job.get('phase'), 'working')} · {job.get('worker') or task.get('worker') or '?'}"
    if task.get("attention"):
        return f"{task['attention']}: needs you"
    if status == "committed":
        return f"committed {task['commit'][:7]}" if task.get("commit") else "committed"
    if status == "abandoned":
        return "abandoned"
    if status == "planning":
        return "plan ready" if task.get("plan_written") else "planning"
    if status == "awaiting_review":
        return f"in review (round {task.get('round')})"
    nxt = task.get("next")
    if nxt:
        if nxt["phase"] == "commit":
            return "approved, ready to commit"
        if nxt["phase"] == "fix":
            return f"changes requested (round {task.get('round')})"
        return f"ready: {nxt['phase']}"
    return str(status)


def task_style(task: dict) -> str:
    if is_running(task):
        return "running"
    if task.get("attention"):
        return "warn"
    if task.get("status") == "committed":
        return "ok"
    if task.get("status") == "abandoned":
        return "dim"
    return "normal"


def worker_summary(w: dict) -> str:
    job = w.get("job")
    if w.get("state") == "running" and job:
        text = f"{PHASE_VERB.get(job['phase'], job['phase'])} {job['task'].split('-')[0]} r{job['round']}"
    elif w.get("state") == "idle":
        text = "waiting for a plan"
    else:
        text = "stopped"
    if w.get("queued"):
        text += f" · {w['queued']} queued"
    return text


def file_summary(task: dict, f: dict) -> str:
    if f["kind"] == "plan":
        return "" if task.get("plan_written") else "being written"
    if f["kind"] == "report":
        return f.get("status") or ""
    if f["kind"] == "review":
        return (f.get("verdict") or "no verdict").replace("_", " ")
    return ""


def is_busy(data: dict | None) -> bool:
    """Whether to poll quickly: something is (or may soon be) moving."""
    if not data:
        return False
    return (any(is_running(t) for t in data.get("tasks") or [])
            or any(w.get("state") == "running" or w.get("queued") for w in data.get("workers") or []))


def transitions(prev: dict | None, new: dict | None) -> list[dict]:
    """Events worth telling the user about between two snapshots. The first snapshot is silent."""
    if not prev or not new:
        return []
    before = {t["id"]: t for t in prev.get("tasks") or []}
    out = []
    for t in new.get("tasks") or []:
        old = before.get(t["id"])
        if not old:
            continue
        label = task_label(t)
        if t["status"] == "committed" and old["status"] != "committed":
            commit = f" ({t['commit'][:7]})" if t.get("commit") else ""
            out.append({"level": "info", "task": t["id"], "message": f"{label} committed{commit}"})
        elif t.get("attention") and t["attention"] != old.get("attention"):
            why = "stopped after both workers failed" if t["attention"] == "failed" else "reached the review round limit"
            out.append({"level": "warning", "task": t["id"],
                        "message": f"{label} {why}; Opus is waiting for you in the chat"})
        elif t["status"] == "awaiting_review" and old["status"] != "awaiting_review":
            out.append({"level": "status", "task": t["id"],
                        "message": f"{label}: round {t.get('round')} finished; Opus is reviewing"})
    return out


# -- rows the dashboard draws ---------------------------------------------------

@dataclass
class Row:
    id: str
    kind: str                 # header | worker | task | file | note
    text: str
    detail: str = ""
    style: str = "normal"     # normal | header | running | ok | warn | dim
    indent: int = 0
    ref: dict = field(default_factory=dict)
    task: str | None = None   # task id for task and file rows

    @property
    def selectable(self) -> bool:
        return self.kind in ("worker", "task", "file")


def rows(data: dict | None, expanded: set[str] | frozenset = frozenset(), keep_finished: int = 10) -> list[Row]:
    """The flat list shown in the dashboard: workers, then tasks with the files of expanded ones."""
    if not data:
        return []
    out = [Row("header:workers", "header", "WORKERS", style="header")]
    for w in data.get("workers") or []:
        style = "running" if w.get("state") == "running" else ("dim" if w.get("state") == "stopped" else "normal")
        out.append(Row(f"worker:{w['name']}", "worker", w["name"], worker_summary(w), style, 1, w))
    out.append(Row("header:tasks", "header", "TASKS", style="header"))
    shown = visible_tasks(data.get("tasks") or [], keep_finished)
    if not shown:
        out.append(Row("note:empty", "note", "none yet: ask Opus for a change in the chat", style="dim", indent=1))
    for t in shown:
        mark = "▾" if t["id"] in expanded else "▸"
        out.append(Row(f"task:{t['id']}", "task", f"{mark} {task_label(t)}", task_summary(t),
                       task_style(t), 1, t, t["id"]))
        if t["id"] in expanded:
            for f in t.get("files") or []:
                out.append(Row(f"file:{t['id']}:{f['name']}", "file", f["name"], file_summary(t, f),
                               "dim" if f["kind"] == "plan" and not t.get("plan_written") else "normal",
                               3, f, t["id"]))
    return out


# -- text that fits a terminal cell grid ----------------------------------------

def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(text: str) -> int:
    return sum(char_width(ch) for ch in text)


def clip(text: str, width: int) -> str:
    """Cut text to at most `width` terminal cells, ending in … when something was dropped."""
    if width <= 0:
        return ""
    if text_width(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        w = char_width(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def wrap(text: str, width: int) -> list[str]:
    """Break one line into pieces of at most `width` cells, at a space when there is one nearby."""
    if width <= 0:
        return [""]
    pieces = []
    while text_width(text) > width:
        used, cut = 0, 0
        for i, ch in enumerate(text):
            used += char_width(ch)
            if used > width:
                break
            cut = i + 1
        cut = max(cut, 1)
        space = text.rfind(" ", cut // 2, cut + 1)
        if space > 0:
            pieces.append(text[:space])
            text = text[space + 1:]
        else:
            pieces.append(text[:cut])
            text = text[cut:]
    pieces.append(text)
    return pieces


def format_row(row: Row, width: int) -> str:
    """One row as plain text: name on the left, what it is doing on the right."""
    left = " " * row.indent + row.text
    if not row.detail:
        return clip(left, width)
    detail = clip(row.detail, max(8, width // 2))
    room = width - text_width(detail) - 2
    if room < row.indent + 6:
        return clip(left, width)
    left = clip(left, room)
    return left + " " * (width - text_width(left) - text_width(detail)) + detail
