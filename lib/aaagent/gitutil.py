"""Small git helpers. All functions take the project root."""

from __future__ import annotations

import subprocess
from pathlib import Path

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
AGENT_DIR = ".aaagent"


def git(project: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=project, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def find_root(start: Path) -> Path | None:
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start, capture_output=True, text=True)
    return Path(proc.stdout.strip()) if proc.returncode == 0 else None


def head(project: Path) -> str | None:
    proc = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=project, capture_output=True, text=True)
    return proc.stdout.strip() or None


def dirty_paths(project: Path) -> list[str]:
    """Changed or untracked paths, excluding the agent folder."""
    out = git(project, "status", "--porcelain", "--untracked-files=all", "--", ".", f":(exclude){AGENT_DIR}")
    paths = []
    for line in out.splitlines():
        if not line.strip():
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip('"'))
    return paths


def untracked(project: Path) -> list[str]:
    out = git(project, "ls-files", "--others", "--exclude-standard", "--", ".", f":(exclude){AGENT_DIR}")
    return [p for p in out.splitlines() if p]


def diff_stat(project: Path, base: str | None) -> str:
    base = base or EMPTY_TREE
    stat = git(project, "diff", "--stat", base, "--", ".", f":(exclude){AGENT_DIR}", check=False).rstrip()
    new = untracked(project)
    if new:
        stat += ("\n" if stat else "") + "\n".join(f" {p} (new, untracked)" for p in new)
    return stat or " (no changes)"


def full_diff(project: Path, base: str | None) -> str:
    base = base or EMPTY_TREE
    out = git(project, "diff", base, "--", ".", f":(exclude){AGENT_DIR}", check=False)
    for path in untracked(project):
        proc = subprocess.run(["git", "diff", "--no-index", "--", "/dev/null", path],
                              cwd=project, capture_output=True, text=True)
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
