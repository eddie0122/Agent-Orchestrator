"""Task folders, state.json, and the rules that decide the next phase.

Layout inside a project:
    .aaagent/tasks/NNN-slug/plan.md        written by the orchestrator
    .aaagent/tasks/NNN-slug/report-N.md    written by the worker (round N)
    .aaagent/tasks/NNN-slug/review-N.md    written by the reviewer (the planner, or the
                                           dedicated reviewer when `[reviewer]` sets a model)
    .aaagent/tasks/NNN-slug/state.json     bookkeeping
    .aaagent/tasks/NNN-slug/logs/          raw worker output
"""

from __future__ import annotations

import fcntl
import fnmatch
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import gitutil
from .config import ROOT, Config

AGENT_DIR = ".aaagent"
TEMPLATES = ROOT / "templates"
FINAL_STATUSES = {"committed", "abandoned"}
GLOB_CHARS = "*?["


class FlowError(Exception):
    """A dispatch that is not allowed in the current task state."""


def agent_dir(project: Path) -> Path:
    return project / AGENT_DIR


def tasks_dir(project: Path) -> Path:
    return agent_dir(project) / "tasks"


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40].rstrip("-") or "task"


def frontmatter(path: Path) -> dict[str, str]:
    """Parse simple `key: value` frontmatter between leading --- lines."""
    if not path.is_file():
        return {}
    lines = path.read_text(errors="replace").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, value = line.split(":", 1)
            value = value.split("#", 1)[0].strip().strip("\"'")
            out[key.strip().lower()] = value
    return out


def numbered(task_path: Path, prefix: str) -> dict[int, Path]:
    found = {}
    for p in task_path.glob(f"{prefix}-*.md"):
        m = re.fullmatch(rf"{prefix}-(\d+)\.md", p.name)
        if m:
            found[int(m.group(1))] = p
    return found


# -- the files a plan may change -----------------------------------------------

def _clean_entry(entry: str) -> str:
    entry = entry.strip()
    while entry.startswith("./"):
        entry = entry[2:]
    return entry


def scope_problem(entry: str) -> str | None:
    """Why a `files:` entry is not allowed, or None when it is fine."""
    if not entry:
        return "is empty"
    if entry.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", entry):
        return "is absolute; use a path relative to the project root"
    normalised = entry.replace("\\", "/")
    if ".." in normalised.split("/"):
        return "contains '..'"
    if normalised == AGENT_DIR or normalised.startswith(AGENT_DIR + "/"):
        return f"is inside {AGENT_DIR}/"
    return None


def git_pathspec(entry: str) -> str:
    """A `files:` entry as a git pathspec (globs need the `:(glob)` magic)."""
    return f":(glob){entry}" if any(ch in entry for ch in GLOB_CHARS) else entry


def in_scope(path: str, scope: list[str] | None) -> bool:
    """Whether a repo-relative path is covered by a `files:` list; None covers everything."""
    if scope is None:
        return True
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    for entry in scope:
        if any(ch in entry for ch in GLOB_CHARS):
            if fnmatch.fnmatchcase(path, entry):
                return True
            if entry.endswith("/"):  # a glob directory: match any parent folder
                parts = path.split("/")
                for i in range(1, len(parts)):
                    if fnmatch.fnmatchcase("/".join(parts[:i]) + "/", entry):
                        return True
            continue
        if entry.endswith("/"):
            base = entry.rstrip("/")
            if path == base or path.startswith(base + "/"):
                return True
            continue
        if path == entry:
            return True
    return False


def _inside(path: str, directory: str) -> bool:
    base = directory.rstrip("/")
    return path == base or path.startswith(base + "/")


def _glob_prefix(entry: str) -> str:
    for i, ch in enumerate(entry):
        if ch in GLOB_CHARS:
            return entry[:i]
    return entry


def entries_overlap(a: str, b: str) -> bool:
    """Conservative check: two `files:` entries that may name the same path."""
    if a == b:
        return True
    if a.endswith("/") and _inside(b, a):
        return True
    if b.endswith("/") and _inside(a, b):
        return True
    a_glob = any(ch in a for ch in GLOB_CHARS)
    b_glob = any(ch in b for ch in GLOB_CHARS)
    if a_glob and fnmatch.fnmatchcase(b, a):
        return True
    if b_glob and fnmatch.fnmatchcase(a, b):
        return True
    if a_glob and b_glob:
        prefix_a, prefix_b = _glob_prefix(a), _glob_prefix(b)
        if not (prefix_a.startswith(prefix_b) or prefix_b.startswith(prefix_a)):
            return False
        return True
    if a_glob or b_glob:
        return True
    return False


def scopes_overlap(a: list[str] | None, b: list[str] | None) -> list[str]:
    """Human-readable pairs of overlapping `files:` entries; empty means no overlap.

    None (an undeclared task) overlaps everything.
    """
    if a is None or b is None:
        return ["(no files: list: the whole working tree)"]
    pairs = []
    for left in a:
        for right in b:
            if entries_overlap(left, right):
                pairs.append(f"{left} overlaps {right}")
    return pairs


@dataclass
class Task:
    project: Path
    path: Path

    @property
    def id(self) -> str:
        return self.path.name

    @property
    def plan(self) -> Path:
        return self.path / "plan.md"

    @property
    def state_file(self) -> Path:
        return self.path / "state.json"

    @property
    def logs(self) -> Path:
        return self.path / "logs"

    def rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.project))
        except ValueError:
            return str(p)

    def report(self, n: int) -> Path:
        return self.path / f"report-{n}.md"

    def review(self, n: int) -> Path:
        return self.path / f"review-{n}.md"

    def reports(self) -> dict[int, Path]:
        return numbered(self.path, "report")

    def reviews(self) -> dict[int, Path]:
        return numbered(self.path, "review")

    def latest_report(self) -> int:
        return max(self.reports(), default=0)

    def latest_review(self) -> int:
        return max(self.reviews(), default=0)

    def verdict(self, n: int) -> str | None:
        v = frontmatter(self.review(n)).get("verdict", "").lower().replace("-", "_")
        return v or None

    def scope(self) -> list[str] | None:
        """The repo-relative paths this task may change, or None when it declares none.

        `files:` entries ending in `/` are directories; `*`, `?` and `[` make a glob.
        """
        entries: list[str] = []
        for part in frontmatter(self.plan).get("files", "").split(","):
            entry = _clean_entry(part)
            if entry and entry not in entries:
                entries.append(entry)
        return entries or None

    def checked_scope(self) -> list[str] | None:
        """`scope()`, raising FlowError for entries that are not allowed."""
        scope = self.scope()
        for entry in scope or []:
            problem = scope_problem(entry)
            if problem:
                raise FlowError(f"{self.rel(self.plan)}: files: entry '{entry}' {problem}.")
        return scope

    def pathspecs(self) -> list[str] | None:
        """`scope()` translated to git pathspecs, or None for the whole working tree."""
        scope = self.scope()
        return None if scope is None else [git_pathspec(entry) for entry in scope]

    # -- state -------------------------------------------------------------
    def load(self) -> dict:
        if self.state_file.is_file():
            return json.loads(self.state_file.read_text())
        return {}

    def save(self, state: dict) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        tmp.replace(self.state_file)

    def update(self, **changes) -> dict:
        state = self.load()
        state.update(changes)
        self.save(state)
        return state

    def log_event(self, message: str, **fields) -> None:
        state = self.load()
        state.setdefault("history", []).append({"at": now(), "event": message, **fields})
        self.save(state)


# -- creation and lookup ---------------------------------------------------

def create(project: Path, name: str) -> Task:
    from . import sessions  # imported here to avoid an import cycle (sessions lists tasks)

    base = tasks_dir(project)
    base.mkdir(parents=True, exist_ok=True)
    nums = [int(m.group(1)) for p in base.iterdir() if (m := re.match(r"(\d+)-", p.name))]
    task_id = f"{max(nums, default=0) + 1:03d}-{slugify(name)}"
    task = Task(project, base / task_id)
    task.path.mkdir()
    task.logs.mkdir()
    template = (TEMPLATES / "plan.md").read_text()
    task.plan.write_text(template.replace("{task_id}", task_id))
    task.save({
        "id": task_id,
        "created": now(),
        "status": "planning",
        "base_commit": gitutil.head(project),
        "preexisting_dirty": gitutil.dirty_paths(project),
        "worker": None,
        "worker_since_round": 1,
        "sessions": {},
        "chat_session": sessions.current_session(project),
        "history": [],
    })
    return task


def all_tasks(project: Path) -> list[Task]:
    base = tasks_dir(project)
    if not base.is_dir():
        return []
    return [Task(project, p) for p in sorted(base.iterdir()) if p.is_dir() and re.match(r"\d+-", p.name)]


def find(project: Path, ref: str) -> Task:
    ref = ref.rstrip("/").split("/")[-1]
    matches = []
    for task in all_tasks(project):
        num = task.id.split("-", 1)[0]
        if ref == task.id or (ref.isdigit() and int(ref) == int(num)):
            return task
        if task.id.startswith(ref):
            matches.append(task)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FlowError(f"no task matches '{ref}'. Run `aaagent status` to list tasks.")
    raise FlowError(f"'{ref}' matches several tasks: {', '.join(t.id for t in matches)}")


# -- next-phase rules ------------------------------------------------------

@dataclass
class Step:
    phase: str          # implement | fix | review | commit
    round: int          # report number this job produces (review: the round it reviews)
    worker: str
    switched: bool = False   # worker changed by the fallback rule
    reason: str = ""
    note: str = ""      # why the step chose this worker, when it is not a fallback


def plan_is_written(task: Task) -> bool:
    text = task.plan.read_text() if task.plan.is_file() else ""
    return bool(text.strip()) and "<!-- TEMPLATE" not in text


def next_step(task: Task, cfg: Config, worker_override: str | None = None, force: bool = False) -> Step:
    """Decide what `aaagent dispatch` should do next, or raise FlowError explaining why not."""
    state = task.load()
    status = state.get("status")
    if status in FINAL_STATUSES:
        raise FlowError(f"task {task.id} is {status}; create a new task with `aaagent new`.")
    if status == "running":
        raise FlowError(f"task {task.id} already has a job running. Use `aaagent wait {task.id}`.")
    if not plan_is_written(task):
        raise FlowError(f"{task.rel(task.plan)} still holds the template. Write the plan first.")
    task.checked_scope()

    names = cfg.worker_names()
    if worker_override and worker_override not in names:
        raise FlowError(f"--worker {worker_override} is not configured "
                        f"(workers: {', '.join(names)}); see `aaagent workers`")
    plan_worker = frontmatter(task.plan).get("worker") or None
    if plan_worker and plan_worker not in names:
        raise FlowError(f"plan.md says worker: {plan_worker}, which is not configured "
                        f"(workers: {', '.join(names)}); fix the plan or use --worker")
    reports, reviews = task.latest_report(), task.latest_review()

    if reports == 0:
        worker = worker_override or plan_worker or cfg.default_worker
        cfg.worker_model(worker)
        return Step("implement", 1, worker)

    if reviews < reports:
        reviewer = cfg.reviewer_worker
        if reviewer:
            return Step("review", reports, reviewer)
        raise FlowError(
            f"report-{reports}.md has no review yet. Review the work and write "
            f"{task.rel(task.review(reports))} (verdict: approved or changes_requested).")

    verdict = task.verdict(reviews)
    current = state.get("worker")
    if current not in names:  # a stale name from a removed worker: ignore it
        current = plan_worker or cfg.default_worker

    if verdict == "approved":
        worker = worker_override or current
        cfg.worker_model(worker)
        return Step("commit", reports, worker)

    if verdict != "changes_requested":
        raise FlowError(
            f"{task.rel(task.review(reviews))} must start with frontmatter "
            f"`verdict: approved` or `verdict: changes_requested` (found: {verdict!r}).")

    if reports >= cfg.max_review_rounds and not force:
        raise FlowError(
            f"{reports} rounds without approval (max_review_rounds = {cfg.max_review_rounds}). "
            f"Stop and ask the user how to proceed. If they want another round, run "
            f"`aaagent dispatch {task.id} --force`; to drop the task, `aaagent abandon {task.id}`.")

    if worker_override:
        cfg.worker_model(worker_override)
        return Step("fix", reports + 1, worker_override, switched=worker_override != current)

    since = int(state.get("worker_since_round", 1))
    rejected_in_a_row = reviews - since + 1
    alt = cfg.alternate(current)
    if alt and rejected_in_a_row >= cfg.fallback_after_failed_reviews:
        return Step("fix", reports + 1, alt, switched=True,
                    reason=f"{current} failed review {rejected_in_a_row} times in a row")
    return Step("fix", reports + 1, current)


# -- active tasks (several at a time when their files: do not overlap) ---------

def active_file(project: Path) -> Path:
    """The old single-task file: read once and migrated into active-tasks.json."""
    return agent_dir(project) / "active-task"


def active_tasks_file(project: Path) -> Path:
    return agent_dir(project) / "active-tasks.json"


def active_lock_file(project: Path) -> Path:
    return agent_dir(project) / "active-tasks.lock"


_lock_state = threading.local()


@contextmanager
def active_lock(project: Path):
    """Serialise the active-task list: `flock` across processes, re-entrant in one thread."""
    depth = getattr(_lock_state, "depth", 0)
    if depth:
        _lock_state.depth = depth + 1
        try:
            yield
        finally:
            _lock_state.depth = depth
        return
    path = active_lock_file(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        _lock_state.depth = 1
        try:
            yield
        finally:
            _lock_state.depth = 0
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _read_active(project: Path) -> list[str]:
    ids: list[str] = []
    f = active_tasks_file(project)
    if f.is_file():
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            data = []
        if isinstance(data, list):
            ids = [str(item).strip() for item in data if str(item).strip()]
    legacy = active_file(project)
    if legacy.is_file():
        old = legacy.read_text().strip()
        if old and old not in ids:
            ids.append(old)
        legacy.unlink(missing_ok=True)
    return ids


def _save_active(project: Path, ids: list[str]) -> None:
    f = active_tasks_file(project)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f"{f.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(ids, indent=2) + "\n")
    tmp.replace(f)


def _prune_active(project: Path, ids: list[str]) -> tuple[list[str], bool]:
    keep: list[str] = []
    for task_id in ids:
        try:
            status = find(project, task_id).load().get("status")
        except (FlowError, OSError, json.JSONDecodeError):
            status = None
        if status in FINAL_STATUSES or status is None or task_id in keep:
            continue
        keep.append(task_id)
    return keep, keep != ids


def active_tasks(project: Path) -> list[str]:
    """The ids of the tasks that are in progress in this working tree."""
    with active_lock(project):
        ids = _read_active(project)
        keep, changed = _prune_active(project, ids)
        if changed or (keep and not active_tasks_file(project).is_file()):
            _save_active(project, keep)
        return keep


def active_task(project: Path) -> str | None:
    """The first active task id; kept for callers that only know the single-task world."""
    ids = active_tasks(project)
    return ids[0] if ids else None


def claim_active(project: Path, task: Task) -> None:
    """Mark the task as running here, if no other active task's files: overlap it."""
    scope = task.checked_scope()
    with active_lock(project):
        ids = _read_active(project)
        keep, _ = _prune_active(project, ids)
        if task.id in keep:
            if keep != ids:
                _save_active(project, keep)
            return
        for other_id in keep:
            other = find(project, other_id)
            other_scope = other.checked_scope()
            if scope is None or other_scope is None:
                who = other.id if other_scope is None else "this task"
                raise FlowError(
                    f"task {other.id} is still in progress in this working tree and {who} does "
                    f"not declare files:, so they cannot run at the same time. Finish it "
                    f"(review → commit) or abandon it, or give both plans a files: list that "
                    f"does not overlap.")
            overlaps = scopes_overlap(scope, other_scope)
            if overlaps:
                raise FlowError(
                    f"task {other.id} is still in progress in this working tree and its files: "
                    f"overlap this task's: {'; '.join(overlaps)}. Finish it (review → commit) or "
                    f"abandon it, or give both plans files: lists that do not overlap.")
        keep.append(task.id)
        _save_active(project, keep)


def release_active(project: Path, task: Task) -> None:
    with active_lock(project):
        ids = _read_active(project)
        keep = [task_id for task_id in ids if task_id != task.id]
        if keep != ids:
            _save_active(project, keep)
