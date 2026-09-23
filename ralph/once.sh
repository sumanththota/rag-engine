#!/bin/bash
set -eo pipefail

cd "$(dirname "$0")/.."

# Refuse to run on the default branch: ralph commits directly.
branch=$(git branch --show-current)
if [[ "$branch" == "master" || "$branch" == "main" || -z "$branch" ]]; then
  echo "Refusing to run on '$branch'. Check out a feature branch first."
  exit 1
fi


issues=$(gh issue list --label ready-for-agent --state open --json number,title,body,comments --limit 50 2>/dev/null || echo "No issues found")
commits=$(git log -n 5 --format="%H%n%ad%n%B---" --date=short 2>/dev/null || echo "No commits found")
prompt=$(cat ralph/prompt.md)

claude --permission-mode acceptEdits \
  "Previous commits: $commits Issues: $issues $prompt"
