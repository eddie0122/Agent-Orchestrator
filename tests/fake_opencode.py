#!/usr/bin/env python3
"""Stand-in for `opencode` used by the tests.

`models` prints a short list of model ids for `aaagent model`; `api GET /api/model` prints
their thinking variants for `aaagent thinking`; `run` behaves like a job (`-m model#variant`
is accepted and the variant is ignored for the job's behaviour).

Env:
  FAKE_OC_FAIL      comma-separated models that fail (error event, exit 1)
  FAKE_OC_NOREPORT  comma-separated models that exit 0 without writing the report
  FAKE_OC_REVIEW    comma-separated verdicts for review jobs, by round (default "approved")
  FAKE_OC_NOREVIEW  comma-separated models that exit 0 without writing the review
  FAKE_OC_REVIEW_SLEEP  seconds a review job sleeps before writing the review (default 0)
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


def plan_files(prompt):
    """The plan's `files:` entries, so a scoped task writes and commits its own files."""
    found = re.search(r"PLAN FILE: (\S+)", prompt)
    if not found:
        return []
    try:
        text = open(found.group(1), errors="replace").read()
    except OSError:
        return []
    if not text.startswith("---"):
        return []
    for line in text.splitlines()[1:]:
        if line.strip() == "---":
            break
        if line.split(":", 1)[0].strip().lower() == "files":
            raw = line.split(":", 1)[1].split("#", 1)[0]
            return [entry.strip().strip("\"'") for entry in raw.split(",") if entry.strip()]
    return []


def write_target(prompt):
    """The file a scoped task's implement round writes; `work.txt` otherwise."""
    for entry in plan_files(prompt):
        if entry.endswith("/") or any(ch in entry for ch in "*?["):
            continue
        return entry
    return "work.txt"


def main():
    args = sys.argv[1:]
    if os.environ.get("FAKE_OC_ARGS_LOG"):
        with open(os.environ["FAKE_OC_ARGS_LOG"], "a") as fh:
            fh.write(json.dumps(args) + "\n")
    if args and args[0] == "models":
        for name in ("opencode-go/deepseek-v4.1-flash", "opencode-go/glm-5.3-flash",
                     "opencode-go/kimi-k3"):
            print(name)
        return 0
    if args[:3] == ["api", "GET", "/api/model"]:
        print(json.dumps({"data": [
            {"id": "deepseek-v4.1-flash", "providerID": "opencode-go",
             "variants": [{"id": "low"}, {"id": "high"}, {"id": "max"}]},
            {"id": "glm-5.3-flash", "providerID": "opencode-go",
             "variants": [{"id": "low"}, {"id": "high"}, {"id": "max"}]},
            {"id": "kimi-k3", "providerID": "opencode-go",
             "variants": [{"id": "low"}, {"id": "high"}, {"id": "max"}]},
        ]}))
        return 0
    if not args or args[0] != "run":
        return 0
    model_arg = args[args.index("-m") + 1]
    model = model_arg.split("#", 1)[0]  # `-m provider/model#variant`
    agent = args[args.index("--agent") + 1]
    session = args[args.index("--session") + 1] if "--session" in args else f"ses_fake{int(time.time() * 1000)}"
    prompt = args[-1]

    if model in os.environ.get("FAKE_OC_FAIL", "").split(","):
        emit({"type": "error", "sessionID": session, "error": {"type": "provider", "message": f"{model} unavailable"}})
        return 1
    emit({"type": "step_start", "sessionID": session, "part": {"type": "step-start"}})

    if agent == "aaagent-committer":
        paths = [entry.rstrip("/") for entry in plan_files(prompt) if os.path.exists(entry.rstrip("/"))]
        subprocess.run(["git", "add", "--", *(paths or ["work.txt"])], check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "commit", "-q", "-m", "feat: fake work"], check=True)
        emit({"type": "text", "sessionID": session, "part": {"type": "text", "text": "committed"}})
        return 0

    if agent == "aaagent-reviewer":
        if model in os.environ.get("FAKE_OC_NOREVIEW", "").split(","):
            return 0
        delay = float(os.environ.get("FAKE_OC_REVIEW_SLEEP") or 0)
        if delay > 0:
            time.sleep(delay)
        review_file = re.search(r"Write your review to: (\S+)", prompt).group(1)
        rnd = int(re.search(r"review-(\d+)\.md", review_file).group(1))
        verdicts = os.environ.get("FAKE_OC_REVIEW", "").split(",")
        verdict = (verdicts[rnd - 1].strip()
                   if rnd - 1 < len(verdicts) and verdicts[rnd - 1].strip() else "approved")
        with open(review_file, "w") as fh:
            fh.write(f"---\nverdict: {verdict}\nround: {rnd}\n---\n"
                     f"## Assessment\nby {model}\n## Required changes\n1. more\n")
        emit({"type": "step_finish", "sessionID": session,
              "part": {"type": "step-finish", "tokens": {"input": 100, "output": 10}}})
        return 0

    target = write_target(prompt)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "a") as fh:
        fh.write(f"{model}\n")
    emit({"type": "tool_use", "sessionID": session,
          "part": {"tool": "write", "state": {"status": "completed", "input": {"path": target}}}})
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
