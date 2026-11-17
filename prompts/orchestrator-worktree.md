
## Tasks run in their own worktrees
`[parallel] isolation` is `worktree`: each task works in its own git worktree (`.aaagent/worktrees/<task>`, branch `aaagent/<task>`) until its commit is applied to the session branch.
- `aaagent diff <task>` shows the worktree's changes. To run validation yourself, run the commands inside that folder (for example `cd .aaagent/worktrees/<task> && <command>`); the project's own working tree does not have the task's changes before its commit.
- Tasks still declare disjoint `files:` to run at the same time.
- When dispatch reports that a commit could not be applied to the branch, tell the user what is in the way; after it is resolved, `aaagent dispatch <task>` retries only the merge.
