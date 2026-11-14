"""Worker backends: opencode (`opencode run`) and headless Claude Code (`claude -p`).

This module knows how to build a job's command and how to read and describe the JSON lines each
backend writes. It must not import session, dispatch, worker, tasks or models (import cycles);
config is fine.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

from .config import ROOT, Config, infer_backend

HOOKS = ROOT / "claude" / "hooks"
DIM, BOLD, RED, GREEN, RESET = "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[0m"


def color_enabled() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(code: str, text: str) -> str:
    return f"{code}{text}{RESET}" if color_enabled() else text


def _short(value, limit: int = 110) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# -- the claude binary ---------------------------------------------------------

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


def worker_claude(cfg: Config) -> str | None:
    """The claude binary for worker jobs: workers.claude_bin, else the chat's."""
    configured = cfg.worker_claude_bin
    if configured:
        return configured if Path(configured).is_file() else None
    return find_claude(cfg)


# -- the agent's system prompt ------------------------------------------------

def agent_prompt(agent: str) -> str:
    """The body of `opencode/agents/<agent>.md`, after its YAML frontmatter; "" when missing."""
    path = ROOT / "opencode" / "agents" / f"{agent}.md"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ""
    ends = [i for i, line in enumerate(lines) if line.strip() == "---"]
    if len(ends) < 2 or ends[0] != 0:
        return ""
    return "\n".join(lines[ends[1] + 1:]).strip()


# -- settings ------------------------------------------------------------------

def _hook(name: str) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(HOOKS / name))}"


def claude_settings(phase: str) -> dict:
    """The `--settings` value for a worker job."""
    guard = {"matcher": "Read|Edit|Write|MultiEdit|NotebookEdit",
             "hooks": [{"type": "command", "command": _hook("guard_worker.py")}]}
    if phase == "commit":
        permissions = {
            "allow": ["Bash(git status*)", "Bash(git diff*)", "Bash(git log*)", "Bash(git show*)",
                      "Bash(git add *)", "Bash(git restore --staged *)", "Bash(git commit *)",
                      "Bash(git rev-parse*)", "Read", "Glob", "Grep"],
            "deny": ["Edit", "Write", "MultiEdit", "NotebookEdit", "Bash(git push*)",
                     "Bash(git commit*--amend*)", "Bash(git add -A*)", "Bash(git add --all*)",
                     "Bash(git add .)", "Bash(git add *.aaagent*)"],
        }
    else:
        permissions = {
            "allow": ["Bash", "Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Glob", "Grep"],
            "deny": ["Bash(git commit*)", "Bash(git push*)", "Bash(git reset*)", "Bash(git rebase*)",
                     "Bash(git clean*)", "Bash(git stash*)", "Bash(git checkout*)",
                     "Bash(git switch*)", "Bash(sudo *)", "Bash(su *)", "Bash(systemctl *)",
                     "Bash(shutdown*)", "Bash(reboot*)", "Bash(docker rm *)", "Bash(docker rmi *)",
                     "Bash(docker stop *)", "Bash(docker kill *)", "Bash(docker volume rm *)",
                     "Bash(docker compose down*)", "Bash(docker-compose down*)"],
        }
    return {"permissions": permissions, "hooks": {"PreToolUse": [guard]}}


# -- the command ---------------------------------------------------------------

def command(job: dict, cfg: Config) -> list[str]:
    """The argv that runs `job` on its backend."""
    backend = job.get("backend") or infer_backend(job["model"])
    if backend == "claude":
        claude = worker_claude(cfg)
        if not claude:
            raise FileNotFoundError(
                "Claude Code CLI not found (set workers.claude_bin or orchestrator.claude_bin)")
        cmd = [claude, "-p", "--output-format", "stream-json", "--verbose",
               "--model", job["model"],
               "--permission-mode", "default" if job["phase"] == "commit" else "acceptEdits",
               "--settings", json.dumps(claude_settings(job["phase"])),
               "--strict-mcp-config"]
        system = agent_prompt(job["agent"])
        if system:
            cmd += ["--append-system-prompt", system]
        if job.get("thinking"):
            cmd += ["--effort", job["thinking"]]
        if job.get("session"):
            cmd += ["--resume", job["session"]]
        cmd.append(job["prompt"])
        return cmd

    model = job["model"] + ("#" + job["thinking"] if job.get("thinking") else "")
    cmd = [cfg.opencode_bin, "run", "--auto", "--agent", job["agent"], "-m", model,
           "--format", "json", "--title", f"aaagent {job['task']} {job['phase']} r{job['round']}"]
    if job.get("session"):
        cmd += ["--session", job["session"]]
    cmd.append(job["prompt"])
    return cmd


# -- one event -----------------------------------------------------------------

def _claude_error(event: dict) -> str:
    return str(event.get("result") or event.get("subtype") or "claude reported an error")


def read_event(backend: str, event: dict) -> dict:
    """Session, error and token counts of one JSON event; everything missing counts as 0/None."""
    out = {"session": None, "error": None, "input": 0, "output": 0}
    if backend == "claude":
        sid = event.get("session_id")
        if isinstance(sid, str) and sid:
            out["session"] = sid
        if event.get("type") == "result":
            usage = event.get("usage") or {}
            out["input"] = (int(usage.get("input_tokens") or 0)
                            + int(usage.get("cache_creation_input_tokens") or 0)
                            + int(usage.get("cache_read_input_tokens") or 0))
            out["output"] = int(usage.get("output_tokens") or 0)
            if event.get("is_error"):
                out["error"] = _claude_error(event)
        return out

    sid = event.get("sessionID")
    if isinstance(sid, str) and sid.startswith("ses"):
        out["session"] = sid
    if event.get("type") == "error":
        err = event.get("error") or {}
        out["error"] = err.get("message") or json.dumps(err)
    part_tokens = (event.get("part") or {}).get("tokens") or {}
    out["input"] = int(part_tokens.get("input") or 0)
    out["output"] = int(part_tokens.get("output") or 0)
    return out


# -- one readable line ---------------------------------------------------------

def describe(backend: str, event: dict, project: Path) -> str | None:
    """One or more readable output lines for a JSON event, or None."""
    if backend == "claude":
        return _describe_claude(event, project)
    return _describe_opencode(event, project)


def _describe_opencode(event: dict, project: Path) -> str | None:
    kind = event.get("type")
    part = event.get("part") or {}
    if kind == "tool_use":
        state = part.get("state") or {}
        inp = state.get("input") or {}
        target = inp.get("command") or inp.get("path") or inp.get("filePath") or inp.get("pattern") or inp
        if isinstance(target, str) and target.startswith(str(project) + "/"):
            target = target[len(str(project)) + 1:]
        line = f"  {part.get('tool', '?'):>6}  {_short(target)}"
        if state.get("status") == "error":
            return paint(RED, line + f"  ✗ {_short(state.get('error', ''), 60)}")
        return line
    if kind == "text" and part.get("text", "").strip():
        return paint(DIM, "  » " + _short(part["text"], 300))
    if kind == "error":
        err = event.get("error") or {}
        return paint(RED, f"  error: {err.get('message', err)}")
    return None


def _describe_claude(event: dict, project: Path) -> str | None:
    kind = event.get("type")
    if kind == "assistant":
        lines = []
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                inp = block.get("input") or {}
                target = (inp.get("command") or inp.get("file_path") or inp.get("path")
                          or inp.get("pattern") or inp)
                if isinstance(target, str) and target.startswith(str(project) + "/"):
                    target = target[len(str(project)) + 1:]
                lines.append(f"  {block.get('name', '?'):>6}  {_short(target)}")
            elif block.get("type") == "text" and (block.get("text") or "").strip():
                lines.append(paint(DIM, "  » " + _short(block["text"], 300)))
        return "\n".join(lines) if lines else None
    if kind == "result" and event.get("is_error"):
        return paint(RED, f"  error: {_claude_error(event)}")
    return None


# -- sessions ------------------------------------------------------------------

def session_fits(backend: str, session) -> bool:
    """Whether a stored session id belongs to this backend (a switched worker starts afresh)."""
    if not session:
        return False
    if backend == "claude":
        return not str(session).startswith("ses")
    return str(session).startswith("ses")
