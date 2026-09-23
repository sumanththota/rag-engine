#!/bin/bash
# Apply the issue update the agent left in ralph/.outcome.json. Runs on the
# host, so GitHub credentials never enter the sandbox.
set -eo pipefail

cd "$(dirname "$0")/.."

f=ralph/.outcome.json
[ -f "$f" ] || exit 0

n=$(jq -r '.issue' "$f")
done=$(jq -r '.done' "$f")
note=$(jq -r '.note // empty' "$f")

if ! [[ "$n" =~ ^[0-9]+$ ]]; then
  echo "Ignoring $f: .issue is not an issue number ($n)" >&2
  exit 1
fi

if [ -n "$note" ]; then
  gh issue comment "$n" --body "$note" >/dev/null
fi

if [ "$done" == "true" ]; then
  gh issue edit "$n" --remove-label ready-for-agent --add-label agent:done >/dev/null
  echo "Marked #$n agent:done"
else
  echo "Commented on #$n (still ready-for-agent)"
fi

rm "$f"
