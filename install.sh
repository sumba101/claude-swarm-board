#!/bin/bash
# Install the swarm board into ~/.claude: the skill, the CLI, and the PostToolUse
# inbox hook. Safe to run again; it never duplicates the hook.
set -euo pipefail

src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
claude_dir="$HOME/.claude"
settings="$claude_dir/settings.json"
hook_cmd="~/.claude/hooks/board-inbox.sh"

for tool in python3 jq; do
  command -v "$tool" >/dev/null || { echo "error: $tool is required" >&2; exit 1; }
done

mkdir -p "$claude_dir/skills/swarm-board" "$claude_dir/bin" "$claude_dir/hooks" "$claude_dir/boards"
cp "$src/skills/swarm-board/SKILL.md" "$claude_dir/skills/swarm-board/SKILL.md"
cp "$src/bin/board.py" "$src/bin/board.sh" "$claude_dir/bin/"
cp "$src/hooks/board-inbox.sh" "$claude_dir/hooks/"
chmod +x "$claude_dir/bin/board.py" "$claude_dir/bin/board.sh" "$claude_dir/hooks/board-inbox.sh"
echo "copied skill to $claude_dir/skills/swarm-board, CLI to $claude_dir/bin, hook to $claude_dir/hooks"

if [ -f "$settings" ]; then
  jq empty "$settings" 2>/dev/null || { echo "error: $settings is not valid JSON; fix it and rerun" >&2; exit 1; }
  backup="$settings.bak.$(date +%Y%m%d%H%M%S)"
  cp "$settings" "$backup"
  echo "backed up settings to $backup"
else
  echo '{}' > "$settings"
fi

already=$(jq '[.hooks.PostToolUse[]?.hooks[]?.command // empty | select(test("board-inbox\\.sh"))] | length' "$settings")
if [ "$already" -gt 0 ]; then
  echo "PostToolUse hook already present in $settings; left it alone"
else
  tmp=$(mktemp)
  jq --arg cmd "$hook_cmd" '.hooks.PostToolUse = ((.hooks.PostToolUse // []) + [{
      matcher: "*",
      hooks: [{type: "command", command: $cmd, timeout: 5}]
    }])' "$settings" > "$tmp"
  mv "$tmp" "$settings"
  echo "added the board-inbox PostToolUse hook to $settings"
fi

echo
echo "Done. Restart Claude Code so it loads the skill and hook."
echo "The CLI is $claude_dir/bin/board.sh; add $claude_dir/bin to PATH to call it as board.sh."
