# aaagent

Chat with Claude Opus in your terminal. Opus plans and reviews; cheaper opencode workers implement and fix, and aaagent commits approved tasks itself.

```
you ──chat──► Opus (Claude Code, Pro subscription): the planner
                 │ 1. investigates, writes  .aaagent/tasks/001-x/plan.md
                 │ 2. aaagent dispatch 001
                 │      ├─► worker implements          → report-1.md
                 │      ├─► reviewer reviews           → review-1.md  (approved / changes_requested / plan_problem)
                 │      └─► worker fixes → reviewer …  (repeats until approved or 3 rounds)
                 │ 3. checks the result, aaagent dispatch 001 ──► commits the task itself
                 ▼
              tells you what changed and the commit hash
```

Without a reviewer model the planner writes each review itself and dispatches every round (see Who reviews).

Workers (OpenCode Go subscription) are generic slots: `worker1`, `worker2`, `worker3`, … — any number of them, each running any model. You add and remove them with `aaagent workers add|remove`. The starting set:

| worker | model | role |
|---|---|---|
| worker1 | `opencode-go/deepseek-v4.1-flash` | default implementer |
| worker2 | `opencode-go/glm-5.3-flash` | takes over when worker1 fails or keeps failing review; can be chosen per task |

### Adding and removing workers

`aaagent workers` lists every worker with its model and role. `aaagent workers add <model>` adds the next free slot (`worker3`, …) with that model, `aaagent workers remove <name>` removes one (not while it has a queued or running job), and `aaagent workers default <name>` / `aaagent workers fallback <name>` choose who implements by default and who takes over on failure — add `--project` to keep the change in this project's config. On a terminal, `workers add` without a model shows a pick list. The two default workers above are only a starting set: once your config defines any worker of its own, they are replaced by it, and `workers add` writes them out first so they cannot vanish by accident. Configs that still name workers `deepseek`/`glm` are renamed to `worker1`/`worker2` automatically.

### Claude as a worker

A worker runs on opencode when its model id has a slash (`provider/model`) and on headless Claude Code (`claude -p`) when it is a Claude model name, so `aaagent model worker2 sonnet` moves that worker to Claude. Say it explicitly with `backend = "opencode"` or `backend = "claude"` in the worker's config:

```toml
[workers.claude]
model = "sonnet"      # thinking = "high" sets claude --effort
```

Claude workers use the same Claude subscription and usage limit as the chat. `workers.claude_bin` points them at another binary; empty means the chat's.

### Who reviews

By default the planner reviews every round itself: you run `aaagent dispatch` for the implement/fix round, read the report, write `review-N.md`, and dispatch again. Give the reviewer its own model and one dispatch does the whole loop by itself:

```toml
[reviewer]
model = "sonnet"          # a Claude model name runs on headless Claude Code
# model = "opencode-go/glm-5.3-flash"   # an opencode id (provider/model) runs on opencode
# thinking = "high"       # claude --effort, or the opencode model variant
# parallel = 2            # reviews at the same time (1–8), useful with parallel tasks
# model = "planner"       # the default: the chat (the planner) reviews each round itself
```

The reviewer is a role of its own, with its own model, thinking level, queue, daemon and log (`.aaagent/workers/reviewer.log`, `aaagent logs reviewer`) — never one of the worker slots. Like a worker, it runs on headless Claude Code when its model is a Claude name and on opencode when the id has a slash.

`aaagent reviewer sonnet` sets it (add `--project` to write this project's config only), `aaagent reviewer opencode-go/glm-5.3-flash --thinking high` sets model and level, `aaagent reviewer worker2` copies worker2's model and thinking into `[reviewer]` (a snapshot: changing worker2 later does not change the reviewer), `aaagent reviewer planner` gives the job back to the chat, and `aaagent reviewer` shows the current one. `aaagent model reviewer <model>` and `aaagent thinking reviewer <level>` work too. In the dashboard press `v`, in VSCode run **Change Reviewer…** in the Reviewer view (a typed model id sets it directly). An old `[flow] reviewer = "<worker>"` is still honoured until you set the reviewer again, which clears that key in the written file.

With a reviewer model, `aaagent dispatch <task>` runs implement → review → fix → review … and returns when the reviewer approved, when the review round limit is reached, or when a job failed. A reviewer that finds the plan itself wrong writes `verdict: plan_problem`: the loop stops, the chat revises `plan.md`, and the next dispatch runs a fix round against the revised plan (it refuses until the plan has changed). The reviewer job cannot change any file but its `review-N.md`. The commit still needs one more `aaagent dispatch` by the planner, which is the final gate: it can overrule the approval by rewriting the review. If the reviewer fails (or writes no valid verdict) it is retried once on the reviewer itself; if that fails too, the round comes back to the chat to review itself.

With `[reviewer] parallel = N` (1–8, default 1) the reviewer daemon runs up to N reviews at the same time, so the reviews of several parallel tasks do not wait for each other; its output lines are prefixed with the task number. Workers always run one job at a time.

### What workers and reviewers are told

- **Check the plan against the code first.** A worker reads the plan, then every file and symbol it names, and records in its report where the plan and the code disagree.
- **Run the full test suite at the end**, not only the plan's own commands, and fix what its change broke. If the same check still fails after three different attempts, it stops and reports `status: partial`.
- **Report status** is `done` (implemented, validation passes), `partial` (part of it; the failing check and the attempts are named) or `blocked` (it could not proceed; the plan is wrong).
- **The report ends with `## Commit message`:** a conventional-commit subject line (`type(scope): summary`, imperative, at most 72 characters), an empty line, then short `- ` bullets saying what changed and why. aaagent uses it to commit the approved task itself; without a valid one a commit job does it instead.
- **Plan template sections:** Goal, Context, Changes, Steps, Interfaces, Pitfalls, Validation (the full suite last), Done when, Out of scope.
- **The reviewer does not trust the report.** It re-runs the plan's validation and the full test suite itself, checks every "Done when" criterion, and looks for bugs and scope creep. From round 2 on it stays consistent: it only repeats items still unaddressed, failures introduced since the last round, or serious bugs it missed before.
- **`plan_problem`** is for a plan that is itself wrong — impossible or contradictory, or its `files:`/Out of scope rule forbids the only correct fix. The reviewer then says exactly what the planner must change instead of asking the worker for a workaround.

### Project lessons

The chat keeps `.aaagent/lessons.md`: one line per mistake reviewers caught that is likely to recur, project conventions, the right test command. aaagent appends this file to every implement, fix and review prompt (up to 6000 characters), so workers and reviewers learn from earlier tasks. Edit it by hand too; delete it to turn the feature off.

### Integration check

Each task is reviewed on its own. To catch problems that only show when the tasks of a branch are
combined, set a command that checks the whole project:

```toml
[integration]
command = "python3 -m unittest discover -s tests -q"
timeout_minutes = 20
```

aaagent runs it after the last task on the branch is committed. When it fails, the chat is told not
to open the pull request and plans a fix task instead; the check runs again after that task.
`aaagent pr` and `aaagent branch-done` also run it when it has not passed for the current commit, and
refuse while it fails. Empty (the default) turns the check off. It runs only with a session branch
(`branch_mode` `branch`, `pr` or `pr-auto`).

### Check after each round

The reviewer is told to run the tests itself, which costs it time on every round. A deterministic
check does it instead, right after each implement or fix round and before the review:

```toml
[check]
command = "python3 -m unittest discover -s tests -q"   # "" = the [integration] command
enabled = true
on_fail = "review"                                     # or "fix"
timeout_minutes = 0                                    # 0 = the [integration] timeout
```

aaagent runs the command in the task's own tree, saves the result to
`.aaagent/tasks/<task>/check-N.md` and passes it to the reviewer. With `on_fail = "fix"` a
failing check rejects the round at once (aaagent writes `review-N.md` itself with
`changes_requested`) and the next round is a fix, without spending a review job; a report with
`status: blocked` always goes to a reviewer. With the default `on_fail = "review"` the review
runs as usual and only warns the planner when an approved round's check failed. On empty
commands (the default) dispatch behaves exactly as before. When several tasks share one working
tree the check is skipped, because it would see their changes too.

## Install

```bash
cd /mnt/HDD1/docker/auto_agent
./install.sh
```

The installer is safe to re-run. It:

- checks python 3.11+ and git
- installs opencode and the Claude Code CLI if they are missing, and asks you to log in if needed
- installs tmux with apt, which asks for your sudo password
- registers the `aaagent-worker`, `aaagent-committer` and `aaagent-reviewer` agents in `~/.config/opencode/agents/`
- links `aaagent` into `~/.local/bin`
- builds and installs the VSCode extension (skip it with `--no-vscode`)
- runs `aaagent doctor`

It also cleans up an install from before the rename (the `aa` command, `aa-*` agents, `~/.config/auto-agent/`).

`./uninstall.sh` removes the links and the VSCode extension. It leaves your config and logins alone.

## Use in VSCode

After installing, run **Developer: Reload Window** once. Then:

1. Open a project folder (a git repository, or any folder in No Git mode) and click the **aaagent** icon in the activity bar.
2. Click **New Session** (or run `aaagent: Ask Opus…` from the command palette and type the request). **New Session** closes the open chat and starts a fresh one; **Restore Previous Session…** reopens an earlier one, and **Open Chat** is in the Sessions view's `…` menu.
   The Opus chat opens as an editor tab, and each worker (and the reviewer, when a model reviews) opens as a split terminal in the panel.
3. Talk to Opus. The sidebar follows along:
   - **Planner**: the chat's Claude model and thinking level. Click the model to change it.
   - **Reviewer**: who reviews each round — the planner (the chat) itself, or any Claude/opencode model. Click **Change Reviewer…** to switch, and while a model reviews, its thinking level, output terminal and **Stop Reviewer** are here too.
   - **Workers**: who is running what. Add a worker with **+** (**Add Worker…**), and right-click a worker to change its model or thinking level, use it as the default, or remove it. Inline buttons show a worker's output or stop it.
   - **Sessions**: each Opus chat session, newest first, with the tasks it created. Expand one to see its tasks; the ↻ button on a session reopens it, and right-click → **Delete Session…** (or the trash button) deletes a session that is not the open chat. **New Session**, **Restore Previous Session…**, **Ask Opus…** and **Attach Image…** are in its title bar.
   - **Tasks**: all tasks, newest first, with the current stage (planning, implementing, in review, committed…). Expand a task to open its `plan.md`, `report-N.md` and `review-N.md`, or **Changes** for the diff.
   - The **status bar** shows what is happening now. A notification appears when a task is committed or needs your decision.

Other handy commands:

- **Select Model…**/**Select Thinking Level…** (Workers view `…` menu, or the command palette): change the planner's, the reviewer's or a worker's model or thinking level, from a list or typed, for all projects or just this one. Each job's own view has a direct button for it (**Change Planner Model…**, **Change Reviewer Thinking Level…**, or right-click a worker → **Change Model…**/**Change Thinking Level…**).
- **Change Reviewer…** (Reviewer view title): choose who reviews each round: the planner (the chat) or any Claude/opencode model, from a list or typed, for all projects or just this one.
- **Add Worker…** (Workers view title): pick or type a model for a new worker; it gets its own terminal like the others.
- **Remove Worker…** and **Use as Default Worker** (right-click a worker): remove a worker (after a confirmation; its config and terminal go, its tasks keep their history) or make it the default implementer.
- **Send Selection to Chat** (editor right-click): adds an `@file#L10-20` reference to the chat input.
- **Attach Image…** (Sessions view title): paste a screenshot (Ctrl+V / ⌘V), hold **Shift** and drop image files, or click **Choose files…**. The image is saved to `.aaagent/attachments/` and its `@`-mention is typed into the chat input, ready for you to add text and press Enter. This works over Remote-SSH too: the files are read on your machine and sent to the server. Dropping a file on the chat terminal (hold Shift) also works over Remote-SSH, Windows PCs included: the path from your computer is detected and aaagent copies the file to the server. Any file type up to 5 MB; images are validated as PNG, JPEG, GIF or WebP.
- **Attach to aaagent Chat** (Explorer or editor tab right-click on an image): types that image's `@`-mention into the chat input.
- **Show Task Changes**, **Abandon Task…**, **Stop All Workers**, **Check Installation**, **Open Configuration**.

Settings: `aaagent.chatLocation` (`editor` or `panel`), `aaagent.notifications` (`all`, `important`, `off`), `aaagent.finishedTasksShown`, `aaagent.path`.

## Use in a terminal

```bash
cd ~/my-project    # any git repository (aaagent offers `git init` or No Git if it isn't one)
aaagent            # open the chat
aaagent "add a /health endpoint with a test"   # open the chat with a first request
aaagent -c         # reopen this project's last chat
```

With tmux installed, `aaagent` opens one window: the Opus chat on the left and the dashboard on the right. Click a pane, or press `Ctrl-b o`, to move between them. Without tmux, the workers run in the background; run `aaagent ui` in another terminal for the dashboard. Quitting the chat stops the workers.

```
┌ Opus chat ─────────────────────┬ aaagent · my-project ─────────────────┐
│                                │ PLANNER                               │
│ > add a /health endpoint       │  planner                  opus · high │
│                                │ REVIEWER                              │
│                                │  reviewer          planner (the chat) │
│                                │ WORKERS                               │
│                                │  worker1      waiting · glm-5.3-flash │
│                                │  worker2      stopped · fallback      │
│                                │  + add worker                         │
│                                │ SESSIONS                              │
│                                │  ▾ add a /health endpoint  open · 2 t…│
│                                │     003 add health        / fixing …  │
│                                │  · fix login              2 h ago     │
│                                │ TASKS                                 │
│                                │  ▾ 003 add health  / fixing · worker1│
│                                │     plan.md                           │
│                                │     report-1.md                   done│
│                                │     review-1.md      changes requested│
│                                │  ▸ 002 fix login     committed a1b2c3d│
│                                │ ── worker1 output (following) ─────── │
│                                │     edit  app.py                      │
└────────────────────────────────┴───────────────────────────────────────┘
```

Dashboard keys:

| key | does |
|---|---|
| `↑` `↓` / `j` `k` | move |
| `enter` | task: show or hide its plan, reports and reviews · session: show its tasks · a session's task: jump to it in TASKS · file: read it · worker: show its output · reviewer: show its output (or, while the planner reviews, choose its model) · `+ add worker`: add one |
| `d` | the selected task's diff (also from a session's task) |
| `l` | choose whose output the bottom pane shows (a worker's or the reviewer's; default: whoever is working) |
| `m` | change a model: the selected row's (planner, reviewer or worker), or pick which one; type a name if it is not listed |
| `t` | the same for a thinking level (the reviewer has one only while a model reviews) |
| `v` | choose who reviews each round: the planner (the chat) or a model (Claude or opencode); type a name if it is not listed |
| `B` | git branch mode: current branch, new branch, or new branch + pull request (see Branches and pull requests) |
| `+` | add a worker, after picking its model |
| `-` | remove the selected worker, after a y/n question |
| `a` | abandon the selected task, after a y/n question |
| `o` | open the selected session in the chat pane, after a y/n question (shown for a session row in aaagent's tmux layout) |
| `x` | delete the selected session and its conversation, after a y/n question |
| `N` | start a new session (closes this chat; shown in aaagent's tmux layout) |
| `R` | restore an earlier session, picked from a list (shown in aaagent's tmux layout) |
| `K` | kill every aaagent process of this project: chat, workers, reviewer and running jobs, after a y/n question |
| `?` | all keys · `q` closes a view, or quits the dashboard |

**SESSIONS** lists the chat sessions newest first, the open one marked `open`, with the tasks each one created. `enter` on a session shows those tasks; `enter` on one of them jumps to it in TASKS, so its plan, reports, reviews and diff are there too. `N` starts a new session, `R` picks an earlier one from a list, and `o` reopens the one that is selected.

The dashboard beeps and says so in its bottom line when a task is committed or needs you.

`K` (or `aaagent kill` in any terminal) is the emergency stop: it kills the chat, the workers, the reviewer, their running jobs and background dispatches, and closes aaagent's tmux window. Queued jobs and task statuses stay as they are, so `aaagent dispatch` can pick them up again later; `aaagent kill --dry-run` only lists what it would kill.

### Sessions

Each `aaagent` starts a new chat session, and a session can create many tasks: `aaagent status` shows each task's session, and `aaagent sessions` lists the sessions with the tasks they created. `aaagent --session <id>` reopens one (the id, or a unique prefix of it, is in that list); `aaagent -c` reopens the last one for this project, and `/resume` inside the chat lists older ones. aaagent remembers its own chats in `.aaagent/sessions.json` (and the last one in `.aaagent/chat.json`), so `-c` never picks up an unrelated Claude Code conversation from the same folder. When a new chat starts while tasks are unfinished, Opus is told about them and asks you whether to continue.

`aaagent sessions delete <id>` (an id or id prefix; several ids allowed) removes a session from the list and deletes its Claude Code conversation too, so the list does not grow forever; `--keep-conversation` only forgets the session and leaves the conversation on disk. `aaagent sessions delete --gone` clears every session whose conversation is already gone. The current chat's session needs `--force` (close the chat first), and the tasks a session created stay.

To start a fresh conversation in the middle of things, press `N` in the dashboard (in the tmux layout) or type `/clear` in the chat; quitting and running `aaagent` again does the same.

Just talk to Opus. Questions get a direct answer; change requests go through the pipeline. You can steer at any point ("use worker2 for this", "stop after the plan", "split it into two tasks").

### Commands

Opus runs these itself. They are also useful for you:

| command | what it does |
|---|---|
| `aaagent ui` | the dashboard, in any terminal |
| `aaagent status [--all]` | workers and tasks, with each task's next step |
| `aaagent sessions [--all]` | this project's chat sessions, with the tasks each created |
| `aaagent sessions delete <id>… [--gone]` | delete chat sessions (and their conversations) |
| `aaagent diff <task> [--stat]` | everything changed since the task started |
| `aaagent logs <worker> [-f]` | worker output |
| `aaagent attach <file\|-> […] [--no-send]` | copy images into `.aaagent/attachments/` and add them to the chat |
| `aaagent new "<title>"` | create a task folder with a plan template |
| `aaagent workers` | list the workers (name, model, role); `--json` for machines |
| `aaagent workers add [<model>]` | add the next free worker slot (pick from a list on a terminal) |
| `aaagent workers remove <name>` | remove a worker (not while it has queued or running jobs) |
| `aaagent workers default\|fallback <name>` | choose the default implementer and the fallback |
| `aaagent dispatch <task> [--worker worker2] [--force]` | run the next phase (implement / fix / commit; with a reviewer model the whole implement → review → fix loop) and wait |
| `aaagent wait <task>` | reattach to a running job |
| `aaagent abandon <task>` | drop a task (its uncommitted changes stay; a worktree task keeps its worktree) |
| `aaagent stop` | stop this project's workers |
| `aaagent kill [--dry-run]` | kill every aaagent process of this project: the chat, workers, reviewer, running jobs and dispatches; closes aaagent's tmux window (dashboard key `K`) |
| `aaagent model [<target> [<model>]]` | show, list (`--list`), pick or set the planner, reviewer and worker models |
| `aaagent thinking [<target> [<level>]]` | show, list (`--list`), pick or set the planner, reviewer and worker thinking levels |
| `aaagent reviewer [<model\|planner\|worker>] [--thinking LEVEL]` | show or set who reviews each round: a model with its own job, or planner (the chat); a worker name copies its model |
| `aaagent branch-mode [current\|branch\|pr\|pr-auto\|none] [--json]` | show or set where tasks commit: the current branch, a new branch per chat session, that plus a pull request, that without asking and with a chat restart, or no git at all |
| `aaagent pr` | finish the open branch: push it, open its pull request and switch back to the base branch (or retry the last failed one) |
| `aaagent branch-done` | finish the open branch without pushing and switch back to the base branch |
| `aaagent --model <name>` | start the chat with another planner model for this chat only |
| `aaagent --session <id>` | reopen a chat session (an id or id prefix from `aaagent sessions`) |
| `aaagent doctor` | check the installation |

Task numbers can be short: `aaagent diff 3` means `003-…`.

### Parallel tasks

Several tasks can run at the same time in one working tree when their plans declare disjoint files. A plan's frontmatter lists everything the task may change:

```markdown
---
task: 020-move-the-health-check
worker:            # optional
files: lib/aaagent/health.py, tests/test_health.py, docs/
---
```

- Entries are repo-relative. `dir/` means everything below that directory; `*`, `?` and `[` make a glob (`lib/aaagent/*.py`). Absolute paths, `..` and `.aaagent/` are refused.
- `aaagent dispatch` lets a task start only when no other active task's `files:` overlap its own. Overlapping tasks, or a task without `files:` while another is active, are refused with a message that names the other task. A task without `files:` keeps the old behaviour: it needs the working tree to itself.
- Disjoint tasks go to different workers when one is free: if the default worker is busy with another task, dispatch picks the first idle worker. Fix rounds stay on the task's own worker.
- `aaagent diff`, the review prompt and the commit (aaagent's own, or the fallback job) only look at the task's declared files, and a round that changes files outside them is reported as a warning (the reviewer decides what to do).
- Commits never run at the same time: a task waits while another task is committing, so the shared git index is never touched by two workers at once.
- Your own uncommitted changes are still kept out of the commits; they are refreshed when the task is first dispatched.

With `[parallel] isolation = "worktree"`, each task also gets its own git worktree (`.aaagent/worktrees/<task>`, branch `aaagent/<task>`): its implement, fix and review jobs run there, so the tasks' tests never see each other's half-written code. aaagent copies the plan, reports and reviews in and out. After approval aaagent commits in the worktree itself (a commit job only as the fallback), and aaagent applies the commit to the session branch (fast-forward, or cherry-pick when other tasks were committed meanwhile), then removes the worktree and its branch. If that fails, for example because your own uncommitted changes are in the way, the task stops with its worktree kept, and the next `aaagent dispatch` retries only the merge. `[parallel] links` (a virtualenv, `node_modules`, `.env`) are linked into each new worktree, and `[parallel] setup` (for example `npm ci`) runs once in it. Worktrees need git and `track_agent_dir = false`; otherwise tasks use the shared tree.

### Branches and pull requests

By default tasks commit on the branch that is checked out. `[git] branch_mode` changes that:

| mode | where the work lands |
|---|---|
| `current` | the branch that is checked out (the default) |
| `branch` | one new branch per chat session (`feat/…`, `fix/…`); every task in the session commits on it |
| `pr` | like `branch`, and the branch becomes one pull request when you say yes |
| `pr-auto` | like `pr`, but the planner opens the pull request without asking and the chat restarts afterwards with a summary of it |

A **session branch** starts at the first `aaagent dispatch` of a chat while no branch is open. Every task dispatched while it is open joins it with its own commit — also tasks dispatched later, after a pause with no active task — so one chat's work lands on one branch instead of a branch and a pull request per group of tasks. Tasks that run in parallel (disjoint `files:`) therefore share one branch too.

The branch name comes from the first plan's `branch:` frontmatter when it has one, otherwise from the task's title: a leading `fix`, `bug`, `bugfix`, `hotfix`, `docs`, `doc`, `refactor`, `test`, `tests`, `chore`, `feat` or `feature` word becomes the prefix (`021-fix-login-crash` → `fix/login-crash`), anything else is `feat/` (`022-branch-mode-tui` → `feat/branch-mode-tui`). A name that is already taken gets `-2`, `-3`, … appended. Later plans' `branch:` is ignored while the branch is open.

When the last active task on the branch is committed, aaagent prints that the tasks are done and tells the planner to **ask you**; nothing is pushed yet. The branch stays open until the planner runs one of these after your yes:

- `aaagent pr` pushes the branch to `origin`, opens one pull request with `gh` (its body lists every commit and task on the branch), and switches the checkout back to the base branch.
- `aaagent branch-done` switches the checkout back to the base branch; nothing is pushed.

Both refuse while a task is still active. On no, keep working: new tasks join the open branch, and the question comes again when they are done. An idle branch with no commits at all (for example its only task was abandoned) closes itself, returns to the base branch and is deleted.

`aaagent branch-mode` shows the mode with the current branch and the open branch, `aaagent branch-mode branch` (or `current`, `pr`, `pr-auto`, `none`) sets it in this project's config, and `aaagent branch-mode --json` prints it for machines. Switching the mode while a branch is open changes which command the planner asks you about at its end.

In `pr` mode, `gh` (the GitHub CLI) must be installed and logged in, and `origin` must be a GitHub repository. A push or pull request failure never fails the commit: it is recorded in `.aaagent/batches/` and printed, and `aaagent pr` retries it (also for a branch that was finished in `branch` mode and never pushed).

In `pr-auto` mode the planner never asks: once every task it planned for your request is committed, it runs `aaagent pr` by itself. The pull request is pushed and opened exactly as in `pr` mode, and the checkout returns to the base branch. aaagent then leaves `.aaagent/new-chat-pending.json`, and the chat is replaced by a fresh one that opens with a short summary of the pull request, so your next request starts cleanly on the base branch. If the push or the pull request fails, the same thing happens, and the new chat offers to retry with `aaagent pr`.

### No Git

Use No Git when the folder cannot or must not be a git repository: a shared or synced folder, a checkout managed by another tool, or a project whose git history aaagent must never touch.

Choose it in the start-up prompt (in a folder that is not a repository, `aaagent` offers `git init` or No Git), with `aaagent branch-mode none`, or with `[git] branch_mode = "none"` in the project's config.

In No Git mode aaagent keeps its own hidden repository in `.aaagent/shadow.git`, with the project folder as its work tree and the project's `.gitignore` files honoured. `git status`, `git diff` and `git log` — in the chat and in the workers — read that history, so diffs and reviews work as usual. An approved task becomes a checkpoint commit there instead of a worker commit job (there is no commit job at all); the planner reports the checkpoint id instead of a commit hash. There are no branches, no pull requests and no pushes, and a real repository in the folder is never touched.

The `git` program is still required: aaagent runs it against the shadow repository.

Switching to or from `none` is refused while tasks are active or a branch is open, and leaving `none` needs a real repository in the folder. After switching the mode, restart the chat so its own git commands follow the new one.

### Images

The chat runs on the machine where aaagent runs, so an image has to be on that machine first: copy it with `scp`, or drag it into VSCode's Explorer. Then run `aaagent attach shot.png`; it types `@.aaagent/attachments/…` into the chat, and Opus reads the image. You can also type `@path/to/image.png` in the chat yourself. A path from another computer (for example `/Users/me/Downloads/band.jpg` or `C:\Users\me\Downloads\band.jpg` over Remote-SSH) is not a file on the server, so Opus cannot open it: it tells you to attach the image instead, and in VSCode dropping it on the chat with Shift copies it for you. That Shift-drop works for any file up to 5 MB, not only images, and Windows paths are understood. On a desktop Linux session, `xclip -selection clipboard -t image/png -o | aaagent attach -` attaches the clipboard. Attachments live in `.aaagent/attachments/`: they are never committed, and you can delete them at any time.

## Safety rules

Code enforces these; they are not just prompt instructions:

- **Opus cannot edit project files.** A hook blocks its edit tools outside `.aaagent/`, and `git commit`/`git push` are denied for it.
- **Implementing workers cannot commit.** `aaagent-worker` denies `git commit/push/reset/rebase/clean/stash/checkout/switch`, `sudo`, destructive `docker` commands, files outside the project, `.env` files, and edits to plans and reviews.
- **The commit step happens only after approval.** `aaagent dispatch` commits when the latest review says `verdict: approved`: aaagent itself, with the `## Commit message` from the report, or the `aaagent-committer` job as the fallback (no message, `direct_commit = false`, or git failed). Repo commit hooks run. The `aaagent-committer` agent cannot edit files and can only run git status/diff/log/add/commit; `--amend` and `add -A`/`add .` are denied.
- **Nothing is pushed until you say yes.** Only `aaagent pr` pushes a branch to `origin` and opens its pull request; `aaagent branch-done` finishes the branch without touching the network, and nothing else does either. Everything else is up to you.
- **Parallel tasks only with disjoint `files:`.** Several tasks may be active in one working tree, but only when every plan declares the files it may change and those lists do not overlap; anything else keeps the working tree to one task. With `[parallel] isolation = "worktree"` each task also gets its own git worktree, and its commit is applied to the branch only after approval.
- **Your own uncommitted changes stay out of the commit.** Files that were already modified when a task started are listed and excluded from its commit.
- **Integration check before a pull request.** With `[integration] command` set, `aaagent pr` and `aaagent branch-done` refuse while it fails for the current commit.
- **Escalation:** a failed implement/fix job is retried once on the fallback worker; a failed review is retried once on the reviewer itself, then the round goes back to the planner. After 2 rejected reviews in a row the task moves to the fallback worker. After 3 rounds without approval Opus stops and asks you. A reviewer that finds the plan itself wrong (`plan_problem`) stops the loop instead: the planner revises `plan.md`, the next dispatch is a fix round on the same worker, and plan_problem rounds do not count toward the fallback worker.

These are permission rules, not a sandbox. Workers run as your user, so treat this like giving a junior developer a shell on the machine.

## Configuration

Put overrides in `~/.config/aaagent/config.toml`, or in `<project>/.aaagent/config.toml` for one project. All options and their defaults are in [config.default.toml](config.default.toml). `aaagent model` and `aaagent thinking` edit these files for you: `aaagent model planner sonnet`, `aaagent thinking planner xhigh`, and `--project` for the project config.

```toml
[orchestrator]
model = "opus"                    # any Claude Code model alias or id
thinking = "high"                 # claude --effort: low, medium, high, xhigh or max; empty = the default
extra_args = []                   # any other claude arguments (an --effort here wins over `thinking`)

[workers.worker1]
model = "opencode-go/deepseek-v4-pro"   # swap a model
thinking = "high"                 # opencode model variant, e.g. low | high | max; empty = the model's default

[workers.worker3]                 # add a worker
model = "opencode-go/kimi-k3"

[reviewer]
model = "planner"                 # or a Claude model name / opencode id for a reviewer job
thinking = ""                     # claude --effort or the opencode variant; empty = default
parallel = 1                      # reviews at the same time (1–8); raise it for parallel tasks

[flow]
default_worker = "worker1"
fallback_worker = "worker2"
max_review_rounds = 3
fallback_after_failed_reviews = 2
job_timeout_minutes = 30

[git]
commit_trailer = ""               # e.g. "Co-Authored-By: aaagent <noreply@example.invalid>"
direct_commit = true              # aaagent commits approved tasks itself with the report's `## Commit message`; false = a worker commit job
branch_mode = "current"           # current = the checked-out branch; branch = a branch per chat session; pr = that + push and pull request when you say yes; pr-auto = that without asking, then the chat restarts; none = No Git, aaagent keeps its own history in .aaagent/shadow.git

[parallel]
isolation = "shared"              # "worktree" = each task in its own git worktree
links = []                        # e.g. [".venv", "node_modules"] linked into each worktree
setup = ""                        # e.g. "npm ci", run once in each new worktree

[integration]
command = ""                      # e.g. "python3 -m unittest discover -s tests -q"
timeout_minutes = 20

[check]
command = ""                      # after every implement/fix round; "" = the [integration] command
enabled = true                    # false = off even when a command is set
on_fail = "review"                # "fix" = reject the round without a review job
timeout_minutes = 0               # 0 = the [integration] timeout
```

`opencode models` lists the model ids you can use. `aaagent workers add|remove` edits the worker list for you; a new worker gets its own pane the next time you run `aaagent`.

## Files in a project

```
.aaagent/                       (listed in .git/info/exclude, never committed)
  config.toml       this project's config overrides
  lessons.md        project lessons, appended to implement, fix and review prompts
  tasks/003-add-shout-function/
    plan.md  report-1.md  review-1.md  state.json
    logs/    raw worker transcripts (jsonl) and job results
  queue/<worker>/   pending and running jobs
  results/          finished job results
  workers/          worker pids and logs
  worktrees/        per-task git worktrees ([parallel] isolation = "worktree")
  attachments/      images from `aaagent attach`
  fetch/            image copy requests between the chat and VSCode
  shadow.git/       aaagent's own git history in No Git mode
  active-tasks.json which tasks are running in this working tree now
  batch.json        the open session branch and the tasks that share it
  batches/          every finished branch, with its commits and pull request
  chat.json         the last chat's session id, for `aaagent -c`
  sessions.json     the chat sessions and the tasks each one created
  new-chat-pending.json  a pr-auto pull request is open; the chat restarts
  chat-pane         the tmux pane of the chat, for `aaagent attach`
```

## Development

```bash
python3 -m unittest discover -s tests -q   # fake opencode, claude and gh; no quota spent
node --test vscode/test/                   # extension logic + activate() against a stub vscode
python3 vscode/build_vsix.py               # → vscode/dist/local.aaagent-<version>.vsix
```

`AAAGENT_POLL_SECONDS` shortens the daemon and dispatch polling interval and is meant for tests.

| path | role |
|---|---|
| `lib/aaagent/cli.py` | commands |
| `lib/aaagent/config.py` | config loading: defaults, user config, project config |
| `lib/aaagent/models.py` | model and thinking-level changes for `aaagent model` / `aaagent thinking` |
| `lib/aaagent/attach.py` | `aaagent attach`: copy images and mention them in the chat |
| `lib/aaagent/session.py` | launches Claude and the workers (tmux or background), chat restore |
| `lib/aaagent/sessions.py` | the chat sessions: history, and which session created each task |
| `lib/aaagent/tui.py` | the terminal dashboard (`aaagent ui`) |
| `lib/aaagent/status.py` | status data and the dashboard's view logic |
| `lib/aaagent/worker.py` | worker daemon and file queue |
| `lib/aaagent/killer.py` | `aaagent kill`: finds and kills the project's aaagent processes |
| `lib/aaagent/backends.py` | worker backends: opencode and headless Claude Code |
| `lib/aaagent/dispatch.py` | queues phases, waits, falls back, checks results |
| `lib/aaagent/tasks.py` | task files and the rules for the next phase |
| `lib/aaagent/worktrees.py` | per-task git worktrees: create, sync files, merge, remove |
| `lib/aaagent/branching.py` | session branches, pull requests, integration check |
| `lib/aaagent/gitutil.py` | git helpers, No Git shadow repository |
| `vscode/extension.js` | VSCode extension: commands, refresh, notifications |
| `vscode/src/` | tree views, terminals, diff view, and the pure UI logic (`model.js`) |
| `prompts/` | `orchestrator.md` with its conditional additions (`orchestrator-worker-review.md`, `orchestrator-no-git.md`, `orchestrator-worktree.md`) and the four worker prompts (`worker-implement.md`, `worker-fix.md`, `worker-review.md`, `worker-commit.md`) |
| `opencode/agents/` | worker agent definitions and permissions |
| `claude/hooks/guard_edit.py` | blocks Opus edits outside `.aaagent/` |
| `claude/hooks/guard_worker.py` | keeps Claude workers away from secrets and the planner's files |
| `claude/hooks/record_session.py` | records the chat's session id for `aaagent -c` |
| `claude/hooks/local_images.py` | copies images from the user's computer that a prompt mentions (VSCode) |
| `claude/hooks/chat_stop.py` | Stop hook: replaces the chat after `aaagent pr` in pr-auto mode |
| `claude/hooks/chat_restart.py` | restarts the tmux chat, run detached by chat_stop |
