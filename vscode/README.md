# aaagent for VSCode

Chat with Claude Opus as planner and reviewer while opencode workers (DeepSeek, GLM) implement, fix and commit.

- **Sidebar** (aaagent icon in the activity bar): the workers and what they are doing, and the tasks with their plan, reports, reviews and changes.
- **Start Chat**: opens the Opus chat as an editor tab, with one terminal per worker in the panel.
- **Resume Last Chat**: reopens this project's previous aaagent chat (`aaagent -c`).
- **Ask Opus…**: type a request; the chat starts if it is not running yet.
- **Send Selection to Chat** (editor right-click): adds an `@file#L10-20` reference to the chat input.
- **Status bar**: what is happening right now. Notifications appear when a task is committed or needs you.

Requires the `aaagent` command, which `install.sh` in the aaagent folder sets up.
