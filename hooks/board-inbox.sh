#!/bin/bash
# PostToolUse: push new swarm-board posts to the agent that made this tool call.
# An agent registers by running `board.sh join <board> <name>`; state maps its
# agent_id to that board and name. The lead never joins (it watches instead).
board_py="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/bin/board.py"
in=$(cat)
key=$(printf '%s' "$in" | jq -r '.agent_id // ("main-" + .session_id)')
cmd=$(printf '%s' "$in" | jq -r '.tool_input.command // empty')
dir="$HOME/.claude/boards/.seen"; state="$dir/$key"

if [[ "$cmd" =~ board\.sh[[:space:]]+join[[:space:]]+[\"\']?([^[:space:]\"\';&|]+)[\"\']?[[:space:]]+[\"\']?([A-Za-z0-9_.-]+) ]]; then
  b="${BASH_REMATCH[1]}"; n="${BASH_REMATCH[2]}"
  b="${b//\"/}"; b="${b//\'/}"; b="${b/#\~/$HOME}"; n="${n//\"/}"; n="${n//\'/}"
  [[ "$b" == */* || "$b" == *.db ]] || b="$HOME/.claude/boards/$b.db"
  if [ -f "$b" ]; then
    mkdir -p "$dir"; printf '%s\t%s\n' "$b" "$n" > "$state"
  fi
  exit 0
fi
[ -f "$state" ] || exit 0
IFS=$'\t' read -r board name < "$state"
[ -f "$board" ] || { rm -f "$state"; exit 0; }
if [[ "$cmd" =~ board\.sh[[:space:]]+read ]]; then
  exec python3 "$board_py" _inbox "$board" "$name" --mark-read
fi
exec python3 "$board_py" _inbox "$board" "$name"
