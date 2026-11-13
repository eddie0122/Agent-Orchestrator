#!/usr/bin/env python3
"""Stand-in for `opencode run` used by the tests.

Env:
  FAKE_OC_FAIL      comma-separated models that fail (error event, exit 1)
  FAKE_OC_NOREPORT  comma-separated models that exit 0 without writing the report
  FAKE_OC_ARGS_LOG  file to append each invocation's argv (JSON) to
"""

import json
import os
import re
import subprocess
import sys
import time


def emit(event):
    print(json.dumps(event), flush=True)


def main():
    args = sys.argv[1:]
    if os.environ.get("FAKE_OC_ARGS_LOG"):
        with open(os.environ["FAKE_OC_ARGS_LOG"], "a") as fh:
            fh.write(json.dumps(args) + "\n")
    if not args or args[0] != "run":
        return 0
    model = args[args.index("-m") + 1]
    agent = args[args.index("--agent") + 1]
    session = args[args.index("--session") + 1] if "--session" in args else f"ses_fake{int(time.time() * 1000)}"
    prompt = args[-1]

    if model in os.environ.get("FAKE_OC_FAIL", "").split(","):
        emit({"type": "error", "sessionID": session, "error": {"type": "provider", "message": f"{model} unavailable"}})
        return 1
    emit({"type": "step_start", "sessionID": session, "part": {"type": "step-start"}})

    if agent == "aaagent-committer":
        subprocess.run(["git", "add", "work.txt"], check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "commit", "-q", "-m", "feat: fake work"], check=True)
        emit({"type": "text", "sessionID": session, "part": {"type": "text", "text": "committed"}})
        return 0

    with open("work.txt", "a") as fh:
        fh.write(f"{model}\n")
    emit({"type": "tool_use", "sessionID": session,
          "part": {"tool": "write", "state": {"status": "completed", "input": {"path": "work.txt"}}}})
    if model not in os.environ.get("FAKE_OC_NOREPORT", "").split(","):
        report = re.search(r"Write your report to: (\S+)", prompt).group(1)
        rnd = re.search(r"round: (\d+)", prompt).group(1)
        with open(report, "w") as fh:
            fh.write(f"---\ntask: x\nround: {rnd}\nstatus: done\n---\n## Summary\nby {model}\n")
    emit({"type": "step_finish", "sessionID": session,
          "part": {"type": "step-finish", "tokens": {"input": 100, "output": 10}}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
