"""One session branch at a time: `[git] branch_mode = current | branch | pr`.

A working tree has one branch checked out, so a branch belongs to a chat session. It starts at
the first `aaagent dispatch` of a task while no batch is open, every task dispatched while it is
open joins it (also after a pause with no active task), and it ends only when `aaagent pr` or
`aaagent branch-done` finishes it — or by itself, without pushing and with the branch deleted,
when it goes idle with no commits at all. When its last active task is committed, the branch
stays open and the lines that dispatch prints tell the planner to ask the user what to do.

Files, inside `.aaagent/`:
    batch.json                                   the open batch (session branch)
    batches/<YYYYmmdd-HHMMSS>-<branch>.json      every finished branch
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import gitutil, tasks
from .config import Config

# The first word of a task's slug picks the branch prefix; anything else is a feature.
PREFIXES = {"fix": "fix", "bug": "fix", "bugfix": "fix", "hotfix": "fix", "docs": "docs",
            "doc": "docs", "refactor": "refactor", "test": "test", "tests": "test",
            "chore": "chore", "feat": "feat", "feature": "feat"}
DEFAULT_PREFIX = "feat"

# The push and the pull request run without a terminal and never wait forever.
PUSH_TIMEOUT_SECONDS = 120
GH_TIMEOUT_SECONDS = 60


# -- files ---------------------------------------------------------------------

def batch_file(project: Path) -> Path:
    return tasks.agent_dir(project) / "batch.json"


def batches_dir(project: Path) -> Path:
    return tasks.agent_dir(project) / "batches"


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def _closed_files(project: Path) -> list[Path]:
    """The closed batches' history files, oldest first.

    Ordered by each record's `closed` timestamp (then its name), so the `-2` suffix of a
    same-second file sorts after the original.
    """
    directory = batches_dir(project)
    if not directory.is_dir():
        return []
    found = []
    for path in directory.glob("*.json"):
        record = _load_json(path)
        found.append((str((record or {}).get("closed") or ""), path.name, path))
    found.sort()
    return [path for _, _, path in found]


def open_batch(project: Path) -> dict | None:
    """The batch that is open now, or None."""
    return _load_json(batch_file(project))


def last_closed(project: Path) -> dict | None:
    """The newest closed batch, or None."""
    files = _closed_files(project)
    return _load_json(files[-1]) if files else None


def _history_path(project: Path, record: dict) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{str(record.get('branch') or 'branch').replace('/', '_')}"
    path = batches_dir(project) / f"{name}.json"
    n = 2
    while path.exists():
        path = batches_dir(project) / f"{name}-{n}.json"
        n += 1
    return path


# -- names ---------------------------------------------------------------------

def _number(task: tasks.Task) -> str:
    return task.id.split("-", 1)[0]


def _slug(task: tasks.Task) -> str:
    return task.id.split("-", 1)[1] if "-" in task.id else task.id


def _valid_branch(name: str) -> bool:
    if not name:
        return False
    proc = subprocess.run(["git", "check-ref-format", "--branch", name],
                          capture_output=True, text=True)
    return proc.returncode == 0


def _fit(task: tasks.Task, slug: str) -> str:
    """`feat/<slug>`, or `feat/<number>` when the slug would not make a valid branch."""
    for candidate in (f"{DEFAULT_PREFIX}/{slug}", f"{DEFAULT_PREFIX}/{_number(task)}"):
        if _valid_branch(candidate):
            return candidate
    return f"{DEFAULT_PREFIX}/task"


def _derived(task: tasks.Task) -> str:
    """`<prefix>/<slug>`: a conventional prefix from the slug's first word, and the rest."""
    slug = _slug(task)
    head, _, rest = slug.partition("-")
    prefix = PREFIXES.get(head.lower())
    if prefix:
        slug = rest or _number(task)
    else:
        prefix = DEFAULT_PREFIX
    return f"{prefix}/{slug}"


def branch_name(task: tasks.Task) -> str:
    """The branch a task starts: its plan's `branch:`, else derived from its title slug."""
    wanted = str(tasks.frontmatter(task.plan).get("branch") or "").strip()
    if wanted:
        return wanted if _valid_branch(wanted) else _fit(task, _slug(task))
    derived = _derived(task)
    return derived if _valid_branch(derived) else _fit(task, _slug(task))


def _branch_exists(project: Path, name: str) -> bool:
    proc = subprocess.run(["git", "rev-parse", "--verify", "-q", f"refs/heads/{name}"],
                          cwd=project, capture_output=True, text=True)
    return proc.returncode == 0


def unique_branch(project: Path, name: str) -> str:
    """`name`, or `name-2`, `name-3`, … when a local branch already has it."""
    if not _branch_exists(project, name):
        return name
    n = 2
    while _branch_exists(project, f"{name}-{n}"):
        n += 1
    return f"{name}-{n}"


def current_branch(project: Path) -> str | None:
    """The checked-out branch, or None when HEAD is detached."""
    out = gitutil.git(project, "symbolic-ref", "--short", "-q", "HEAD", check=False).strip()
    return out or None


# -- the batch lifecycle -------------------------------------------------------

def start_if_needed(project: Path, task: tasks.Task, cfg: Config) -> str | None:
    """Join or open the batch for this task's first dispatch; the line to print, or None."""
    with tasks.active_lock(project):
        batch = open_batch(project)
        if batch:
            branch = str(batch.get("branch") or "")
            ids = list(batch.get("tasks") or [])
            if task.id in ids:
                return None
            ids.append(task.id)
            batch["tasks"] = ids
            _save_json(batch_file(project), batch)
            wanted = str(tasks.frontmatter(task.plan).get("branch") or "").strip()
            if wanted and wanted != branch:
                return (f"{task.id} joins the open branch {branch} "
                        f"(its plan's branch: {wanted} is ignored)")
            return f"{task.id} works on {branch}"
        if cfg.branch_mode == "current":
            return None
        others = [task_id for task_id in tasks.active_tasks(project) if task_id != task.id]
        if others:  # a mode switched on mid-way: wait for a fresh batch
            return None
        head = gitutil.head(project)
        if head is None:
            raise tasks.FlowError(
                f"cannot create a branch for {task.id}: this repository has no commits yet. "
                f"Commit something first, or set [git] branch_mode = \"current\".")
        base = current_branch(project)
        if base is None:
            raise tasks.FlowError(
                f"cannot create a branch for {task.id}: HEAD is detached. Check out a branch "
                f"first (git switch <branch>), or set [git] branch_mode = \"current\".")
        name = unique_branch(project, branch_name(task))
        mode = cfg.branch_mode
        proc = subprocess.run(["git", "switch", "-c", name], cwd=project,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            detail = (proc.stderr.strip() or proc.stdout.strip()
                      or f"git switch exited {proc.returncode}")
            raise tasks.FlowError(f"could not create the branch {name}: {detail}")
        _save_json(batch_file(project), {
            "branch": name, "base": base, "start_head": head, "mode": mode,
            "started": tasks.now(), "tasks": [task.id],
        })
        task.log_event("created branch", branch=name, base=base, mode=mode)
        return f"created branch {name} from {base} (branch_mode = {mode})"


def _commits_since(project: Path, start: str) -> list[str]:
    """`<hash> <subject>` for every commit since the branch started; `[]` on a git error."""
    if not start:
        return []
    out = gitutil.git(project, "log", "--format=%h %s", f"{start}..HEAD", check=False)
    return [line.strip() for line in out.splitlines() if line.strip()]


def _close_record(project: Path, batch: dict, commits: list[str]) -> tuple[Path, dict]:
    """Remove batch.json and write the history file. Call under the active-task lock."""
    record = {**batch, "closed": tasks.now(), "commits": commits,
              "pr_url": None, "pr_error": None}
    path = _history_path(project, record)
    batch_file(project).unlink(missing_ok=True)
    _save_json(path, record)
    return path, record


def on_idle(project: Path, cfg: Config) -> list[str]:
    """Called after a commit or an abandon; the lines to print.

    While the branch has commits it stays open, so the planner can ask the user before anything
    is pushed: the lines say which command finishes it. With no commits at all (for example the
    only task was abandoned) it closes itself, switches back to the base branch and deletes the
    empty branch. Everything that touches the checkout runs after the lock is released.
    """
    with tasks.active_lock(project):
        batch = open_batch(project)
        if not batch:
            return []
        if tasks.active_tasks(project):
            return []
        branch = str(batch.get("branch") or "")
        base = str(batch.get("base") or "main")
        commits = _commits_since(project, str(batch.get("start_head") or ""))
        mode = cfg.branch_mode
        path = record = None
        if not commits:
            path, record = _close_record(project, batch, commits)
    if commits:
        lines = [f"All tasks on branch {branch} are done "
                 f"({len(commits)} commit(s) since {base})."]
        if mode == "pr":
            lines.append(
                f"Ask the user whether to open a pull request into {base} now. Yes → run "
                f"`aaagent pr` (it pushes, opens the pull request and switches back to {base}). "
                f"No → keep the branch: tasks dispatched later join it, so ask again when they "
                f"are done.")
        else:
            lines.append(
                f"Ask the user whether to finish the branch now. Yes → run `aaagent branch-done` "
                f"(it switches back to {base}; nothing is pushed). No → keep the branch: tasks "
                f"dispatched later join it, so ask again when they are done.")
        return lines
    # No commits: the branch is empty, so close it, go back to the base and drop the branch.
    lines = _switch_back(project, record)
    if current_branch(project) != branch:
        detail = _delete_branch(project, branch)
        if detail:
            lines.append(f"could not delete the empty branch {branch}: {detail}")
        elif record.get("switched_back"):
            lines = [f"branch {branch} had no commits; switched back to {base} and deleted it"]
        else:
            lines.append(f"deleted the empty branch {branch}")
    _save_json(path, record)
    return lines


def finish(project: Path, cfg: Config, push: bool) -> list[str]:
    """End the open branch (`aaagent pr` with push, `aaagent branch-done` without it)."""
    with tasks.active_lock(project):
        batch = open_batch(project)
        if not batch:
            raise tasks.FlowError("no open branch to finish")
        branch = str(batch.get("branch") or "")
        active = tasks.active_tasks(project)
        if active:
            raise tasks.FlowError(
                f"tasks are still active on {branch}: {', '.join(active)}; wait for them to be "
                f"committed or abandon them first")
        commits = _commits_since(project, str(batch.get("start_head") or ""))
        path, record = _close_record(project, batch, commits)
    if push and commits:
        lines = open_pr(project, record)  # outside the lock: it may talk to the network
    elif push:
        lines = [f"branch {branch} has no commits; no pull request opened"]
    else:
        lines = [f"branch {branch} finished ({len(commits)} commits); not pushed"]
    lines += _switch_back(project, record)
    _save_json(path, record)
    return lines


def _switch_back(project: Path, record: dict) -> list[str]:
    """Leave the branch for its base when the tree allows it; the lines to print. Never raises."""
    branch = str(record.get("branch") or "")
    base = str(record.get("base") or "main")
    record["switched_back"] = False
    current = current_branch(project)
    if current != branch:
        return [f"the checkout is on {current or 'a detached HEAD'}, not {branch}; "
                f"left as it is"]
    dirty = gitutil.dirty_paths(project)
    if dirty:
        return [f"stayed on {branch}: uncommitted changes in {', '.join(dirty)}; "
                f"run `git switch {base}` yourself"]
    proc = subprocess.run(["git", "switch", base], cwd=project, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip()
                  or f"git switch exited {proc.returncode}")
        return [f"could not switch back to {base}: {detail}"]
    record["switched_back"] = True
    return [f"switched back to {base}"]


def _delete_branch(project: Path, branch: str) -> str:
    """`git branch -d` the empty branch; "" on success, else git's error."""
    proc = subprocess.run(["git", "branch", "-d", branch], cwd=project,
                          capture_output=True, text=True)
    if proc.returncode == 0:
        return ""
    return (proc.stderr.strip() or proc.stdout.strip() or f"git branch exited {proc.returncode}")


# -- pull requests -------------------------------------------------------------

def _commit_parts(commit: str) -> tuple[str, str]:
    """`<hash> <subject>` split into (hash, subject)."""
    sha, _, subject = commit.partition(" ")
    return sha, subject.strip()


def pr_title(record: dict) -> str:
    """The one commit's subject, else `<prefix>: <branch name without the prefix>`."""
    commits = list(record.get("commits") or [])
    if len(commits) == 1:
        return _commit_parts(commits[0])[1] or str(record.get("branch") or "")
    branch = str(record.get("branch") or "")
    prefix, sep, rest = branch.partition("/")
    if not sep:
        prefix, rest = DEFAULT_PREFIX, branch
    return f"{prefix}: {rest.replace('-', ' ') or branch}"


def pr_body(record: dict) -> str:
    lines = ["## Commits", ""]
    for commit in record.get("commits") or []:
        sha, subject = _commit_parts(str(commit))
        lines.append(f"- {subject} ({sha})")
    lines += ["", "## Tasks", ""]
    for task_id in record.get("tasks") or []:
        lines.append(f"- {task_id}")
    lines += ["", "Opened by aaagent."]
    return "\n".join(lines)


def _pr_env() -> dict:
    """The environment for the push and gh: never ask for a passphrase or a confirmation."""
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}


def open_pr(project: Path, record: dict) -> list[str]:
    """Push the record's branch and open its pull request; the lines to print. Never raises.

    `record` is updated with `pr_url` and `pr_error`; the caller saves it. Both commands run
    without a terminal and time out, so a credential prompt or a hung gh cannot block forever.
    """
    branch = str(record.get("branch") or "")
    base = str(record.get("base") or "main")
    record["pr_url"] = None
    record["pr_error"] = None

    def push_failed(detail: str) -> list[str]:
        return [f"could not push {branch} to origin: {detail}", "run `aaagent pr` to retry"]

    try:
        push = subprocess.run(["git", "push", "-u", "origin", branch], cwd=project,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=PUSH_TIMEOUT_SECONDS, env=_pr_env())
    except subprocess.TimeoutExpired:
        record["pr_error"] = f"git push timed out after {PUSH_TIMEOUT_SECONDS} s"
        return push_failed(record["pr_error"])
    except OSError as exc:
        record["pr_error"] = str(exc)
        return push_failed(str(exc))
    if push.returncode != 0:
        detail = (push.stderr or push.stdout).strip().splitlines()
        record["pr_error"] = detail[-1] if detail else f"git push exited {push.returncode}"
        return push_failed(record["pr_error"])
    # The branch is on origin; anything below only opens the pull request.
    gh = os.environ.get("AAAGENT_GH") or shutil.which("gh")
    if not gh:
        record["pr_error"] = "the GitHub CLI (gh) was not found"
        return [f"branch {branch} pushed, but the GitHub CLI (gh) was not found",
                "install it (https://cli.github.com), then run `aaagent pr` to retry"]
    command = [gh, "pr", "create", "--base", base, "--head", branch,
               "--title", pr_title(record), "--body", pr_body(record)]
    try:
        proc = subprocess.run(command, cwd=project, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=GH_TIMEOUT_SECONDS,
                              env=_pr_env())
    except subprocess.TimeoutExpired:
        record["pr_error"] = f"gh timed out after {GH_TIMEOUT_SECONDS} s"
        return [f"branch {branch} pushed, but {record['pr_error']}",
                "run `aaagent pr` to retry"]
    except OSError as exc:
        record["pr_error"] = str(exc)
        return [f"branch {branch} pushed, but gh could not run: {exc}",
                "run `aaagent pr` to retry"]
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        record["pr_error"] = detail or f"gh exited {proc.returncode}"
        return [f"branch {branch} pushed, but opening the pull request failed: "
                f"{record['pr_error']}", "run `aaagent pr` to retry"]
    url = ""
    for line in reversed(proc.stdout.splitlines()):
        if line.strip():
            url = line.strip()
            break
    if not url:
        record["pr_error"] = "gh printed no pull request URL"
        return [f"branch {branch} pushed, but gh printed no pull request URL",
                "run `aaagent pr` to retry"]
    record["pr_url"] = url
    return [f"pull request: {url}"]


def retry_pr(project: Path, cfg: Config) -> list[str]:
    """`aaagent pr`: finish the open branch, else open the newest closed batch's pull request."""
    if open_batch(project) is not None:
        return finish(project, cfg, push=True)
    with tasks.active_lock(project):
        files = _closed_files(project)
        if not files:
            return ["no finished branch to open a pull request for"]
        path = files[-1]
        record = _load_json(path)
        if record is None:
            return ["no finished branch to open a pull request for"]
        if record.get("pr_url"):
            return [f"pull request: {record['pr_url']}"]
    lines = open_pr(project, record)  # outside the lock: it may talk to the network
    _save_json(path, record)
    return lines


# -- status --------------------------------------------------------------------

def git_status(project: Path, cfg: Config) -> dict:
    """The `git` entry of `aaagent status --json`."""
    batch = open_batch(project)
    closed = last_closed(project)
    return {
        "mode": cfg.branch_mode,
        "branch": current_branch(project),
        "batch": {"branch": batch.get("branch"), "base": batch.get("base"),
                  "tasks": list(batch.get("tasks") or []),
                  "commits": len(_commits_since(project, str(batch.get("start_head") or ""))),
                  "idle": not tasks.active_tasks(project)} if batch else None,
        "last_pr": {"branch": closed.get("branch"), "url": closed.get("pr_url"),
                    "error": closed.get("pr_error")} if closed else None,
    }
