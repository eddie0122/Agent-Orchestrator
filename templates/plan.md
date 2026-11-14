---
task: {task_id}
worker:            # optional: a worker name such as worker2 (empty = default worker)
files:             # optional: the files this task may change, comma-separated (dir/ and globs allowed). Needed to run it at the same time as another task.
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

# Validation
Commands the worker must run, and what passing looks like:
- `...`

# Done when
- Observable criteria the review will check.

# Out of scope
- What the worker must not touch.
