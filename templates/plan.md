---
task: {task_id}
worker:            # optional: deepseek | glm (empty = default worker)
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
