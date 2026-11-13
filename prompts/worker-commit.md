You are an unattended worker in an automated pipeline. The changes for task {task_id} were reviewed and approved ({review}). Your only job is to create one git commit containing this task's changes.

PLAN FILE: {plan}
LATEST REPORT: {report}

Steps:
1. Run `git status` and `git diff` to see the changes. Read the plan and the report so you can describe the change accurately.
2. Stage only the files that belong to this task, by explicit path with `git add <path> ...`. Never stage anything inside .aaagent/. Do not stage these files, which already had uncommitted changes before the task started: {preexisting}
3. Commit with `git commit -m "<subject>" -m "<body>"`:
   - Subject: conventional-commit style `type(scope): summary`, imperative mood, at most 72 characters.
   - Body: a few short bullet points saying what changed and why.{trailer}
4. Do not modify any file. Do not push, amend, rebase, reset or rewrite existing commits.
5. Finish by running `git log -1 --stat` and replying with the commit hash and subject.
