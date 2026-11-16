
## This project runs without git
`[git] branch_mode` is `none` (No Git): this folder is not under git, or aaagent must not touch its git. aaagent keeps its own history in `.aaagent/shadow.git`. `git status`, `git diff` and `git log` in this chat and in the workers read that history, not a real repository.
- There are no branches and no pull requests. Leave `branch:` empty in plans, and never run `aaagent pr`, `aaagent branch-done`, `git init` or `git commit`.
- When you dispatch an approved task, aaagent records a checkpoint itself (no worker commit job). In step 6, report the checkpoint id that dispatch prints instead of a commit hash.
- `aaagent diff <task>` shows a task's changes, also after its checkpoint.
