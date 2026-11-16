"""Small git helpers. All functions take the project root.

With `[git] branch_mode = "none"` (No Git) the project folder is not a git repository, or
must not be touched: aaagent keeps a hidden repository of its own in `.aaagent/shadow.git`
with the folder as its work tree, and every git call about the project goes there through
`GIT_DIR` and `GIT_WORK_TREE` (see `git_env`).
"""

from __future__ import annotations

import fcntl
import os
import subprocess
from pathlib import Path

from .config import Config

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
AGENT_DIR = ".aaagent"
SHADOW = "shadow.git"
# Set by a chat or a worker running in No Git mode; they must not leak into a git call
# about a real repository.
GIT_ENV_KEYS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


def shadow_dir(project: Path) -> Path:
    """The hidden repository aaagent keeps in No Git mode."""
    return project / AGENT_DIR / SHADOW


def no_git(project: Path) -> bool:
    """True when the project runs without git (`[git] branch_mode = "none"`)."""
    return Config.load(project).branch_mode == "none"


def _stripped_env() -> dict:
    """os.environ without the GIT_* variables a No Git chat or worker sets."""
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_KEYS}
    return env


def _shadow_env(project: Path) -> dict:
    env = _stripped_env()
    env["GIT_DIR"] = str(shadow_dir(project))
    env["GIT_WORK_TREE"] = str(project)
    return env


def git_env(project: Path) -> dict:
    """The environment for every git call about `project`.

    Always an explicit dict: a No Git chat or worker has GIT_DIR set, and it must not leak
    into git calls about a real repository. In No Git mode the shadow repository is the
    target, and it is created first when it does not exist yet.
    """
    if no_git(project):
        ensure_shadow(project)
        return _shadow_env(project)
    return _stripped_env()


def _run(project: Path, args: list[str], env: dict) -> str:
    """Run git with an explicit environment; RuntimeError with git's stderr on failure."""
    proc = subprocess.run(["git", *args], cwd=project, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def ensure_shadow(project: Path) -> None:
    """Create `.aaagent/shadow.git` with the folder as its work tree, if it is missing.

    The first commit holds the folder as it is now, so later diffs show only what aaagent
    changed. The project's own `.gitignore` files are honoured, a real `.git` directory is
    always skipped, and `.aaagent/` is excluded so the shadow never records itself.
    """
    if (shadow_dir(project) / "HEAD").exists():
        return
    agent = project / AGENT_DIR
    agent.mkdir(parents=True, exist_ok=True)
    with (agent / "shadow.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if (shadow_dir(project) / "HEAD").exists():
            return
        env = _shadow_env(project)
        _run(project, ["init", "-q"], env)
        _run(project, ["config", "user.name", "aaagent"], env)
        _run(project, ["config", "user.email", "aaagent@localhost"], env)
        _run(project, ["config", "commit.gpgsign", "false"], env)
        exclude = shadow_dir(project) / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        text = exclude.read_text() if exclude.exists() else ""
        if f"/{AGENT_DIR}/" not in text.split():
            with exclude.open("a") as fh:
                fh.write(("" if text.endswith("\n") or not text else "\n") + f"/{AGENT_DIR}/\n")
        _run(project, ["add", "-A"], env)
        _run(project, ["commit", "-q", "--allow-empty", "-m", "aaagent: starting point"], env)


def git(project: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=project, capture_output=True, text=True,
                          env=git_env(project))
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def find_root(start: Path) -> Path | None:
    """The real repository root containing `start`; None outside one (No Git included)."""
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start,
                          capture_output=True, text=True, env=_stripped_env())
    return Path(proc.stdout.strip()) if proc.returncode == 0 else None


def find_project(start: Path) -> Path | None:
    """The No Git aaagent folder containing `start`, else the real repository root.

    The walk stops at the first real repository: a No Git folder above it must not capture
    a repository inside it (`~/work` in No Git mode never owns `~/work/repo/sub`).
    """
    resolved = start.resolve()
    for directory in (resolved, *resolved.parents):
        if (directory / AGENT_DIR).is_dir() and no_git(directory):
            return directory
        if (directory / ".git").exists():  # a `.git` directory, or a file (worktree)
            return find_root(start)
    return find_root(start)


def head(project: Path) -> str | None:
    proc = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=project,
                          capture_output=True, text=True, env=git_env(project))
    return proc.stdout.strip() or None


def _pathspecs(paths: list[str] | None) -> list[str]:
    """Git pathspecs limiting the output to `paths` (None = the whole project)."""
    if paths is None:
        return [".", f":(exclude){AGENT_DIR}"]
    return [*paths, f":(exclude){AGENT_DIR}"]


def dirty_paths(project: Path, paths: list[str] | None = None) -> list[str]:
    """Changed or untracked paths (optionally only `paths`), excluding the agent folder."""
    out = git(project, "status", "--porcelain", "--untracked-files=all", "--", *_pathspecs(paths))
    found = []
    for line in out.splitlines():
        if not line.strip():
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        found.append(path.strip('"'))
    return found


def untracked(project: Path, paths: list[str] | None = None) -> list[str]:
    out = git(project, "ls-files", "--others", "--exclude-standard", "--", *_pathspecs(paths))
    return [p for p in out.splitlines() if p]


def diff_stat(project: Path, base: str | None, paths: list[str] | None = None) -> str:
    base = base or EMPTY_TREE
    stat = git(project, "diff", "--stat", base, "--", *_pathspecs(paths), check=False).rstrip()
    new = untracked(project, paths)
    if new:
        stat += ("\n" if stat else "") + "\n".join(f" {p} (new, untracked)" for p in new)
    return stat or " (no changes)"


def full_diff(project: Path, base: str | None, paths: list[str] | None = None) -> str:
    base = base or EMPTY_TREE
    out = git(project, "diff", base, "--", *_pathspecs(paths), check=False)
    for path in untracked(project, paths):
        proc = subprocess.run(["git", "diff", "--no-index", "--", "/dev/null", path],
                              cwd=project, capture_output=True, text=True,
                              env=git_env(project))
        out += proc.stdout
    return out


def log_since(project: Path, base: str | None) -> str:
    rng = f"{base}..HEAD" if base else "HEAD"
    return git(project, "log", "--oneline", rng, check=False).strip()


def ensure_excluded(project: Path) -> None:
    """Add .aaagent/ to .git/info/exclude so it is never committed."""
    git_dir = Path(git(project, "rev-parse", "--git-dir").strip())
    if not git_dir.is_absolute():
        git_dir = project / git_dir
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text() if exclude.exists() else ""
    if f"/{AGENT_DIR}/" not in text.split():
        with exclude.open("a") as fh:
            fh.write(("" if text.endswith("\n") or not text else "\n") + f"/{AGENT_DIR}/\n")


def checkpoint(project: Path, message: str, pathspecs: list[str] | None = None,
               exclude: list[str] | tuple[str, ...] = ()) -> str:
    """Record a commit in the shadow repository (No Git mode only); the new HEAD.

    `exclude` paths are staged and then unstaged again: `git add -A -- <path>
    ':(exclude)<other>'` adds nothing for an untracked file inside a new directory (git
    2.39). `.aaagent/` stays out through the shadow's `info/exclude`, which `ensure_shadow`
    always writes.
    """
    if not no_git(project):
        raise RuntimeError(f"a checkpoint needs [git] branch_mode = \"none\" ({project})")
    # A declared path with no change at all (never created, or already committed) makes
    # `git add` fail with "did not match any files"; adding it would add nothing anyway.
    wanted = list(pathspecs) if pathspecs else ["."]
    if wanted != ["."]:
        wanted = [spec for spec in wanted
                  if git(project, "status", "--porcelain", "--", spec, check=False).strip()]
    if wanted:
        git(project, "add", "-A", "--", *wanted)
        if exclude:
            # A path that is not in the index cannot be unstaged; `git reset` complains.
            git(project, "reset", "-q", "--", *exclude, check=False)
    git(project, "commit", "-q", "--allow-empty", "-m", message)
    new = head(project)
    if new is None:
        raise RuntimeError("the checkpoint did not create a commit")
    return new


def still_dirty(project: Path, pathspecs: list[str] | None = None,
                exclude: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Dirty paths inside `pathspecs` and outside `exclude`, e.g. after a checkpoint."""
    skipped = set(exclude)
    return [path for path in dirty_paths(project, pathspecs) if path not in skipped]


def show(project: Path, commit: str) -> str:
    """A commit as `git show` prints it (header, stat and patch)."""
    return git(project, "show", "--stat", "--patch",
               "--format=commit %H%nDate:   %ad%n%n    %s%n%n%b", commit, check=False)
