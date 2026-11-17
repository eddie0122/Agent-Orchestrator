
## Reviews are written by the reviewer
In this project the review is not yours to write: a separate reviewer ({model}) reviews each round. This replaces steps 4 and 5 above.
- `aaagent dispatch <task>` runs implement → review → fix → review … by itself. It returns when the reviewer approved, when the review round limit is reached, or when a job failed. That can take a long time, so keep running it in the background.
- When it returns with an approval: read the latest `review-N.md` and report, look over `aaagent diff <task>`, then run `aaagent dispatch <task>` again to commit. If you find a real problem the reviewer missed, overwrite that `review-N.md` with `verdict: changes_requested` and your required changes, and dispatch again.
- When it returns with `plan_problem`: read that review, revise plan.md (widen `files:` if the fix needs other files; ask the user first if the goal itself changes), then run `aaagent dispatch <task>` again. The worker then fixes the work against the revised plan. Dispatch refuses until plan.md has changed.
- Otherwise do not write `review-N.md` yourself. If dispatch says the reviewer could not review a round, review that round yourself as in step 4.
