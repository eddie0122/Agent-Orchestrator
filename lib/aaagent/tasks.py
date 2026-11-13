"""Task folders, state.json, and the rules that decide the next phase.

Layout inside a project:
    .aaagent/tasks/NNN-slug/plan.md        written by the orchestrator
    .aaagent/tasks/NNN-slug/report-N.md    written by the worker (round N)
    .aaagent/tasks/NNN-slug/review-N.md    written by the orchestrator (reviews report-N)
    .aaagent/tasks/NNN-slug/state.json     bookkeeping
    .aaagent/tasks/NNN-slug/logs/          raw worker output
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import gitutil
from .config import ROOT, Config

AGENT_DIR = ".aaagent"
TEMPLATES = ROOT / "templates"
FINAL_STATUSES = {"committed", "abandoned"}


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
    phase: str          # implement | fix | commit
    round: int          # report number this job produces (commit: last round)
    worker: str
    switched: bool = False   # worker changed by the fallback rule
    reason: str = ""


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

    plan_worker = frontmatter(task.plan).get("worker") or None
    reports, reviews = task.latest_report(), task.latest_review()

    if reports == 0:
        worker = worker_override or plan_worker or cfg.default_worker
        cfg.worker_model(worker)
        return Step("implement", 1, worker)

    if reviews < reports:
        raise FlowError(
            f"report-{reports}.md has no review yet. Review the work and write "
            f"{task.rel(task.review(reports))} (verdict: approved or changes_requested).")

    verdict = task.verdict(reviews)
    current = state.get("worker") or plan_worker or cfg.default_worker

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


# -- active-task lock ------------------------------------------------------

def active_file(project: Path) -> Path:
    return agent_dir(project) / "active-task"


def active_task(project: Path) -> str | None:
    f = active_file(project)
    if not f.is_file():
        return None
    task_id = f.read_text().strip()
    try:
        status = find(project, task_id).load().get("status")
    except FlowError:
        status = None
    if status in FINAL_STATUSES or status is None:
        f.unlink(missing_ok=True)
        return None
    return task_id


def claim_active(project: Path, task: Task) -> None:
    current = active_task(project)
    if current and current != task.id:
        raise FlowError(
            f"task {current} is still in progress in this working tree. Finish it "
            f"(review → commit) or run `aaagent abandon {current}` before starting {task.id}.")
    active_file(project).write_text(task.id + "\n")


def release_active(project: Path, task: Task) -> None:
    f = active_file(project)
    if f.is_file() and f.read_text().strip() == task.id:
        f.unlink()
