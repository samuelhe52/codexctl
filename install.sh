#!/bin/sh
# Symlink codexctl onto PATH and the codex-runner agent into Claude Code.
set -eu

repo=$(cd "$(dirname "$0")" && pwd)
bin_dir=${CODEXCTL_BIN_DIR:-"$HOME/.local/bin"}
agents_dir=${CLAUDE_AGENTS_DIR:-"$HOME/.claude/agents"}

mkdir -p "$bin_dir" "$agents_dir"
chmod +x "$repo/codexctl.py"
ln -sfn "$repo/codexctl.py" "$bin_dir/codexctl"
ln -sfn "$repo/agents/codex-runner.md" "$agents_dir/codex-runner.md"

echo "linked $bin_dir/codexctl"
echo "linked $agents_dir/codex-runner.md"
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) echo "warning: $bin_dir is not on PATH" >&2 ;;
esac
