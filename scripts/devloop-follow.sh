#!/bin/bash
# Follow the agent working in a devloop worktree, live: messages and tool calls.
#
#   scripts/devloop-follow.sh issue-52      # implementer of issue #52
#   scripts/devloop-follow.sh pr-74         # reviewer or fixer of PR #74
#
# Reads the newest session transcript whose working directory is that worktree: Claude
# (~/.claude/projects) or Codex (~/.codex/sessions). devloop writes its own agent log only
# when the turn ends. Requires jq. Ctrl-C to stop.
set -eu
name=${1:?usage: devloop-follow.sh issue-N|pr-N}
root=$(git rev-parse --path-format=absolute --git-common-dir)/..
wt=$(cd "$root" && pwd)/.ssdlc/devloop/worktrees/$name

claude_dir=$HOME/.claude/projects/$(printf '%s' "$wt" | tr '/.' '--')
claude=$(ls -t "$claude_dir"/*.jsonl 2>/dev/null | head -1 || true)
codex=""
for f in $(ls -t "$HOME"/.codex/sessions/*/*/*/*.jsonl 2>/dev/null | head -40); do
  if [ "$(head -n 1 "$f" | jq -r '.payload.cwd // empty' 2>/dev/null)" = "$wt" ]; then codex=$f; break; fi
done

file=$claude
if [ -n "$codex" ] && { [ -z "$claude" ] || [ "$codex" -nt "$claude" ]; }; then file=$codex; fi
[ -n "$file" ] || { echo "no Claude or Codex session found for $wt" >&2; exit 1; }
echo "following $file" >&2

tail -n +1 -F "$file" | jq --unbuffered -r '
  def cut($n): tostring | gsub("\n"; " ") | .[0:$n];
  (.timestamp // "")[11:19] as $t
  | if .type == "assistant" then .message.content[]?
      | if .type == "tool_use" then "\($t) → \(.name) \(.input.command // .input.file_path // .input.pattern // "" | cut(110))"
        elif .type == "text" then "\($t) · \(.text | cut(200))"
        else empty end
    elif .type == "response_item" then .payload
      | if .type == "function_call" or .type == "custom_tool_call" then "\($t) → \(.name) \(.arguments // .input // "" | cut(110))"
        elif .type == "message" and .role == "assistant" then "\($t) · \([.content[]?.text // empty] | join(" ") | cut(200))"
        else empty end
    else empty end'
