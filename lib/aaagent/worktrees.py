"""Per-task git worktrees: `[parallel] isolation = "worktree"`.

With isolation enabled, `aaagent dispatch` gives each task its own git worktree
(`<dir>/<task-id>` on branch `aaagent/<task-id>`) at its first dispatch. Its implement, fix
and review jobs then run in that worktree instead of the project's working tree, so parallel
tasks do not see each other's half-written code when they run tests.

A worktree is a separate checkout, so the planner's files (`.aaagent/tasks/<id>/`) are copied
into it before each job (`sync_into`) and the job's report or review is copied back afterwards
(`sync_back`). `.aaagent/` is excluded through `.git/info/exclude`, which git shares between
all worktrees of a repository, so those copies never show up in git status there either.

`links` entries that exist in the project are symlinked into the worktree (`.venv` and the
like); `setup` is a shell command run once in the new worktree. Committing a worktree task is
not supported yet.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import gitutil, tasks

DEFAULT_DIR = ".aaagent/worktrees"
SETUP_TIMEOUT = 900  # seconds
GIT_TIMEOUT = 300  # seconds; a worktree checkout of a big repository can be slow
OUTPUT_LINES = 20


def settings(cfg) -> dict:
    """`isolation`, `dir`, `links` and `setup` from `[parallel]`."""
    section = cfg.data.get("parallel", {})
    if not isinstance(section, dict):
        section = {}
    isolation = str(section.get("isolation") or "shared").strip().lower()
    if isolation not in ("shared", "worktree"):
        isolation = "shared"
    links = section.get("links") or []
    if isinstance(links, str):
        links = [links]
    return {
        "isolation": isolation,
        "dir": str(section.get("dir") or DEFAULT_DIR),
        "links": [str(entry) for entry in links if str(entry).strip()],
        "setup": str(section.get("setup") or ""),
    }


def enabled(project: Path, cfg) -> bool:
    """Whether tasks get their own worktrees in this project."""
    return (settings(cfg)["isolation"] == "worktree"
            and not gitutil.no_git(project)
            and not cfg.track_agent_dir)


def why_disabled(project: Path, cfg) -> str | None:
    """Why `[parallel] isolation = "worktree"` cannot be used here, or None."""
    if settings(cfg)["isolation"] != "worktree":
        return None
    if gitutil.no_git(project):
        return "No Git mode"
    if cfg.track_agent_dir:
        return "track_agent_dir = true"
    return None


def branch_for(task) -> str:
    return f"aaagent/{task.id}"


def path_for(project: Path, cfg, task) -> Path:
    """`<dir>/<task.id>`; a relative dir is relative to the project."""
    directory = Path(settings(cfg)["dir"])
    if not directory.is_absolute():
        directory = project / directory
    return directory / task.id


def _tail(text: str) -> str:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return "\n".join(lines[-OUTPUT_LINES:])


def _git(project: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git in `project` with a timeout; FlowError (with the output) on failure."""
    command = "git " + " ".join(args)
    try:
        proc = subprocess.run(["git", *args], cwd=project, env=gitutil.git_env(project),
                              capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise tasks.FlowError(f"{command} timed out after {GIT_TIMEOUT}s in {project}") from None
    if check and proc.returncode != 0:
        detail = _tail(proc.stderr) or _tail(proc.stdout) or f"exited {proc.returncode}"
        raise tasks.FlowError(f"{command} failed in {project}: {detail}")
    return proc


def create(project: Path, cfg, task, base: str) -> Path:
    """Create the task's worktree on its own branch at `base`; the worktree path."""
    st = settings(cfg)
    path = path_for(project, cfg, task)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        # A leftover from an earlier attempt: `-B` reuses the branch anyway.
        _git(project, "worktree", "remove", "--force", str(path), check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    _git(project, "worktree", "prune", check=False)
    _git(project, "worktree", "add", "-B", branch_for(task), str(path), base)

    for entry in st["links"]:
        source = project / entry
        target = path / entry
        if not source.exists() or target.exists() or target.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source, target_is_directory=source.is_dir())

    if st["setup"]:
        _run_setup(path, st["setup"])
    return path


def _run_setup(path: Path, command: str) -> None:
    try:
        proc = subprocess.run(command, shell=True, cwd=path, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                              timeout=SETUP_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        raise tasks.FlowError(
            f"the [parallel] setup command timed out after {SETUP_TIMEOUT}s in {path}: "
            f"{command}\n{_tail(output)}") from None
    if proc.returncode != 0:
        raise tasks.FlowError(
            f"the [parallel] setup command failed with exit code {proc.returncode} in {path}: "
            f"{command}\n{_tail(proc.stdout)}")


def workdir(project: Path, task) -> Path:
    """The directory the task's files live in: its worktree when it has one, else the project."""
    path = task.load().get("worktree")
    if path:
        worktree = Path(path)
        if worktree.is_dir():
            return worktree
    return project


def _task_dir(root: Path, task) -> Path:
    return tasks.agent_dir(root) / "tasks" / task.id


def sync_into(project: Path, task, worktree: Path) -> None:
    """Copy the task's plan, reports and reviews (and the attachments) into the worktree."""
    target = _task_dir(worktree, task)
    target.mkdir(parents=True, exist_ok=True)
    names = ["plan.md", *[p.name for p in task.reports().values()],
             *[p.name for p in task.reviews().values()]]
    for name in names:
        source = task.path / name
        if source.is_file():
            shutil.copy2(source, target / name)
    attachments = tasks.agent_dir(project) / "attachments"
    if attachments.is_dir():
        shutil.copytree(attachments, tasks.agent_dir(worktree) / "attachments",
                        dirs_exist_ok=True)


def sync_back(project: Path, task, worktree: Path, name: str) -> bool:
    """Copy `.aaagent/tasks/<id>/<name>` from the worktree back; whether it existed there."""
    source = _task_dir(worktree, task) / name
    if not source.is_file():
        return False
    task.path.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, task.path / name)
    return True
