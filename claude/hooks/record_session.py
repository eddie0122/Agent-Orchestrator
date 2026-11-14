#!/usr/bin/env python3
"""Claude Code session hook: remember the chats of this project.

On SessionStart it writes <project>/.aaagent/chat.json so `aaagent -c` can reopen the last chat,
and keeps the one before it as `previous`. On UserPromptSubmit it only titles the chat session.
Both events also keep <project>/.aaagent/sessions.json up to date (`aaagent sessions`).
It never blocks the chat: every failure is ignored and it exits 0.
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

KEEP = ("session_id", "transcript_path", "at")


def record_chat(agent_dir: Path, event: dict, session_id: str) -> None:
    """The chat.json record behind `aaagent -c` and the dashboard's `R` key."""
    target = agent_dir / "chat.json"
    try:
        old = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError):
        old = {}
    if not isinstance(old, dict):
        old = {}

    previous = old.get("previous")
    if old.get("session_id") and old["session_id"] != session_id:
        # Only a chat that was really used (it has a transcript) is worth going back to.
        transcript = old.get("transcript_path")
        if transcript and Path(transcript).is_file():
            previous = {k: old.get(k) for k in KEEP}
    record = {
        "session_id": session_id,
        "transcript_path": event.get("transcript_path"),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "previous": previous,
    }
    tmp = target.with_name(f"chat.json.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(record, indent=2) + "\n")
        tmp.replace(target)
    except OSError:
        pass


def record_session(project: Path, event: dict, session_id: str, prompt: str | None) -> None:
    """sessions.json: start or resume a chat, or note a prompt and title it."""
    try:
        from aaagent import sessions
    except Exception:  # noqa: BLE001 - a bookkeeping hook must never break the chat
        return
    if event.get("hook_event_name") == "UserPromptSubmit":
        sessions.record_prompt(project, session_id, prompt or "")
    else:
        sessions.record_start(project, session_id, event.get("transcript_path"))


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    session_id = event.get("session_id") if isinstance(event, dict) else None
    if not session_id:
        return 0
    project = Path(os.environ.get("AAAGENT_PROJECT") or event.get("cwd") or os.getcwd())
    agent_dir = project / ".aaagent"
    if not agent_dir.is_dir():
        return 0

    if event.get("hook_event_name") == "UserPromptSubmit":
        # The prompt belongs to a chat that chat.json already knows about.
        try:
            record_session(project, event, session_id, event.get("prompt"))
        except Exception:  # noqa: BLE001
            pass
        return 0

    try:
        record_chat(agent_dir, event, session_id)
    except Exception:  # noqa: BLE001
        pass
    try:
        record_session(project, event, session_id, None)
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - a bookkeeping hook must never break the chat
        sys.exit(0)
