# aaagent

Chat with Claude Opus in your terminal. Opus plans and reviews; cheaper opencode workers implement, fix, and commit.

```
you ──chat──► Opus (Claude Code, Pro subscription)
                 │ 1. investigates, writes  .aaagent/tasks/001-x/plan.md
                 │ 2. aaagent dispatch 001  ──►  DeepSeek worker implements  → report-1.md
                 │ 3. reviews diff + tests, writes review-1.md (approved / changes_requested)
                 │ 4. aaagent dispatch 001  ──►  worker fixes → report-2.md … (repeat 3–4)
                 │ 5. aaagent dispatch 001  ──►  worker commits (only after "approved")
                 ▼
              tells you what changed and the commit hash
```

Workers (OpenCode Go subscription):

| worker | model | role |
|---|---|---|
| deepseek | `opencode-go/deepseek-v4.1-flash` | default implementer |
| glm | `opencode-go/glm-5.3-flash` | takes over when DeepSeek fails or keeps failing review; can be chosen per task |

## Install

```bash
cd /mnt/HDD1/docker/auto_agent
./install.sh
```

The installer is safe to re-run. It:

- checks python 3.11+ and git
- installs opencode and the Claude Code CLI if they are missing, and asks you to log in if needed
- installs tmux with apt, which asks for your sudo password
- registers the `aaagent-worker` and `aaagent-committer` agents in `~/.config/opencode/agents/`
- links `aaagent` into `~/.local/bin`
- builds and installs the VSCode extension (skip it with `--no-vscode`)
- runs `aaagent doctor`

It also cleans up an install from before the rename (the `aa` command, `aa-*` agents, `~/.config/auto-agent/`).

`./uninstall.sh` removes the links and the VSCode extension. It leaves your config and logins alone.

## Use in VSCode

After installing, run **Developer: Reload Window** once. Then:

1. Open a project folder (a git repository) and click the **aaagent** icon in the activity bar.
2. Click **New Chat** (or run `aaagent: Ask Opus…` from the command palette and type the request). **New Chat** closes the open chat and starts a fresh one; **Open Chat** and **Resume Last Chat** are in the Tasks view's `…` menu.
   The Opus chat opens as an editor tab, and the DeepSeek and GLM workers open as split terminals in the panel.
3. Talk to Opus. The sidebar follows along:
   - **Workers**: who is running what. Inline buttons show a worker's output or stop it.
   - **Tasks**: newest first, with the current stage (planning, implementing, in review, committed…). Expand a task to open its `plan.md`, `report-N.md` and `review-N.md`, or **Changes** for the diff.
   - The **status bar** shows what is happening now. A notification appears when a task is committed or needs your decision.

Other handy commands:

- **Select Model…** (Workers view title, or right-click a worker → **Change Model…**): change the planner's Claude model or a worker's opencode model, from a list or typed, for all projects or just this one.
- **Select Thinking Level…** (Workers view title, or right-click a worker → **Change Thinking Level…**): set the planner's reasoning effort or a worker's model variant, for all projects or just this one.
- **Send Selection to Chat** (editor right-click): adds an `@file#L10-20` reference to the chat input.
- **Attach Image…** (Tasks view title): paste a screenshot (Ctrl+V / ⌘V), hold **Shift** and drop image files, or click **Choose files…**. The image is saved to `.aaagent/attachments/` and its `@`-mention is typed into the chat input, ready for you to add text and press Enter. This works over Remote-SSH too: the files are read on your machine and sent to the server. Dropping an image on the chat terminal (hold Shift) also works over Remote-SSH: the path from your computer is detected and aaagent copies the file to the server. Images up to 5 MB (PNG, JPEG, GIF, WebP).
- **Attach to aaagent Chat** (Explorer or editor tab right-click on an image): types that image's `@`-mention into the chat input.
- **Show Task Changes**, **Abandon Task…**, **Stop All Workers**, **Check Installation**, **Open Configuration**.

Settings: `aaagent.chatLocation` (`editor` or `panel`), `aaagent.notifications` (`all`, `important`, `off`), `aaagent.finishedTasksShown`, `aaagent.path`.

## Use in a terminal

```bash
cd ~/my-project    # any git repository (aaagent offers `git init` if it isn't one)
aaagent            # open the chat
aaagent "add a /health endpoint with a test"   # open the chat with a first request
aaagent -c         # reopen this project's last chat
```

With tmux installed, `aaagent` opens one window: the Opus chat on the left and the dashboard on the right. Click a pane, or press `Ctrl-b o`, to move between them. Without tmux, the workers run in the background; run `aaagent ui` in another terminal for the dashboard. Quitting the chat stops the workers.

```
┌ Opus chat ─────────────────────┬ aaagent · my-project ─────────────────┐
│                                │ WORKERS                               │
│ > add a /health endpoint       │ WORKERS                               │
│                                │  planner                  opus · high │
│                                │  deepseek          / fixing 003 r2    │
│                                │  glm               waiting for a plan │
│                                │ TASKS                                 │
│                                │  ▾ 003 add health  / fixing · deepseek│
│                                │     plan.md                           │
│                                │     report-1.md                   done│
│                                │     review-1.md      changes requested│
│                                │  ▸ 002 fix login     committed a1b2c3d│
│                                │ ── deepseek output (following) ────── │
│                                │     edit  app.py                      │
└────────────────────────────────┴───────────────────────────────────────┘
```

Dashboard keys:

| key | does |
|---|---|
| `↑` `↓` / `j` `k` | move |
| `enter` | task: show or hide its plan, reports and reviews · file: read it · worker: show its output |
| `d` | the selected task's diff |
| `l` | choose whose output the bottom pane shows (default: whoever is working) |
| `m` | change a model: the selected worker's or the planner's, or pick which one; type a name if it is not listed |
| `t` | the same for a thinking level |
| `a` | abandon the selected task, after a y/n question |
| `N` | close the chat and start a new one (shown when the chat is in aaagent's tmux layout) |
| `R` | reopen the previous chat in the chat pane (shown when there is one) |
| `?` | all keys · `q` closes a view, or quits the dashboard |

The dashboard beeps and says so in its bottom line when a task is committed or needs you.

### Sessions

Each `aaagent` starts a new chat session, and a session can create many tasks: `aaagent status` shows each task's session, and `aaagent sessions` lists the sessions with the tasks they created. `aaagent --session <id>` reopens one (the id, or a unique prefix of it, is in that list); `aaagent -c` reopens the last one for this project, and `/resume` inside the chat lists older ones. aaagent remembers its own chats in `.aaagent/sessions.json` (and the last one in `.aaagent/chat.json`), so `-c` never picks up an unrelated Claude Code conversation from the same folder. When a new chat starts while tasks are unfinished, Opus is told about them and asks you whether to continue.

To start a fresh conversation in the middle of things, press `N` in the dashboard (in the tmux layout) or type `/clear` in the chat; quitting and running `aaagent` again does the same.

Just talk to Opus. Questions get a direct answer; change requests go through the pipeline. You can steer at any point ("use glm for this", "stop after the plan", "split it into two tasks").

### Commands

Opus runs these itself. They are also useful for you:

| command | what it does |
|---|---|
| `aaagent ui` | the dashboard, in any terminal |
| `aaagent status [--all]` | workers and tasks, with each task's next step |
| `aaagent sessions [--all]` | this project's chat sessions, with the tasks each created |
| `aaagent diff <task> [--stat]` | everything changed since the task started |
| `aaagent logs <worker> [-f]` | worker output |
| `aaagent attach <file\|-> […] [--no-send]` | copy images into `.aaagent/attachments/` and add them to the chat |
| `aaagent new "<title>"` | create a task folder with a plan template |
| `aaagent dispatch <task> [--worker glm] [--force]` | run the next phase (implement / fix / commit) and wait |
| `aaagent wait <task>` | reattach to a running job |
| `aaagent abandon <task>` | drop a task (its uncommitted changes stay) |
| `aaagent stop` | stop this project's workers |
| `aaagent model [<target> [<model>]]` | show, list (`--list`), pick or set the planner and worker models |
| `aaagent thinking [<target> [<level>]]` | show, list (`--list`), pick or set the planner and worker thinking levels |
| `aaagent --model <name>` | start the chat with another planner model for this chat only |
| `aaagent --session <id>` | reopen a chat session (an id or id prefix from `aaagent sessions`) |
| `aaagent doctor` | check the installation |

Task numbers can be short: `aaagent diff 3` means `003-…`.

### Images

The chat runs on the machine where aaagent runs, so an image has to be on that machine first: copy it with `scp`, or drag it into VSCode's Explorer. Then run `aaagent attach shot.png`; it types `@.aaagent/attachments/…` into the chat, and Opus reads the image. You can also type `@path/to/image.png` in the chat yourself. A path from another computer (for example `/Users/me/Downloads/band.jpg` over Remote-SSH) is not a file on the server, so Opus cannot open it: it tells you to attach the image instead, and in VSCode dropping it on the chat with Shift copies it for you. On a desktop Linux session, `xclip -selection clipboard -t image/png -o | aaagent attach -` attaches the clipboard. Attachments live in `.aaagent/attachments/`: they are never committed, and you can delete them at any time.

## Safety rules

Code enforces these; they are not just prompt instructions:

- **Opus cannot edit project files.** A hook blocks its edit tools outside `.aaagent/`, and `git commit`/`git push` are denied for it.
- **Implementing workers cannot commit.** `aaagent-worker` denies `git commit/push/reset/rebase/clean/stash/checkout/switch`, `sudo`, destructive `docker` commands, files outside the project, `.env` files, and edits to plans and reviews.
- **The commit step happens only after approval.** `aaagent dispatch` sends a commit job only when the latest review says `verdict: approved`. The `aaagent-committer` agent cannot edit files and can only run git status/diff/log/add/commit; `--amend` and `add -A`/`add .` are denied.
- **Nothing is pushed.** Pushing is up to you.
- **One active task per working tree**, because workers share the files.
- **Your own uncommitted changes stay out of the commit.** Files that were already modified when a task started are listed and excluded from its commit.
- **Escalation:** a failed job is retried once on the other worker. After 2 rejected reviews in a row the task moves to the fallback worker. After 3 rounds without approval Opus stops and asks you.

These are permission rules, not a sandbox. Workers run as your user, so treat this like giving a junior developer a shell on the machine.

## Configuration

Put overrides in `~/.config/aaagent/config.toml`, or in `<project>/.aaagent/config.toml` for one project. All options and their defaults are in [config.default.toml](config.default.toml). `aaagent model` and `aaagent thinking` edit these files for you: `aaagent model planner sonnet`, `aaagent thinking planner xhigh`, and `--project` for the project config.

```toml
[orchestrator]
model = "opus"                    # any Claude Code model alias or id
thinking = "high"                 # claude --effort: low, medium, high, xhigh or max; empty = the default
extra_args = []                   # any other claude arguments (an --effort here wins over `thinking`)

[workers.deepseek]
model = "opencode-go/deepseek-v4-pro"   # swap a model
thinking = "high"                 # opencode model variant, e.g. low | high | max; empty = the model's default

[workers.kimi]                    # add a worker
model = "opencode-go/kimi-k3"

[flow]
default_worker = "deepseek"
fallback_worker = "glm"
max_review_rounds = 3
fallback_after_failed_reviews = 2
job_timeout_minutes = 30

[git]
commit_trailer = ""               # e.g. "Co-Authored-By: aaagent <noreply@example.invalid>"
```

`opencode models` lists the model ids you can use. A new worker gets its own pane the next time you run `aaagent`.

## Files in a project

```
.aaagent/                       (listed in .git/info/exclude, never committed)
  tasks/003-add-shout-function/
    plan.md  report-1.md  review-1.md  state.json
    logs/    raw worker transcripts (jsonl) and job results
  queue/<worker>/   pending and running jobs
  workers/          worker pids and logs
  attachments/      images from `aaagent attach`
  fetch/            image copy requests between the chat and VSCode
  chat.json         the last chat's session id, for `aaagent -c`
  sessions.json     the chat sessions and the tasks each one created
  chat-pane         the tmux pane of the chat, for `aaagent attach`
```

## Development

```bash
python3 -m unittest discover -s tests      # uses a fake opencode; no quota spent
node --test vscode/test/                   # extension logic + activate() against a stub vscode
python3 vscode/build_vsix.py               # → vscode/dist/local.aaagent-<version>.vsix
```

| path | role |
|---|---|
| `lib/aaagent/cli.py` | commands |
| `lib/aaagent/session.py` | launches Claude and the workers (tmux or background), chat restore |
| `lib/aaagent/sessions.py` | the chat sessions: history, and which session created each task |
| `lib/aaagent/tui.py` | the terminal dashboard (`aaagent ui`) |
| `lib/aaagent/status.py` | status data and the dashboard's view logic |
| `lib/aaagent/worker.py` | worker daemon and file queue |
| `lib/aaagent/dispatch.py` | queues phases, waits, falls back, checks results |
| `lib/aaagent/tasks.py` | task files and the rules for the next phase |
| `vscode/extension.js` | VSCode extension: commands, refresh, notifications |
| `vscode/src/` | tree views, terminals, diff view, and the pure UI logic (`model.js`) |
| `prompts/` | Opus system-prompt addition and worker prompts |
| `opencode/agents/` | worker agent definitions and permissions |
| `claude/hooks/guard_edit.py` | blocks Opus edits outside `.aaagent/` |
| `claude/hooks/record_session.py` | records the chat's session id for `aaagent -c` |
| `claude/hooks/local_images.py` | copies images from the user's computer that a prompt mentions (VSCode) |
