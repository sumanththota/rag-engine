#!/bin/bash
# Promote `ralph:queued` issues to `ready-for-agent` once every native
# "blocked by" dependency is closed or labelled `agent:done`.
# Runs on the host before each iteration so the next slice unlocks itself.
set -eo pipefail

cd "$(dirname "$0")/.."

repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner)

queued=$(gh issue list --label ralph:queued --state open --json number,labels \
  --jq '.[] | select([.labels[].name] | index("agent:blocked") | not) | .number')

for n in $queued; do
  pending=$(gh api "repos/$repo/issues/$n/dependencies/blocked_by" \
    --jq '[.[] | select(.state == "open" and ([.labels[].name] | index("agent:done") | not))] | length')
  if [[ "$pending" == "0" ]]; then
    gh issue edit "$n" --remove-label ralph:queued --add-label ready-for-agent >/dev/null
    echo "Promoted #$n to ready-for-agent"
  fi
done
