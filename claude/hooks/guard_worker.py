#!/usr/bin/env python3
"""Claude Code PreToolUse hook for worker jobs: no secrets, and the planner's files stay read-only.

Exit code 2 blocks the tool call and shows stderr to the model.
"""

import json
import os
import re
import sys
from pathlib import Path

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
REVIEW = re.compile(r"review-\d+\.md$")


def block(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if not isinstance(event, dict):
        return 0
    tool_input = event.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
    if not target:
        return 0
    project = Path(os.environ.get("AAAGENT_PROJECT") or event.get("cwd") or os.getcwd()).resolve()
    path = Path(str(target))
    if not path.is_absolute():
        path = Path(event.get("cwd") or project) / path
    path = path.resolve()
    reviewer = sys.argv[1:2] == ["reviewer"]

    name = path.name
    if (name == ".env" or name.startswith(".env.")) and name != ".env.example":
        return block(f"aaagent: secrets are not readable by workers, so {path} stays closed. "
                     "Tell the planner in your report if the plan needed it.")

    if event.get("tool_name") in EDIT_TOOLS:
        agent_dir = project / ".aaagent"
        rel = path.relative_to(agent_dir) if agent_dir in path.parents else None
        if reviewer:
            review_file = (rel is not None and len(rel.parts) == 3 and rel.parts[0] == "tasks"
                           and bool(REVIEW.fullmatch(rel.parts[2])))
            if not review_file:
                return block("aaagent: a reviewer does not change files; write only your review file.")
            return 0
        if rel is not None:
            planner_file = (rel == Path("config.toml")
                            or (len(rel.parts) == 3 and rel.parts[0] == "tasks"
                                and (rel.parts[2] in ("plan.md", "state.json")
                                     or bool(REVIEW.match(rel.parts[2])))))
            if planner_file:
                return block(f"aaagent: the planner's files are read-only for workers, so {path} "
                             "cannot be edited. Ask for the change in your report instead.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
