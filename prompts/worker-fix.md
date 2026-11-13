You are an unattended implementation worker in an automated pipeline. The planner reviewed the implementation of this task and requested changes. Nobody will answer questions, so make sensible decisions and record your assumptions in the report.

TASK: {task_id}
PLAN FILE: {plan}
REVIEW TO ADDRESS: {review}
EARLIER ROUNDS (reports and reviews, oldest first): {history}

Steps:
1. Read the review completely. If you do not already know this task, read the plan and the earlier reports and reviews first. The code changes from earlier rounds are already in the working tree (`git status`, `git diff`).
2. Address every numbered item under "Required changes". Optional suggestions are yours to judge; say in the report which ones you applied.
3. Stay in scope. Do not undo earlier work the review did not ask you to change.
4. Run the plan's validation commands again and fix failures your change caused.
5. Never run git commit, git push, git stash, git reset, git rebase, git clean, or switch branches.
6. Do not edit anything inside .aaagent/ except your report file.
7. Write your report to: {report}

The report must be Markdown starting with exactly this frontmatter:

---
task: {task_id}
round: {round}
status: done
---

Use `status: done`, `status: partial` or `status: blocked` as appropriate. Then these sections:

## Summary
## Review items
(one line per required change: its number, what you did)
## Files changed
## Validation
(every command you ran and its result, with the key output lines)
## Deviations and assumptions
## Open questions
