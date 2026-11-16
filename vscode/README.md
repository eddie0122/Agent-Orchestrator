# aaagent for VSCode

Chat with Claude as the planner; a reviewer (the planner or any model) checks each round, and opencode or Claude workers implement and commit.

- **Sidebar** (aaagent icon in the activity bar), with five views:
  - **Planner**: the chat's Claude model and thinking level, and the **Git** node: where tasks commit — the checked-out branch, one branch per chat session (with the pull request asked for or opened automatically), or **No Git** (no repository; aaagent keeps its own history). Click either to change it.
  - **Reviewer**: who reviews each round — the planner (the chat) itself, or any Claude/opencode model. **Change Reviewer…** switches; while a model reviews, its thinking level, output terminal and **Stop Reviewer** are here too.
  - **Workers**: the workers and what they are doing. Add one with **+**, and right-click a worker to change its model or thinking level, use it as the default, or remove it.
  - **Sessions**: each Opus chat session, newest first, with the tasks it created. Expand one to see its tasks; the ↻ button on a session reopens it, and right-click → **Delete Session…** (or the trash button) deletes a session that is not the open chat.
  - **Tasks**: all tasks with their plan, reports, reviews and Changes.
- **New Session** (Sessions view title): opens a fresh Opus chat as an editor tab, with one terminal per worker (and the reviewer, when a model reviews) in the panel. Any open chat closes first.
- **Open Chat**: shows the open chat, or starts one if there is none (Sessions view `…` menu).
- **Restore Previous Session…** (Sessions view title): reopens one of this project's earlier chats (`aaagent start --session <id>`).
- **Open Session**: the ↻ button on a session in the Sessions view reopens it; from the command palette it asks which one.
- **Delete Session…**: right-click a session (or use the trash button) to remove it and its conversation, after a modal confirmation. The open chat cannot be deleted.
- **Ask Opus…**: type a request; the chat starts if it is not running yet.
- **Change Planner Model…**/**Change Planner Thinking Level…** (Planner view): the chat's Claude model and reasoning effort, for all projects or just this one.
- **Select Git Branch Mode…** (Planner view title, or click the Git node): picks current branch / new branch / new branch + pull request / new branch + pull request (auto) / No Git. A new branch lasts the whole chat session: the planner asks before finishing it, and only then does `aaagent pr` push and open the pull request (or `aaagent branch-done` switch back without pushing). In the auto variant there is no question: when your request is done the planner opens the pull request itself, then the chat restarts. No Git needs no repository: aaagent keeps its own history in `.aaagent/shadow.git`. It is the same setting as `aaagent branch-mode`.
- **Change Reviewer…** (Reviewer view title): choose who reviews each round: the planner (the chat) or any Claude/opencode model, from a list or typed, for all projects or just this one.
- **Change Reviewer Thinking Level…**/**Show Reviewer Output**/**Stop Reviewer** (Reviewer view, while a model reviews): its reasoning effort or model variant, its terminal, and stopping it.
- **Add Worker…** (Workers view title): pick or type a model for a new worker; it gets its own terminal like the others.
- **Remove Worker…** and **Use as Default Worker** (right-click a worker): remove a worker after a confirmation (its config and terminal go, its tasks keep their history) or make it the default implementer.
- **Select Model…**/**Select Thinking Level…** (Workers view `…` menu, or the command palette): change the planner's, the reviewer's or a worker's model/thinking level, from a list or typed, for all projects or just this one.
- **Send Selection to Chat** (editor right-click): adds an `@file#L10-20` reference to the chat input.
- **Attach Image…** (Sessions view title): paste a screenshot (Ctrl+V / ⌘V), hold **Shift** and drop image files, or click **Choose files…**. The image goes to `.aaagent/attachments/` and its `@`-mention is typed into the chat input. Works over Remote-SSH; images up to 5 MB (PNG, JPEG, GIF, WebP). Dropping an image on the chat terminal (hold Shift) also works over Remote-SSH: the path from your computer is detected and aaagent copies the file to the server.
- **Attach to aaagent Chat** (Explorer or editor tab right-click on an image): types that image's `@`-mention into the chat input.
- **Status bar**: what is happening right now. Notifications appear when a task is committed or needs you.

Requires the `aaagent` command, which `install.sh` in the aaagent folder sets up.
