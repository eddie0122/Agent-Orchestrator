#!/usr/bin/env python3
"""Stand-in for `claude -p` used by the tests.

Prints the stream-json event lines of one worker job: an `init` system event, tool and text
events, and a final `result` event. The commit job commits work.txt; an implement job appends
the model name to work.txt and writes the report.

Env:
  FAKE_CLAUDE_FAIL     comma-separated models that fail (error result event, exit 1)
  FAKE_CLAUDE_NOREPORT comma-separated models that exit 0 without writing the report
  FAKE_CLAUDE_ARGS_LOG file to append each invocation's argv (JSON) to
"""

import json
import os
import re
import subprocess
import sys
import uuid


def emit(event):
    print(json.dumps(event), flush=True)


def result(session, text, is_error=False, usage=None):
    emit({"type": "result", "subtype": "error_during_execution" if is_error else "success",
          "is_error": is_error, "result": text, "session_id": session,
          "usage": usage or {}})


def main():
    args = sys.argv[1:]
    if os.environ.get("FAKE_CLAUDE_ARGS_LOG"):
        with open(os.environ["FAKE_CLAUDE_ARGS_LOG"], "a") as fh:
            fh.write(json.dumps(args) + "\n")
    if "-p" not in args:
        return 0
    model = args[args.index("--model") + 1]
    session = args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())
    prompt = args[-1]

    if model in os.environ.get("FAKE_CLAUDE_FAIL", "").split(","):
        result(session, f"{model} unavailable", is_error=True)
        return 1
    emit({"type": "system", "subtype": "init", "session_id": session})

    if "Your only job is to create one git commit" in prompt:
        subprocess.run(["git", "add", "work.txt"], check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "commit", "-q", "-m", "feat: fake work"], check=True)
        result(session, "committed", usage={"input_tokens": 100, "output_tokens": 10})
        return 0

    with open("work.txt", "a") as fh:
        fh.write(f"{model}\n")
    emit({"type": "assistant", "session_id": session, "message": {"content": [
        {"type": "tool_use", "name": "Write", "input": {"file_path": "work.txt"}}]}})
    if model not in os.environ.get("FAKE_CLAUDE_NOREPORT", "").split(","):
        report = re.search(r"Write your report to: (\S+)", prompt).group(1)
        rnd = re.search(r"round: (\d+)", prompt).group(1)
        with open(report, "w") as fh:
            fh.write(f"---\ntask: x\nround: {rnd}\nstatus: done\n---\n## Summary\nby {model}\n")
    result(session, "done", usage={"input_tokens": 100, "output_tokens": 10})
    return 0


if __name__ == "__main__":
    sys.exit(main())
