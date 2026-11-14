# aaagent for VSCode

Chat with Claude Opus as planner and reviewer while opencode workers (DeepSeek, GLM) implement, fix and commit.

- **Sidebar** (aaagent icon in the activity bar), with three views:
  - **Workers**: the workers and what they are doing.
  - **Sessions**: each Opus chat session, newest first, with the tasks it created. Expand one to see its tasks; the ↻ button on a session reopens it.
  - **Tasks**: all tasks with their plan, reports, reviews and Changes.
- **New Session** (Sessions view title): opens a fresh Opus chat as an editor tab, with one terminal per worker in the panel. Any open chat closes first.
- **Open Chat**: shows the open chat, or starts one if there is none (Sessions view `…` menu).
- **Restore Previous Session…** (Sessions view title): reopens one of this project's earlier chats (`aaagent start --session <id>`).
- **Open Session**: the ↻ button on a session in the Sessions view reopens it; from the command palette it asks which one.
- **Ask Opus…**: type a request; the chat starts if it is not running yet.
- **Select Model…** (Workers view title, or right-click a worker → **Change Model…**): change the planner's Claude model or a worker's opencode model, for all projects or just this one.
- **Select Thinking Level…** (Workers view title, or right-click a worker → **Change Thinking Level…**): set the planner's reasoning effort or a worker's model variant, for all projects or just this one.
- **Send Selection to Chat** (editor right-click): adds an `@file#L10-20` reference to the chat input.
- **Attach Image…** (Sessions view title): paste a screenshot (Ctrl+V / ⌘V), hold **Shift** and drop image files, or click **Choose files…**. The image goes to `.aaagent/attachments/` and its `@`-mention is typed into the chat input. Works over Remote-SSH; images up to 5 MB (PNG, JPEG, GIF, WebP). Dropping an image on the chat terminal (hold Shift) also works over Remote-SSH: the path from your computer is detected and aaagent copies the file to the server.
- **Attach to aaagent Chat** (Explorer or editor tab right-click on an image): types that image's `@`-mention into the chat input.
- **Status bar**: what is happening right now. Notifications appear when a task is committed or needs you.

Requires the `aaagent` command, which `install.sh` in the aaagent folder sets up.
