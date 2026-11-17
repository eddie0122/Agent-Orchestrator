---
task: {task_id}
worker:            # optional: a worker name such as worker2 (empty = default worker)
files:             # optional: the files this task may change, comma-separated (dir/ and globs allowed). Needed to run it at the same time as another task.
branch:            # optional, used when [git] branch_mode is branch or pr: e.g. feat/login-form or fix/crash-on-save (empty = derived from the task title)
---
<!-- TEMPLATE: replace this whole file with the plan. Remove this line. -->

# Goal
What the user wants, in one or two sentences.

# Context
What you found: relevant files and how they work, logs and error messages, conventions to follow, constraints.

# Changes
- `path/to/file`: what to change and why.

# Steps
1. ...

# Interfaces
Names, signatures, file formats or CLI output that other tasks or callers rely on and that must not change (or exactly how they change). Write "None." when nothing applies.

# Pitfalls
What a less careful implementer is likely to get wrong here: races, missing timeouts, edge cases, stale tests, conventions. Write "None." when nothing applies.

# Validation
Commands the worker must run, and what passing looks like. Always include the project's full test suite command last:
- `...`

# Done when
- Observable criteria the review will check.

# Out of scope
- What the worker must not touch.
