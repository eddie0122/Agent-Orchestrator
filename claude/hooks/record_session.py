#!/usr/bin/env python3
"""Claude Code SessionStart hook: remember which conversation is the aaagent chat.

Writes <project>/.aaagent/chat.json so `aaagent -c` can reopen the last chat, and keeps the one
before it as `previous`. It never blocks the chat: every failure is ignored and it exits 0.
"""

import json
import os
import sys
import time
from pathlib import Path

KEEP = ("session_id", "transcript_path", "at")


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
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - a bookkeeping hook must never break the chat
        sys.exit(0)
