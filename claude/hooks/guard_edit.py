#!/usr/bin/env python3
"""Claude Code PreToolUse hook: the orchestrator may only write inside .aaagent/.

Exit code 2 blocks the tool call and shows stderr to the model.
"""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    tool_input = event.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
    if not target:
        return 0
    project = Path(os.environ.get("AAAGENT_PROJECT") or event.get("cwd") or os.getcwd()).resolve()
    path = Path(target)
    if not path.is_absolute():
        path = Path(event.get("cwd") or project) / path
    path = path.resolve()

    allowed = [project / ".aaagent", Path.home() / ".claude"]
    if any(path == a or a in path.parents for a in allowed):
        return 0
    print(
        f"aaagent: you are the planner and reviewer, so you cannot edit {path}. "
        f"Put the change in a plan (`aaagent new \"<title>\"`, write plan.md, `aaagent dispatch <task>`) "
        f"or in a review's required changes, and a worker will implement it.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
