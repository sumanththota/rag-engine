#!/bin/bash
set -eo pipefail

if [ -z "$1" ]; then
  echo "Usage: $0 <iterations>"
  exit 1
fi

cd "$(dirname "$0")/.."

# Refuse to run on the default branch: ralph commits directly.
branch=$(git branch --show-current)
if [[ "$branch" == "master" || "$branch" == "main" || -z "$branch" ]]; then
  echo "Refusing to run on '$branch'. Check out a feature branch first."
  exit 1
fi

sandbox=ralph-rag-engine
pg_port=5433 # host Postgres port, from DATABASE_URL in .env

# One-time sandbox setup. The sandbox proxy blocks direct TCP to the host, so
# Postgres is reached by tunnelling through the proxy's HTTP CONNECT (allow +
# bypass TLS interception for that one port). The Linux venv lives outside the
# mounted workspace so it never clobbers the host's macOS .venv.
if ! docker sandbox ls | awk 'NR>1 {print $1}' | grep -qx "$sandbox"; then
  docker sandbox create --name "$sandbox" -q claude .
  docker sandbox network proxy "$sandbox" --allow-host "localhost:$pg_port" --bypass-host "localhost:$pg_port"
  docker sandbox exec "$sandbox" bash -c 'echo "export UV_PROJECT_ENVIRONMENT=/home/agent/.venv-rag-engine" > /etc/sandbox-persistent.sh'
fi

# jq filter to extract streaming text from assistant messages
stream_text='select(.type == "assistant").message.content[]? | select(.type == "text").text // empty | gsub("\n"; "\r\n") | . + "\r\n\n"'

# jq filter to extract final result
final_result='select(.type == "result").result // empty'

for ((i=1; i<=$1; i++)); do
  tmpfile=$(mktemp)
  trap "rm -f $tmpfile" EXIT

  ralph/promote.sh

  commits=$(git log -n 5 --format="%H%n%ad%n%B---" --date=short 2>/dev/null || echo "No commits found")
  issues=$(gh issue list --label ready-for-agent --state open --json number,title,body,comments --limit 50 2>/dev/null || echo "No issues found")
  prompt=$(cat ralph/prompt.md)

  # Forward sandbox localhost:$pg_port to host Postgres via the proxy, so
  # DATABASE_URL works unchanged. Exits harmlessly if already listening.
  docker sandbox exec -d "$sandbox" socat "TCP-LISTEN:$pg_port,bind=127.0.0.1,fork,reuseaddr" \
    "PROXY:host.docker.internal:localhost:$pg_port,proxyport=3128"

  docker sandbox run "$sandbox" -- \
    --verbose \
    --print \
    --output-format stream-json \
    "Previous commits: $commits Issues: $issues $prompt" \
  | grep --line-buffered '^{' \
  | tee "$tmpfile" \
  | jq --unbuffered -rj "$stream_text"

  result=$(jq -r "$final_result" "$tmpfile")

  if [[ "$result" == *"<promise>NO MORE TASKS</promise>"* ]]; then
    echo "Ralph complete after $i iterations."
    exit 0
  fi
done
