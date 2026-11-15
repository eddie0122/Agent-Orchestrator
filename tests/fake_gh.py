#!/usr/bin/env python3
"""Stand-in for `gh` (the GitHub CLI) used by the tests.

Env:
  FAKE_GH_LOG    file to append each invocation's argv (JSON) to
  FAKE_GH_FAIL   when set, print an error to stderr and exit 1
  FAKE_GH_SLEEP  seconds to sleep before answering (the timeout test)
"""

import json
import os
import sys
import time


def main():
    args = sys.argv[1:]
    log = os.environ.get("FAKE_GH_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(json.dumps(args) + "\n")
    sleep = os.environ.get("FAKE_GH_SLEEP")
    if sleep:
        time.sleep(float(sleep))
    if os.environ.get("FAKE_GH_FAIL"):
        print("error: could not create pull request", file=sys.stderr)
        return 1
    if args[:2] == ["pr", "create"]:
        print("https://github.com/example/repo/pull/1")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
