"""`aaagent` with no subcommand: open the chat with the orchestrator and start the workers."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import attach, branching, dispatch, gitutil, sessions, tasks, worker, worktrees
from .backends import find_claude
from .config import REVIEWER, ROOT, Config, project_config_path, set_toml_value

AAAGENT_BIN = ROOT / "bin" / "aaagent"
HOOKS = ROOT / "claude" / "hooks"


def unfinished_tasks(project: Path, cfg: Config) -> list[str]:
    """One status line per task that is neither committed nor abandoned."""
    return [dispatch.status_line(task, cfg) for task in tasks.all_tasks(project)
            if task.load().get("status") not in tasks.FINAL_STATUSES]


def open_branch_section(cfg: Config, project: Path) -> str:
    """The paragraph that tells a new chat about a branch that is still open; "" if none."""
    try:
        info = branching.git_status(project, cfg).get("batch")
    except Exception:  # noqa: BLE001 - a git problem must never block the chat from starting
        return ""
    if not info:
        return ""
    ids = ", ".join(info.get("tasks") or []) or "-"
    return ("\n## Open branch from an earlier chat\n"
            f"The branch {info.get('branch')} (from {info.get('base')}) is still open with "
            f"{info.get('commits') or 0} commit(s); tasks: {ids}.\n"
            "Tell the user in your first reply and ask whether to open a pull request "
            "(`aaagent pr`), finish it without one (`aaagent branch-done`), or keep working on "
            "it (new tasks join it).\n")


def orchestrator_prompt(cfg: Config, project: Path | None = None) -> str:
    text = (ROOT / "prompts" / "orchestrator.md").read_text()
    workers = ", ".join(f"{w} ({cfg.worker_model(w)})" for w in cfg.worker_names())
    text = (text.replace("{workers}", workers)
                .replace("{default_worker}", cfg.default_worker)
                .replace("{fallback_worker}", cfg.fallback_worker))
    reviewer = cfg.reviewer_worker
    if reviewer:
        note = (ROOT / "prompts" / "orchestrator-worker-review.md").read_text()
        text += note.replace("{model}", cfg.worker_label(REVIEWER))
    pending = unfinished_tasks(project, cfg) if project else []
    if pending:
        text += ("\n## Unfinished work from an earlier chat\n"
                 "These tasks were open when this chat started:\n\n"
                 + "\n".join(f"- {line}" for line in pending) + "\n")
    if project:
        if gitutil.no_git(project):
            text += (ROOT / "prompts" / "orchestrator-no-git.md").read_text()
        else:
            text += open_branch_section(cfg, project)
            if worktrees.enabled(project, cfg):
                text += (ROOT / "prompts" / "orchestrator-worktree.md").read_text()
    return text


def _hook(name: str) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(HOOKS / name))}"


def claude_settings(project: Path) -> dict:
    deny = ["Bash(git push*)", "Bash(git commit*)", "Bash(git reset*)"]
    if gitutil.no_git(project):
        # There is no repository to create: aaagent's own history lives in .aaagent/shadow.git.
        deny.append("Bash(git init*)")
    return {
        "permissions": {
            "allow": ["Bash(aaagent *)", "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)",
                      "Bash(git show*)", "Read", "Glob", "Grep"],
            "deny": deny,
        },
        "hooks": {
            "PreToolUse": [{
                "matcher": "Edit|Write|MultiEdit|NotebookEdit",
                "hooks": [{"type": "command", "command": _hook("guard_edit.py")}],
            }],
            # Remembers the chat's session id in .aaagent/chat.json for `aaagent -c`, and the
            # project's chat sessions in .aaagent/sessions.json.
            "SessionStart": [{"hooks": [{"type": "command", "command": _hook("record_session.py")}]}],
            # Copies images the prompt mentions from the user's computer (VSCode Remote-SSH),
            # and titles the chat session in .aaagent/sessions.json.
            "UserPromptSubmit": [{"hooks": [
                {"type": "command", "command": _hook("local_images.py"), "timeout": 30},
                {"type": "command", "command": _hook("record_session.py")},
            ]}],
            # `aaagent pr` asks for a new chat: restart it once the reply has ended.
            "Stop": [{"hooks": [{"type": "command", "command": _hook("chat_stop.py")}]}],
        },
    }


def claude_command(project: Path, cfg: Config, claude: str, first_prompt: str | None,
                   resume_id: str | None = None, model: str | None = None) -> list[str]:
    cmd = [claude, "--model", model or cfg.planner_model,
           "--permission-mode", "acceptEdits",
           "--settings", json.dumps(claude_settings(project)),
           "--append-system-prompt", orchestrator_prompt(cfg, project),
           "-n", f"aaagent {project.name}"]
    if resume_id:
        cmd += ["--resume", resume_id]
    extra_args = [str(a) for a in cfg.orchestrator.get("extra_args", [])]
    if cfg.planner_thinking and "--effort" not in extra_args:
        cmd += ["--effort", cfg.planner_thinking]
    cmd += extra_args
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
    """The env for the chat: in No Git mode GIT_DIR/GIT_WORK_TREE point at the shadow."""
    env = gitutil.git_env(project)
    env["AAAGENT_PROJECT"] = str(project)
    env["PATH"] = f"{ROOT / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    return env


def ensure_repo(start: Path) -> Path | None:
    """The project for `start`: a No Git folder or repository root; else ask git init or No Git."""
    project = gitutil.find_project(start)
    if project:
        return project
    print(f"{start} is not a git repository.")
    print("  g  run `git init` here (aaagent commits each task; branches and pull requests work)")
    print("  n  No Git: work in this folder without git "
          "(aaagent keeps its own history in .aaagent/shadow.git)")
    if not sys.stdin.isatty():
        print("Start aaagent in a terminal to choose, or run `git init` first.")
        return None
    answer = input("Choose [g/n], anything else cancels: ").strip().lower()
    if answer in ("g", "git"):
        subprocess.run(["git", "init", "-q"], cwd=start, check=True)
        return start
    if answer in ("n", "no", "none"):
        (start / ".aaagent").mkdir(parents=True, exist_ok=True)
        set_toml_value(project_config_path(start), "git", "branch_mode", "none")
        gitutil.ensure_shadow(start)
        print("No Git mode: aaagent keeps its own history in .aaagent/shadow.git")
        return start
    return None


def _exclude_in_the_real_repository(project: Path) -> None:
    """Add .aaagent/ to the real repository's .git/info/exclude (No Git mode).

    `gitutil.ensure_excluded` cannot do this in No Git mode: its env points at the shadow.
    The file is local and never committed.
    """
    env = {k: v for k, v in os.environ.items() if k not in gitutil.GIT_ENV_KEYS}
    proc = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=project,
                          capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        return
    git_dir = Path(proc.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = project / git_dir
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text() if exclude.exists() else ""
    if f"/{gitutil.AGENT_DIR}/" not in text.split():
        with exclude.open("a") as fh:
            fh.write(("" if text.endswith("\n") or not text else "\n")
                     + f"/{gitutil.AGENT_DIR}/\n")


def init_project(project: Path, cfg: Config) -> None:
    for sub in ("tasks", "queue", "results", "workers", "attachments", "fetch"):
        (project / ".aaagent" / sub).mkdir(parents=True, exist_ok=True)
    if gitutil.no_git(project):
        gitutil.ensure_shadow(project)
        # The real repository, when there is one, must not list .aaagent/ as untracked.
        if not cfg.track_agent_dir and gitutil.find_root(project) == project.resolve():
            _exclude_in_the_real_repository(project)
    elif not cfg.track_agent_dir:
        gitutil.ensure_excluded(project)


def launch(start: Path, first_prompt: str | None = None, use_tmux: bool = True,
           manage_workers: bool = True, resume: bool = False, model: str | None = None,
           session_ref: str | None = None) -> int:
    """Open the orchestrator chat.

    manage_workers=False: only run the chat; something else (the VSCode extension's worker
    terminals) runs the workers.
    resume=True: reopen the last aaagent chat of this project instead of starting a new one.
    model: planner model for this chat only (default: the configured one).
    session_ref: reopen this chat session (an id or id prefix from `aaagent sessions`).
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
    if session_ref:
        resume_id = sessions.find(project, session_ref)["id"]
    elif resume and not resume_id:
        print("no earlier aaagent chat for this project; starting a new one.")
    cmd = claude_command(project, cfg, claude, first_prompt, resume_id, model)
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
        worker.stop(project, REVIEWER)      # ignore False: only runs when a model reviews


def _write_chat_script(project: Path, claude_cmd: list[str], env: dict, close: str) -> Path:
    """The chat pane's command. It carries the whole system prompt, so it lives in a file to stay
    clear of tmux's argument limits. Replaced atomically: a running copy keeps reading the old one.
    """
    py, aaagent = shlex.quote(sys.executable), shlex.quote(str(AAAGENT_BIN))
    script = project / ".aaagent" / "run-orchestrator.sh"
    tmp = script.with_name(f"{script.name}.{os.getpid()}.tmp")
    tmp.write_text(f"#!/bin/sh\n{_exports(project, env, AAAGENT_TMUX_CLOSE=close)}\n"
                   f"{shlex.join(claude_cmd)}\n"
                   f"{py} {aaagent} stop >/dev/null 2>&1\n{close}\n")
    tmp.chmod(0o700)
    tmp.replace(script)
    return script


def _exports(project: Path, env: dict, **extra: str) -> str:
    pairs = {"AAAGENT_PROJECT": str(project), "PATH": env["PATH"]}
    for key in ("GIT_DIR", "GIT_WORK_TREE"):
        if key in env:
            pairs[key] = env[key]
    pairs.update(extra)
    script = "export " + " ".join(f"{k}={shlex.quote(v)}" for k, v in pairs.items()) + "; "
    if "GIT_DIR" not in env and "GIT_WORK_TREE" not in env:
        # tmux starts the pane from a shell that may have them set: never leak a stale value.
        script = "unset GIT_DIR GIT_WORK_TREE; " + script
    return script


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
    attach.chat_pane_file(project).write_text(chat_pane + "\n")
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


def respawn_chat(project: Path, chat_pane: str, resume_id: str | None, close: str,
                 first_prompt: str | None = None) -> None:
    """Restart the chat pane on an earlier conversation (the dashboard's `R` key), or on a
    fresh chat when resume_id is None (the `N` key, and `aaagent pr`'s new chat). A first
    prompt is passed as the chat command's last argument, so the fresh chat answers it."""
    cfg = Config.load(project)
    claude = find_claude(cfg)
    if not claude:
        raise RuntimeError("Claude Code CLI not found")
    env = session_env(project)
    cmd = claude_command(project, cfg, claude, first_prompt, resume_id)
    script = _write_chat_script(project, cmd, env, close)
    proc = subprocess.run(["tmux", "respawn-pane", "-k", "-t", chat_pane, "-c", str(project),
                           f"sh {shlex.quote(str(script))}"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "tmux could not restart the chat pane")


def restart_pending_chat(project: Path, delay: float = 1.0) -> bool:
    """Start a fresh chat when `aaagent pr` left `.aaagent/new-chat-pending.json` behind.

    Runs detached from the chat's Stop hook, a moment after the final reply, so the reply is
    on screen before the pane is replaced. The marker is claimed with `os.replace` first, so
    two stops that race start at most one new chat. True when a new chat was started.
    """
    if delay > 0:
        time.sleep(delay)
    pending = project / ".aaagent" / "new-chat-pending.json"
    claimed = pending.with_name(f"new-chat-claimed.{os.getpid()}.json")
    try:
        os.replace(pending, claimed)    # another restart may have claimed it first
    except OSError:
        return False
    try:
        text = claimed.read_text()
    except OSError:
        return False
    finally:
        try:
            claimed.unlink()
        except OSError:
            pass
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        prompt = None

    try:
        pane = attach.chat_pane_file(project).read_text().strip()
    except OSError:
        return False
    if not pane:
        return False
    close = os.environ.get("AAAGENT_TMUX_CLOSE") or "tmux kill-window"
    respawn_chat(project, pane, None, close, first_prompt=prompt)
    return True
