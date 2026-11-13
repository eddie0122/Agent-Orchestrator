# aaagent for VSCode

Chat with Claude Opus as planner and reviewer while opencode workers (DeepSeek, GLM) implement, fix and commit.

- **Sidebar** (aaagent icon in the activity bar): the workers and what they are doing, and the tasks with their plan, reports, reviews and changes.
- **New Chat**: opens a fresh Opus chat as an editor tab, with one terminal per worker in the panel. Any open chat closes first.
- **Open Chat**: shows the open chat, or starts one if there is none (Tasks view `…` menu).
- **Resume Last Chat**: reopens this project's previous aaagent chat (`aaagent -c`).
- **Ask Opus…**: type a request; the chat starts if it is not running yet.
- **Select Model…** (Workers view title, or right-click a worker → **Change Model…**): change the planner's Claude model or a worker's opencode model, for all projects or just this one.
- **Send Selection to Chat** (editor right-click): adds an `@file#L10-20` reference to the chat input.
- **Attach Image…** (Tasks view title): paste a screenshot (Ctrl+V / ⌘V), hold **Shift** and drop image files, or click **Choose files…**. The image goes to `.aaagent/attachments/` and its `@`-mention is typed into the chat input. Works over Remote-SSH; images up to 5 MB (PNG, JPEG, GIF, WebP).
- **Attach to aaagent Chat** (Explorer or editor tab right-click on an image): types that image's `@`-mention into the chat input.
- **Status bar**: what is happening right now. Notifications appear when a task is committed or needs you.

Requires the `aaagent` command, which `install.sh` in the aaagent folder sets up.
