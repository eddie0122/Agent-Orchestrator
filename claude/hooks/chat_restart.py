#!/usr/bin/env python3
"""Restart the tmux chat on a fresh one, detached from the Stop hook (`chat_stop.py`).

The restart kills the chat that ran the hook, so it happens here, in a process of its own.
Every failure is appended to <project>/.aaagent/workers/chat-restart.log instead of being
shown, because nobody is looking at this process' output.
"""

import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))


def log(project: Path, text: str) -> None:
    try:
        folder = project / ".aaagent" / "workers"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "chat-restart.log").open("a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {text}\n")
    except OSError:
        pass


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    project = Path(sys.argv[1])
    try:
        from aaagent import session
        session.restart_pending_chat(project)
    except Exception:  # noqa: BLE001 - the log is the only place a failure shows up
        log(project, "could not restart the chat:\n" + traceback.format_exc())
    return 0


if __name__ == "__main__":
    sys.exit(main())
