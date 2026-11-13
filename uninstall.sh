#!/usr/bin/env bash
# Remove what install.sh linked. Keeps opencode, Claude Code, tmux, your config
# (~/.config/aaagent) and every project's .aaagent/ folder.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

remove_link() {
  if [ -L "$1" ] && [ "$(readlink -f "$1")" = "$(readlink -f "$2")" ]; then
    rm "$1" && echo "removed $1"
  fi
}

remove_link "$HOME/.local/bin/aaagent" "$ROOT/bin/aaagent"
for f in "$ROOT"/opencode/agents/*.md; do
  remove_link "$HOME/.config/opencode/agents/$(basename "$f")" "$f"
done
command -v opencode >/dev/null && opencode reload >/dev/null 2>&1 || true

# VSCode extension (local `code`, or every VSCode server install that has it)
for cli in $(command -v code || true) "$HOME"/.vscode-server/cli/servers/*/server/bin/code-server "$HOME"/.vscode-server/bin/*/bin/code-server; do
  [ -x "$cli" ] || continue
  if "$cli" --list-extensions 2>/dev/null | grep -qx "local.aaagent"; then
    "$cli" --uninstall-extension local.aaagent >/dev/null 2>&1 && echo "removed VSCode extension local.aaagent"
    break
  fi
done
echo "aaagent unlinked. The PATH line in ~/.bashrc and ~/.config/aaagent/ were left in place."
