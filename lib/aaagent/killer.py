"""`aaagent kill`: find and kill every aaagent process of one project.

The processes of a project are:

- its worker daemons (`.aaagent/workers/*.pid`) and everything they started (their jobs);
- every process whose `AAAGENT_PROJECT` is the project, or one of its task worktrees: the chat,
  `aaagent dispatch` / `aaagent wait`, the dashboard;
- the tmux chat pane's `sh .aaagent/run-orchestrator.sh`;
- all descendants of those.

Only individual pids are signalled: a process group may be shared with the terminal that
started us, and the chat pane may still run its own close command while we work. The current
process and its ancestors are never killed, so `aaagent kill` can run from the chat's Bash
tool.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import attach, worker

COMMAND_CHARS = 100


@dataclass
class Target:
    pid: int
    role: str        # "worker1", "reviewer", "worker1 job", "chat", "aaagent dispatch", ...
    command: str     # the command line, joined with spaces, clipped to ~100 chars


@dataclass
class KillResult:
    killed: list[Target] = field(default_factory=list)     # processes signalled
    spared: list[Target] = field(default_factory=list)     # matched, but we must not kill them
    survivors: list[Target] = field(default_factory=list)  # still alive after SIGKILL
    window: str | None = None                              # tmux window that was closed


@dataclass
class _Proc:
    pid: int
    ppid: int
    state: str
    argv: list[str]
    environ: dict[str, str]


# -- reading the process table -----------------------------------------------------

def _read_stat(pid: int) -> tuple[int, str] | None:
    """(ppid, state) of a pid, or None when it is gone. The comm field may hold spaces and
    parentheses, so the fields start after the LAST `)`."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    end = text.rfind(")")
    if end < 0:
        return None
    parts = text[end + 1:].split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1]), parts[0]
    except ValueError:
        return None


def _read_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _read_environ(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    env: dict[str, str] = {}
    for part in raw.split(b"\0"):
        if not part:
            continue
        key, sep, value = part.partition(b"=")
        if sep:
            env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return env


def _ps_table() -> list[_Proc]:
    """The process table from `ps`, for systems without /proc: no environ is available."""
    try:
        done = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True,
                              text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return []
    procs = []
    for line in done.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        try:
            argv = shlex.split(parts[2])
        except ValueError:
            argv = [parts[2]]
        procs.append(_Proc(pid, ppid, "", argv, {}))
    return procs


def _process_table() -> tuple[list[_Proc], bool]:
    """The live processes, and whether their environ could be read."""
    if not Path("/proc/self/stat").exists():
        return _ps_table(), False
    procs: list[_Proc] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return [], True
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        stat = _read_stat(pid)
        if stat is None or stat[1] == "Z":
            continue        # gone, or a zombie: not a process we can stop
        procs.append(_Proc(pid, stat[0], stat[1], _read_cmdline(pid), _read_environ(pid)))
    return procs, True


# -- naming a process --------------------------------------------------------------

def _ancestors(pid: int, by_pid: dict[int, _Proc]) -> set[int]:
    """`pid` and every ancestor of it, up to init."""
    family: set[int] = set()
    while pid > 1 and pid not in family:
        family.add(pid)
        proc = by_pid.get(pid)
        parent = proc.ppid if proc else (_read_stat(pid) or (0, ""))[0]
        if parent <= 1:
            break
        pid = parent
    return family


def _clip(command: str) -> str:
    return command if len(command) <= COMMAND_CHARS else command[:COMMAND_CHARS - 3] + "..."


def _role(argv: list[str]) -> str:
    """A readable role from a command line."""
    if not argv:
        return "process"
    for part in argv[:2]:
        if os.path.basename(part).startswith("claude"):
            return "chat"
    for index, part in enumerate(argv[:-1]):
        if os.path.basename(part) == "aaagent":
            sub = argv[index + 1]
            if not sub.startswith("-"):
                return f"aaagent {sub}"
    return os.path.basename(argv[0]) or "process"


def _inside_project(value: str, root: str) -> bool:
    """Whether an AAAGENT_PROJECT value is this project or one of its worktrees."""
    try:
        real = os.path.realpath(value)
    except OSError:
        return False
    return real == root or real.startswith(root + "/.aaagent/worktrees/")


def _is_tmux(argv: list[str]) -> bool:
    """A tmux client or server. The server shows as `tmux: server`; tmux must never be a
    target, and we never walk below it: its children are every pane of every session."""
    if not argv:
        return False
    base = os.path.basename(argv[0])
    return base == "tmux" or base.startswith(("tmux:", "tmux "))


def find_targets(project: Path, exclude: Iterable[int] = ()) -> tuple[list[Target], list[Target]]:
    """The processes of a project (the run-orchestrator `sh` first), and the matching ones that
    must be left alone: this process, its ancestors, and `exclude`."""
    project = Path(project)
    root = os.path.realpath(project)
    given = {int(pid) for pid in exclude}
    procs, has_environ = _process_table()
    by_pid = {p.pid: p for p in procs}
    children: dict[int, list[int]] = {}
    for p in procs:
        children.setdefault(p.ppid, []).append(p.pid)

    # Processes we must not kill, and their subtrees: our own family, `exclude`, and tmux.
    family = _ancestors(os.getpid(), by_pid)
    spared_pids = family | given
    tmux_pids = {p.pid for p in procs if _is_tmux(p.argv)}

    daemons: dict[int, str] = {}
    for path in sorted((project / ".aaagent" / "workers").glob("*.pid")):
        try:
            pid = int(path.read_text().strip())
        except (OSError, ValueError):
            continue
        if pid > 1 and pid not in tmux_pids and worker.pid_alive(pid):
            daemons[pid] = path.stem

    roles: dict[int, str] = dict(daemons)
    scripts = {str(Path(root) / ".aaagent" / "run-orchestrator.sh"),
               str(project / ".aaagent" / "run-orchestrator.sh")}
    script_pids: set[int] = set()
    for p in procs:
        if p.pid in tmux_pids:
            continue
        matched = False
        if has_environ:
            value = p.environ.get("AAAGENT_PROJECT")
            if value and _inside_project(value, root):
                matched = True
        if any(script and script in " ".join(p.argv) for script in scripts):
            matched = True
            script_pids.add(p.pid)
        if matched and p.pid not in roles:
            roles[p.pid] = _role(p.argv)

    def descendants(start: int) -> set[int]:
        """Below `start`, stopping at a spared process and at tmux: never walk below either."""
        out: set[int] = set()
        seen: set[int] = set()
        stack = list(children.get(start, []))
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            if pid in spared_pids or pid in tmux_pids:
                continue
            out.add(pid)
            stack.extend(children.get(pid, []))
        return out

    # Every descendant of a root; those below a daemon are that daemon's job. A spared root
    # (our own shell, the chat we run in) is not walked: its children are not ours to kill.
    job_roles: dict[int, str] = {}
    for start, name in daemons.items():
        if start in spared_pids:
            continue
        for pid in descendants(start):
            job_roles.setdefault(pid, f"{name} job")
    targets = set(roles) | set(job_roles) | set(daemons)
    for start in list(roles):
        if start in spared_pids:
            continue
        targets |= descendants(start)

    found: list[Target] = []
    spared: list[Target] = []
    for pid in sorted(targets):
        if pid <= 1 or pid in tmux_pids:
            continue
        proc = by_pid.get(pid)
        argv = proc.argv if proc else []
        role = daemons.get(pid) or job_roles.get(pid) or roles.get(pid) or _role(argv)
        target = Target(pid, role, _clip(" ".join(argv)))
        if pid in spared_pids:
            spared.append(target)
        else:
            found.append(target)
    found.sort(key=lambda target: 0 if target.pid in script_pids else 1)
    return found, spared


# -- killing -----------------------------------------------------------------------

def _gone(pid: int) -> bool:
    """A pid that is gone, or a zombie (the test's own children stay zombies until reaped)."""
    stat = _read_stat(pid)
    return stat is None or stat[1] == "Z"


def _signal(pid: int, sig: int) -> bool:
    try:
        os.kill(pid, sig)
    except OSError:
        return False
    return True


def _wait_gone(pids: set[int], timeout: float) -> list[int]:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        alive = [pid for pid in pids if not _gone(pid)]
        if not alive or time.monotonic() >= deadline:
            return alive
        time.sleep(0.1)


def _tmux(args: list[str]) -> bool:
    try:
        done = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=5,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _chat_window(project: Path) -> str | None:
    """The tmux window holding the chat pane recorded in `.aaagent/chat-pane`."""
    if not shutil.which("tmux"):
        return None
    try:
        pane = attach.chat_pane_file(project).read_text().strip()
    except OSError:
        return None
    if not pane:
        return None
    try:
        done = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#{window_id}"],
                              capture_output=True, text=True, timeout=5,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


def kill_project(project: Path, *, exclude: Iterable[int] = (), grace: float = 3.0,
                 dry_run: bool = False, close_window: bool = True) -> KillResult:
    """Kill every aaagent process of one project, then close its tmux window.

    The window is closed last, and never when we were started from inside it (the dashboard,
    or the chat's Bash tool): that would kill our caller.
    """
    project = Path(project)
    targets, spared = find_targets(project, exclude)
    window = _chat_window(project) if close_window else None
    # The dashboard itself is a matching process too: only a strict ancestor means that we
    # were started from inside the aaagent chat, whose window must then stay open.
    ancestors = _ancestors(os.getpid(), {}) - {os.getpid()}
    started_from_chat = any(t.pid in ancestors for t in spared)

    if dry_run:
        return KillResult(killed=list(targets), spared=spared, survivors=[],
                          window=None if started_from_chat else window)

    killed: dict[int, Target] = {}
    for target in targets:
        if _signal(target.pid, signal.SIGTERM):
            killed[target.pid] = target
    _wait_gone({t.pid for t in targets}, grace)

    # A daemon may have started a job meanwhile; everything still alive gets SIGKILL.
    again, _ = find_targets(project, exclude)
    second: dict[int, Target] = {}
    for target in [*targets, *again]:
        if target.pid not in second and not _gone(target.pid):
            second[target.pid] = target
    for target in second.values():
        if _signal(target.pid, signal.SIGKILL):
            killed.setdefault(target.pid, target)
    if second:
        _wait_gone(set(second), 1.0)

    seen: dict[int, Target] = {}
    for target in [*targets, *again]:
        seen.setdefault(target.pid, target)
    survivors = [target for pid, target in seen.items() if not _gone(pid)]

    window = None if started_from_chat else window
    if window:
        _tmux(["kill-window", "-t", window])
    return KillResult(killed=list(killed.values()), spared=spared, survivors=survivors,
                      window=window)
