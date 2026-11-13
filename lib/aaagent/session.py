"""`aaagent` with no subcommand: open the chat with the orchestrator and start the workers."""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import dispatch, gitutil, tasks, worker
from .config import ROOT, Config

AAAGENT_BIN = ROOT / "bin" / "aaagent"
HOOKS = ROOT / "claude" / "hooks"


def find_claude(cfg: Config) -> str | None:
    configured = cfg.orchestrator.get("claude_bin")
    if configured:
        return configured if Path(configured).is_file() else None
    if os.environ.get("AAAGENT_CLAUDE_BIN"):
        return os.environ["AAAGENT_CLAUDE_BIN"]
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    home = Path.home()
    for cand in (home / ".local" / "bin" / "claude", home / ".claude" / "local" / "claude"):
        if cand.is_file():
            return str(cand)
    bundled = []
    for ext_root in (".vscode-server", ".vscode", ".cursor-server"):
        bundled += glob.glob(str(home / ext_root / "extensions" / "anthropic.claude-code-*" /
                                 "resources" / "native-binary" / "claude"))

    def version_key(p: str):
        m = re.search(r"claude-code-(\d+)\.(\d+)\.(\d+)", p)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
    return max(bundled, key=version_key) if bundled else None


def unfinished_tasks(project: Path, cfg: Config) -> list[str]:
    """One status line per task that is neither committed nor abandoned."""
    return [dispatch.status_line(task, cfg) for task in tasks.all_tasks(project)
            if task.load().get("status") not in tasks.FINAL_STATUSES]


def orchestrator_prompt(cfg: Config, project: Path | None = None) -> str:
    text = (ROOT / "prompts" / "orchestrator.md").read_text()
    workers = ", ".join(f"{w} ({cfg.worker_model(w)})" for w in cfg.worker_names())
    text = (text.replace("{workers}", workers)
                .replace("{default_worker}", cfg.default_worker)
                .replace("{fallback_worker}", cfg.fallback_worker))
    pending = unfinished_tasks(project, cfg) if project else []
    if pending:
        text += ("\n## Unfinished work from an earlier chat\n"
                 "These tasks were open when this chat started:\n\n"
                 + "\n".join(f"- {line}" for line in pending) + "\n")
    return text


def _hook(name: str) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(HOOKS / name))}"


def claude_settings(project: Path) -> dict:
    return {
        "permissions": {
            "allow": ["Bash(aaagent *)", "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)",
                      "Bash(git show*)", "Read", "Glob", "Grep"],
            "deny": ["Bash(git push*)", "Bash(git commit*)", "Bash(git reset*)"],
        },
        "hooks": {
            "PreToolUse": [{
                "matcher": "Edit|Write|MultiEdit|NotebookEdit",
                "hooks": [{"type": "command", "command": _hook("guard_edit.py")}],
            }],
            # Remembers the chat's session id in .aaagent/chat.json for `aaagent -c`.
            "SessionStart": [{"hooks": [{"type": "command", "command": _hook("record_session.py")}]}],
        },
    }


def claude_command(project: Path, cfg: Config, claude: str, first_prompt: str | None,
                   resume_id: str | None = None) -> list[str]:
    cmd = [claude, "--model", cfg.orchestrator.get("model", "opus"),
           "--permission-mode", "acceptEdits",
           "--settings", json.dumps(claude_settings(project)),
           "--append-system-prompt", orchestrator_prompt(cfg, project),
           "-n", f"aaagent {project.name}"]
    if resume_id:
        cmd += ["--resume", resume_id]
    cmd += [str(a) for a in cfg.orchestrator.get("extra_args", [])]
    if first_prompt:
        cmd.append(first_prompt)
    return cmd


# -- the chat aaagent last opened (written by claude/hooks/record_session.py) ----

def chat_file(project: Path) -> Path:
    return project / ".aaagent" / "chat.json"


def _usable(entry) -> str | None:
    """The entry's session id, if its conversation is still on disk."""
    if not isinstance(entry, dict) or not entry.get("session_id"):
        return None
    transcript = entry.get("transcript_path")
    return entry["session_id"] if transcript and Path(transcript).is_file() else None


def _chat_record(project: Path) -> dict:
    try:
        data = json.loads(chat_file(project).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def last_chat(project: Path) -> str | None:
    """Session id of the most recent chat that was actually used, for `aaagent -c`."""
    record = _chat_record(project)
    return _usable(record) or _usable(record.get("previous"))


def previous_chat(project: Path) -> str | None:
    """Session id of the chat before the one that is open now."""
    return _usable(_chat_record(project).get("previous"))


def session_env(project: Path) -> dict:
    env = dict(os.environ)
    env["AAAGENT_PROJECT"] = str(project)
    env["PATH"] = f"{ROOT / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    return env


def ensure_repo(start: Path) -> Path | None:
    root = gitutil.find_root(start)
    if root:
        return root
    print(f"{start} is not a git repository. aaagent needs git to diff and commit work.")
    if not sys.stdin.isatty():
        return None
    answer = input("Run `git init` here? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        return None
    subprocess.run(["git", "init", "-q"], cwd=start, check=True)
    return start


def init_project(project: Path, cfg: Config) -> None:
    for sub in ("tasks", "queue", "results", "workers"):
        (project / ".aaagent" / sub).mkdir(parents=True, exist_ok=True)
    if not cfg.track_agent_dir:
        gitutil.ensure_excluded(project)


def launch(start: Path, first_prompt: str | None = None, use_tmux: bool = True,
           manage_workers: bool = True, resume: bool = False) -> int:
    """Open the orchestrator chat.

    manage_workers=False: only run the chat; something else (the VSCode extension's worker
    terminals) runs the workers.
    resume=True: reopen the last aaagent chat of this project instead of starting a new one.
    """
    project = ensure_repo(start)
    if project is None:
        return 1
    cfg = Config.load(project)
    init_project(project, cfg)
    claude = find_claude(cfg)
    if not claude:
        print("Claude Code CLI not found. Run install.sh, or set orchestrator.claude_bin in "
              "~/.config/aaagent/config.toml.")
        return 1
    env = session_env(project)
    resume_id = last_chat(project) if resume else None
    if resume and not resume_id:
        print("no earlier aaagent chat for this project; starting a new one.")
    cmd = claude_command(project, cfg, claude, first_prompt, resume_id)
    names = cfg.worker_names()

    if not manage_workers:
        return subprocess.call(cmd, cwd=project, env=env)

    if use_tmux and shutil.which("tmux"):
        return _launch_tmux(project, cmd, names, env)

    for name in names:
        worker.start_background(project, name)
    print(f"Workers running in the background ({', '.join(names)}). "
          f"Follow tasks and workers from another terminal with `aaagent ui`.")
    try:
        return subprocess.call(cmd, cwd=project, env=env)
    finally:
        for name in names:
            worker.stop(project, name)


def _write_chat_script(project: Path, claude_cmd: list[str], env: dict, close: str) -> Path:
    """The chat pane's command. It carries the whole system prompt, so it lives in a file to stay
    clear of tmux's argument limits. Replaced atomically: a running copy keeps reading the old one.
    """
    py, aaagent = shlex.quote(sys.executable), shlex.quote(str(AAAGENT_BIN))
    script = project / ".aaagent" / "run-orchestrator.sh"
    tmp = script.with_name(f"{script.name}.{os.getpid()}.tmp")
    tmp.write_text(f"#!/bin/sh\n{_exports(project, env)}\n{shlex.join(claude_cmd)}\n"
                   f"{py} {aaagent} stop >/dev/null 2>&1\n{close}\n")
    tmp.chmod(0o700)
    tmp.replace(script)
    return script


def _exports(project: Path, env: dict, **extra: str) -> str:
    pairs = {"AAAGENT_PROJECT": str(project), "PATH": env["PATH"], **extra}
    return "export " + " ".join(f"{k}={shlex.quote(v)}" for k, v in pairs.items()) + "; "


def _launch_tmux(project: Path, claude_cmd: list[str], names: list[str], env: dict) -> int:
    """One window: the chat on the left, the dashboard (`aaagent ui`) on the right."""
    session = "aaagent-" + re.sub(r"[^A-Za-z0-9_-]", "-", project.name)
    py, aaagent = shlex.quote(sys.executable), shlex.quote(str(AAAGENT_BIN))

    inside = bool(os.environ.get("TMUX"))
    if not inside and subprocess.run(["tmux", "has-session", "-t", session],
                                     capture_output=True).returncode == 0:
        return subprocess.call(["tmux", "attach", "-t", session])

    # Workers run in the background and log to .aaagent/workers/; the dashboard shows the log.
    for name in names:
        worker.start_background(project, name)

    close = "tmux kill-window" if inside else f"tmux kill-session -t {shlex.quote(session)}"
    main = f"sh {shlex.quote(str(_write_chat_script(project, claude_cmd, env, close)))}"
    if inside:
        new = ["new-window", "-P", "-F", "#{pane_id}", "-n", "aaagent", "-c", str(project), main]
    else:
        new = ["new-session", "-d", "-P", "-F", "#{pane_id}", "-s", session, "-n", "aaagent",
               "-x", "220", "-y", "50", "-c", str(project), main]
    chat_pane = subprocess.run(["tmux", *new], capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["tmux", "set-option", "-w", "-t", chat_pane, "remain-on-exit", "off"],
                   capture_output=True)
    if not inside:
        # Our own session: let a click move between the chat and the dashboard.
        subprocess.run(["tmux", "set-option", "-t", session, "mouse", "on"], capture_output=True)

    ui_cmd = (_exports(project, env, AAAGENT_CHAT_PANE=chat_pane, AAAGENT_TMUX_CLOSE=close)
              + f"{py} {aaagent} ui; exec $SHELL")
    subprocess.run(["tmux", "split-window", "-h", "-l", "40%", "-t", chat_pane, "-c", str(project),
                    ui_cmd], check=True)
    subprocess.run(["tmux", "select-pane", "-t", chat_pane], check=True)
    if inside:
        return 0
    return subprocess.call(["tmux", "attach", "-t", session])


def respawn_chat(project: Path, chat_pane: str, resume_id: str, close: str) -> None:
    """Restart the chat pane on an earlier conversation (the dashboard's `R` key)."""
    cfg = Config.load(project)
    claude = find_claude(cfg)
    if not claude:
        raise RuntimeError("Claude Code CLI not found")
    env = session_env(project)
    cmd = claude_command(project, cfg, claude, None, resume_id)
    script = _write_chat_script(project, cmd, env, close)
    proc = subprocess.run(["tmux", "respawn-pane", "-k", "-t", chat_pane, "-c", str(project),
                           f"sh {shlex.quote(str(script))}"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "tmux could not restart the chat pane")
