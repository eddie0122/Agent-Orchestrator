#!/usr/bin/env python3
"""Claude Code Stop hook: replace the chat when `aaagent pr` asked for a new one.

`aaagent pr` (git mode `pr-auto`) writes <project>/.aaagent/new-chat-pending.json before the
planner's final reply ends. This hook runs once the reply has ended and hands the request to
whatever can restart the chat:

- in VSCode it becomes <project>/.aaagent/new-chat.json, which the extension watches;
- in the tmux layout a detached `chat_restart.py` restarts the chat pane (it must not run
  inside this hook: the restart kills this chat);
- in a plain terminal there is nothing to restart, so the marker is dropped.

It never blocks or fails the chat: every failure is ignored and it exits 0.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent


def chat_pane_exists(project: Path) -> bool:
    """True when this hook runs inside the tmux pane recorded for this project's chat.

    A restart runs `tmux respawn-pane -k`, which kills whatever is in that pane, so the
    recorded pane is only trusted when this process really lives in it: tmux sets TMUX_PANE
    for every process in a pane, and Claude and its hooks inherit it. A stale
    `.aaagent/chat-pane` (say, after a tmux server restart) can name an unrelated live pane,
    and a plain terminal must never restart it.
    """
    try:
        pane = (project / ".aaagent" / "chat-pane").read_text().strip()
    except OSError:
        return False
    if not pane or os.environ.get("TMUX_PANE") != pane:
        return False
    if not shutil.which("tmux"):
        return False
    try:
        shown = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#{pane_id}"],
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return shown.returncode == 0 and shown.stdout.strip() == pane


def drain_stdin() -> None:
    """Claude Code writes the event on stdin; read it so it never sees a broken pipe."""
    try:
        sys.stdin.read()
    except (OSError, ValueError):
        pass


def restart_detached(project: Path) -> None:
    subprocess.Popen([sys.executable, str(HOOKS / "chat_restart.py"), str(project)],
                     start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    drain_stdin()
    project = os.environ.get("AAAGENT_PROJECT")
    if not project:
        return 0
    root = Path(project)
    agent_dir = root / ".aaagent"
    pending = agent_dir / "new-chat-pending.json"
    if not pending.is_file():
        return 0

    if os.environ.get("AAAGENT_VSCODE") == "1":
        pending.replace(agent_dir / "new-chat.json")
        return 0

    if chat_pane_exists(root):
        restart_detached(root)
        return 0

    # A plain terminal: nothing can restart the chat, so the marker must not fire later.
    pending.unlink()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - a chat hook must never block the chat
        sys.exit(0)
