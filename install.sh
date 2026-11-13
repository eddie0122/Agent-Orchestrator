#!/usr/bin/env bash
# aaagent one-shot installer. Safe to re-run.
#   ./install.sh            install everything that is missing
#   ./install.sh --no-tmux  skip installing tmux
#   ./install.sh --no-vscode  skip installing the VSCode extension
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
OC_AGENTS="$HOME/.config/opencode/agents"
AAAGENT_CONFIG_DIR="$HOME/.config/aaagent"
WANT_TMUX=1
WANT_VSCODE=1
for arg in "$@"; do
  case "$arg" in
    --no-tmux) WANT_TMUX=0 ;;
    --no-vscode) WANT_VSCODE=0 ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '  \033[31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }
interactive() { [ -t 0 ] && [ -t 1 ]; }
STEP=0; STEPS=9
step() { STEP=$((STEP + 1)); bold "$STEP/$STEPS $*"; }
export PATH="$BIN_DIR:$HOME/.opencode/bin:$PATH"

step "prerequisites"
command -v python3 >/dev/null || die "python3 is required"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "python 3.11+ is required (found $(python3 --version))"
ok "$(python3 --version)"
command -v git >/dev/null || die "git is required"
ok "$(git --version)"
command -v curl >/dev/null || die "curl is required"

step "opencode (workers)"
if ! command -v opencode >/dev/null; then
  echo "  installing opencode…"
  curl -fsSL https://opencode.ai/install | bash
  export PATH="$HOME/.opencode/bin:$PATH"
fi
command -v opencode >/dev/null || die "opencode install failed"
ok "opencode $(opencode --version 2>/dev/null)"
if opencode auth list 2>/dev/null | grep -q "OpenCode Go"; then
  ok "logged in to OpenCode Go"
elif interactive; then
  echo "  log in to your OpenCode Go subscription:"
  opencode auth login || warn "login not completed; run 'opencode auth login' later"
else
  warn "not logged in to OpenCode Go; run 'opencode auth login'"
fi

step "Claude Code CLI (orchestrator)"
if command -v claude >/dev/null; then
  ok "claude $(claude --version 2>/dev/null | head -1)"
else
  echo "  installing Claude Code (native installer)…"
  if curl -fsSL https://claude.ai/install.sh | bash; then
    ok "claude $("$BIN_DIR/claude" --version 2>/dev/null | head -1)"
  else
    warn "native install failed; aaagent will fall back to the VSCode extension's bundled claude if present"
  fi
fi
CLAUDE_BIN="$(command -v claude || true)"
if [ -n "$CLAUDE_BIN" ]; then
  if "$CLAUDE_BIN" auth status 2>/dev/null | grep -q '"loggedIn": true'; then
    ok "logged in to Claude"
  elif interactive; then
    echo "  log in with your Claude subscription:"
    "$CLAUDE_BIN" auth login || warn "login not completed; run 'claude auth login' later"
  else
    warn "not logged in to Claude; run 'claude auth login'"
  fi
fi

step "tmux (shows workers next to the chat)"
if command -v tmux >/dev/null; then
  ok "$(tmux -V)"
elif [ "$WANT_TMUX" = 0 ]; then
  warn "skipped (--no-tmux); workers will run in the background"
elif command -v apt-get >/dev/null && { sudo -n true 2>/dev/null || interactive; }; then
  echo "  installing tmux with apt (sudo may ask for your password)…"
  if sudo apt-get install -y tmux >/dev/null; then ok "$(tmux -V)"; else warn "tmux install failed; workers will run in the background"; fi
else
  warn "tmux not installed (needs sudo). Later: sudo apt-get install tmux. Workers will run in the background meanwhile."
fi

step "clean up the old 'aa' install"
# Links this repo created under earlier names (aa, aa-worker.md, ...) now point at nothing.
cleaned=0
for link in "$BIN_DIR"/* "$OC_AGENTS"/*; do
  [ -L "$link" ] || continue
  target="$(readlink "$link")"
  case "$target" in
    "$ROOT"/*) if [ ! -e "$target" ]; then rm "$link"; ok "removed stale link $link"; cleaned=1; fi ;;
  esac
done
OLD_CONFIG_DIR="$HOME/.config/auto-agent"
if [ -f "$OLD_CONFIG_DIR/config.toml" ] && [ ! -f "$AAAGENT_CONFIG_DIR/config.toml" ]; then
  mkdir -p "$AAAGENT_CONFIG_DIR"
  mv "$OLD_CONFIG_DIR/config.toml" "$AAAGENT_CONFIG_DIR/config.toml"
  rmdir "$OLD_CONFIG_DIR" 2>/dev/null || true
  ok "moved $OLD_CONFIG_DIR/config.toml → $AAAGENT_CONFIG_DIR/"
  cleaned=1
fi
[ "$cleaned" = 1 ] || ok "nothing to clean up"

step "worker agents for opencode"
mkdir -p "$OC_AGENTS"
for f in "$ROOT"/opencode/agents/*.md; do
  ln -sfn "$f" "$OC_AGENTS/$(basename "$f")"
  ok "$(basename "$f" .md) → $OC_AGENTS"
done
opencode reload >/dev/null 2>&1 || true

step "aaagent command"
mkdir -p "$BIN_DIR"
chmod +x "$ROOT/bin/aaagent" "$ROOT/claude/hooks/guard_edit.py" "$ROOT/claude/hooks/record_session.py"
existing="$(command -v aaagent || true)"
if [ -n "$existing" ] && [ "$(readlink -f "$existing")" != "$ROOT/bin/aaagent" ]; then
  warn "another 'aaagent' exists at $existing; $BIN_DIR/aaagent will be used only if $BIN_DIR comes first in PATH"
fi
ln -sfn "$ROOT/bin/aaagent" "$BIN_DIR/aaagent"
ok "$BIN_DIR/aaagent → $ROOT/bin/aaagent"
MARK="# auto-agent: ~/.local/bin on PATH"
if ! grep -qF "$MARK" "$HOME/.bashrc" 2>/dev/null; then
  printf '\n%s\ncase ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac\n' "$MARK" >> "$HOME/.bashrc"
  ok "added ~/.local/bin to PATH in ~/.bashrc"
fi

step "configuration"
mkdir -p "$AAAGENT_CONFIG_DIR"
if [ ! -f "$AAAGENT_CONFIG_DIR/config.toml" ]; then
  cat > "$AAAGENT_CONFIG_DIR/config.toml" <<'EOF'
# aaagent user config. Only put overrides here; every option and its default
# is listed in config.default.toml in the aaagent folder.
#
# [orchestrator]
# model = "opus"
#
# [workers.deepseek]
# model = "opencode-go/deepseek-v4.1-flash"
#
# [workers.glm]
# model = "opencode-go/glm-5.3-flash"
#
# [flow]
# default_worker = "deepseek"
# fallback_worker = "glm"
# max_review_rounds = 3
EOF
  ok "created $AAAGENT_CONFIG_DIR/config.toml"
else
  ok "kept existing $AAAGENT_CONFIG_DIR/config.toml"
fi

step "VSCode extension"
# The VSCode CLI that installs into this machine's VSCode: a local `code`, or the
# newest VSCode server (Remote-SSH) install.
find_vscode_cli() {
  if command -v code >/dev/null; then command -v code; return; fi
  local lru="$HOME/.vscode-server/cli/servers/lru.json" srv
  if [ -f "$lru" ]; then
    srv="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[0])' "$lru" 2>/dev/null || true)"
    if [ -n "$srv" ] && [ -x "$HOME/.vscode-server/cli/servers/$srv/server/bin/code-server" ]; then
      echo "$HOME/.vscode-server/cli/servers/$srv/server/bin/code-server"; return
    fi
  fi
  ls -t "$HOME"/.vscode-server/cli/servers/*/server/bin/code-server "$HOME"/.vscode-server/bin/*/bin/code-server 2>/dev/null | head -1
}
if [ "$WANT_VSCODE" = 0 ]; then
  warn "skipped (--no-vscode)"
else
  VSCODE_CLI="$(find_vscode_cli || true)"
  if [ -z "$VSCODE_CLI" ]; then
    warn "VSCode not found on this machine; skipped. Connect with VSCode Remote-SSH once, then re-run install.sh."
  else
    VSIX="$(python3 "$ROOT/vscode/build_vsix.py")"
    if "$VSCODE_CLI" --install-extension "$VSIX" --force >/dev/null 2>&1; then
      ok "installed $(basename "$VSIX"). In VSCode run 'Developer: Reload Window' to load it."
    else
      warn "could not install $VSIX with $VSCODE_CLI; install it by hand: Extensions view → … → Install from VSIX"
    fi
  fi
fi

echo
bold "checking the installation"
"$ROOT/bin/aaagent" doctor || true
echo
bold "done. Open a new terminal (or run: source ~/.bashrc), cd into a project, and run: aaagent"
