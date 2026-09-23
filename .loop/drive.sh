#!/bin/bash
#
# drive.sh — Feature Loop v4 orchestrator. Implements .loop/RULES.md; judgment lives in
# the agents (.loop/agents/), never here. If no rule applies, the tick is a NO-OP.
#
# Usage:   TICKETS="33 34" .loop/drive.sh
# Config:  MAX_TICKS=40  AGENT_TIMEOUT=1800  GATE_TIMEOUT=1800  ENV_FILE=<driver>/.env
#          LOOP_CLONES=$TMPDIR/loop-clones/<repo>  VENV_CMD=...  CLAUDE_BIN=claude
#
# Exit: 0 all tickets merged · 1 current ticket blocked · 2 MAX_TICKS reached
#       3 did not start (lock held by a live drive.sh, or missing config/tool)
#
# State: .loop/state/<id>.json — the only state; GitHub labels are a write-only mirror.
# Logs:  .loop/.ticks/<run>-<n>-<id>.log (full tick output) · .loop/drive.log (one line/tick)
# Per ticket, driver checkout only: .loop/<id>/journal.md, .loop/<id>/feedback.md
#
# bash 3.2 compatible (macOS /bin/bash).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOOP="$REPO_ROOT/.loop"
BIN="$LOOP/bin"
STATE_DIR="$LOOP/state"
TICK_DIR="$LOOP/.ticks"
LOCK="$STATE_DIR/lock"

TICKETS="${TICKETS:-}"
MAX_TICKS="${MAX_TICKS:-40}"
AGENT_TIMEOUT="${AGENT_TIMEOUT:-1800}"
export GATE_TIMEOUT="${GATE_TIMEOUT:-1800}"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"
LOOP_CLONES="${LOOP_CLONES:-${TMPDIR:-/tmp}/loop-clones/$(basename "$REPO_ROOT")}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
export VENV_CMD="${VENV_CMD:-uv venv -q .venv && uv pip install -q --python .venv/bin/python -e \".[dev]\"}"

TARGET=traces-ui
PHASES="new planned implementing gate-pending verified merged blocked"
INIT_STATE='{"phase":"new","round":0,"verify_rounds":0,"plan_attempts":0,"base_sha":null,"head_sha":null,"verified_sha":null,"pr":null,"contract_sha":null,"started_at":null,"fails":{}}'
MAX_COMMENT=60000

cd "$REPO_ROOT" || exit 3

# ---------------------------------------------------------------- output

# Tick stdout is the tick log; `say` also reaches the terminal (fd 3).
say() { local l; l="$(date '+%H:%M:%S')  $*"; echo "$l"; echo "$l" >&3; }
section() { printf '\n----- %s\n' "$1"; cat "$2"; printf '\n----- end %s\n' "$1"; }
oneline() { tr '\n\r' '  ' | cut -c1-300; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ---------------------------------------------------------------- state

st_file() { echo "$STATE_DIR/$1.json"; }

st_get() { # id jq-filter -> value ("" for null)
  local f; f="$(st_file "$1")"
  if [ -f "$f" ]; then jq -r "$2 | if . == null then \"\" else . end" "$f"
  else jq -rn "$INIT_STATE | $2 | if . == null then \"\" else . end"; fi
}

st_update() { # id jq-args... — atomic: tmp + mv
  local id="$1" f tmp; shift
  f="$(st_file "$id")"; tmp="$f.tmp.$$"
  if [ -f "$f" ]; then jq "$@" "$f" >"$tmp"; else jq "$@" <<<"$INIT_STATE" >"$tmp"; fi \
    && mv "$tmp" "$f" || { rm -f "$tmp"; say "state write failed for #$id"; return 1; }
}

phase() { st_get "$1" .phase; }

# ---------------------------------------------------------------- tick results

TR=""
advance() { # id from to [extra jq filter] — record the transition
  st_update "$1" --arg p "$3" ".phase = \$p ${4:+| $4}" || return 1
  mirror "$1" "$3"
  TR="ADVANCED $1 $2 -> $3"
}

block() { # id reason — phase blocked, reason as an issue comment
  local reason; reason="$(printf '%s' "$2" | oneline)"
  st_update "$1" '.phase = "blocked"'
  mirror "$1" blocked
  gh issue comment "$1" --body "drive.sh: **blocked** — $reason" >/dev/null 2>&1 \
    || say "warn: could not comment on #$1"
  TR="BLOCKED $1 $reason"
}

noop() { say "no-op: $*"; TR="NO-OP"; }

set_phase() { st_update "$1" --arg p "$2" '.phase = $p' && mirror "$1" "$2"; }

mirror() { # id phase — write-only; a failure never stops the tick
  local p rm=""
  for p in $PHASES; do [ "$p" = "$2" ] || rm="$rm${rm:+,}agent:$p"; done
  gh issue edit "$1" --add-label "agent:$2" --remove-label "$rm" >/dev/null 2>&1 \
    || say "warn: label mirror failed for #$1 ($2)"
}

# ---------------------------------------------------------------- ticket files

tdir() { mkdir -p "$LOOP/$1"; echo "$LOOP/$1"; }       # driver checkout, never pushed
feedback_file() { echo "$(tdir "$1")/feedback.md"; }   # next agent's input, verbatim
cdir() { echo "$LOOP_CLONES/$1"; }

fetch_issue() { # id -> $W/issue.json, $W/human.md
  gh issue view "$1" --json title,body >"$W/issue.json" 2>"$W/gh.err" || { cat "$W/gh.err"; return 1; }
  jq -r .body "$W/issue.json" | "$BIN/issue-text" human >"$W/human.md"
}
issue_title() { jq -r .title "$W/issue.json"; }
issue_hash() { jq -r .body "$W/issue.json" | "$BIN/issue-text" hash 2>/dev/null; }
issue_contract() { jq -r .body "$W/issue.json" | "$BIN/issue-text" contract 2>/dev/null; }

branch_of() { # existing impl/<id>-* wins, so a retitled issue keeps its branch
  local b; b="$(git for-each-ref --format='%(refname:short)' "refs/heads/impl/$1-*" | head -1)"
  [ -n "$b" ] && echo "$b" || echo "impl/$1-$("$BIN/issue-text" slug "$(issue_title)")"
}

issue_prompt() { printf '# Issue #%s: %s\n\n' "$1" "$(issue_title)"; cat "$W/human.md"; }

contract_from_issue() { # -> $W/contract.md, $W/contract.json
  issue_contract >"$W/contract.md" && [ -s "$W/contract.md" ] \
    && "$BIN/contract-json" <"$W/contract.md" >"$W/contract.json"
}

# ---------------------------------------------------------------- clones

mk_clone() { # dir ref [--branch] — own .git; origin removed so nothing points back here
  local dir="$1" ref="$2"
  rm -rf "$dir"; mkdir -p "$(dirname "$dir")"
  if [ "${3:-}" = "--branch" ]; then
    git clone -q --local --branch "$ref" "$REPO_ROOT" "$dir" || return 1
  else
    { git clone -q --local --no-checkout "$REPO_ROOT" "$dir" \
        && git -C "$dir" checkout -q --detach "$ref"; } || return 1
  fi
  git -C "$dir" remote remove origin
  printf '\n.venv/\n.env\n.tmp/\n*.egg-info/\n' >>"$dir/.git/info/exclude"
}

build_venv() { (cd "$1" && bash -c "$VENV_CMD") >"$W/venv.out" 2>&1 || { tail -40 "$W/venv.out"; return 1; }; }

# ---------------------------------------------------------------- agents

agent_pid_file() { echo "$STATE_DIR/$1.agent"; }

kill_stale_agent() { # previous driver was SIGKILLed (skips traps) while an agent ran
  local f pg; f="$(agent_pid_file "$1")"
  [ -f "$f" ] || return 0
  pg="$(cat "$f")"
  if ps -A -o pgid=,command= | awk -v g="$pg" '$1 == g' | grep -q claude; then
    say "killing orphaned agent process group $pg"
    kill -KILL -- "-$pg" 2>/dev/null
  fi
  rm -f "$f"
}

# spawn role id cwd prompt-file out-prefix -> 0 with the result text in <out>.txt, else 1
spawn() {
  local role="$1" id="$2" cwd="$3" prompt="$4" out="$5" allowed denied pid rc
  case "$role" in
    implementer) allowed="Read,Grep,Glob,Bash,Write,Edit"; denied="" ;;
    *)           allowed="Read,Grep,Glob,Bash";            denied="Write,Edit" ;;
  esac
  local args=(-p --model sonnet --output-format json
    --append-system-prompt-file "$LOOP/agents/$role.md"
    --setting-sources user --settings "$LOOP/agents/sandbox.json"
    --allowedTools "$allowed")
  [ -n "$denied" ] && args+=(--disallowedTools "$denied")

  say "spawn $role for #$id in $cwd (timeout ${AGENT_TIMEOUT}s)"
  rm -f "$out.timeout"
  # Own process group, so the watchdog (and our traps) can kill everything it started.
  ( cd "$cwd" && exec env -u GH_TOKEN -u GITHUB_TOKEN \
      perl -e 'setpgrp(0,0); exec @ARGV or die "exec $ARGV[0]: $!\n"' "$CLAUDE_BIN" "${args[@]}" ) \
    <"$prompt" >"$out.json" 2>"$out.err" &
  pid=$!
  AGENT_PGID="$pid"; echo "$pid" >"$(agent_pid_file "$id")"
  perl -e '$SIG{TERM} = sub { exit 0 }; my ($s, $pg, $mark) = @ARGV; sleep $s;
           open(my $f, ">", $mark); close $f; kill "-TERM", $pg; sleep 5; kill "-KILL", $pg;' \
    "$AGENT_TIMEOUT" "$pid" "$out.timeout" &
  WATCHDOG="$!"
  wait "$pid"; rc=$?
  kill "$WATCHDOG" 2>/dev/null; wait "$WATCHDOG" 2>/dev/null
  kill -KILL -- "-$pid" 2>/dev/null   # whatever the agent left running
  rm -f "$(agent_pid_file "$id")"; AGENT_PGID=""; WATCHDOG=""

  if [ -f "$out.timeout" ]; then say "$role killed after ${AGENT_TIMEOUT}s"; return 1; fi
  if [ "$rc" -ne 0 ]; then say "$role exited $rc"; section "$role stderr" "$out.err"; return 1; fi
  if ! jq -er 'if type == "array" then (map(select(.type == "result")) | last) else . end
               | if .is_error then error("is_error") else .result | strings end' \
        "$out.json" >"$out.txt" 2>/dev/null; then
    say "$role output is not a successful JSON result"; section "$role raw output" "$out.json"
    return 1
  fi
  section "$role output" "$out.txt"
}

# ---------------------------------------------------------------- budgets

over_budget() { # id -> prints the reason and returns 0 when a budget is exhausted
  local r
  r="$(jq -rn --slurpfile s "$(st_file "$1")" --slurpfile c "$W/contract.json" \
         --argjson now "$(date -u +%s)" '
    $s[0] as $s | $c[0].budgets as $b
    | ($s.started_at | if . == null then null else $now - fromdateiso8601 end) as $sec
    | [$s.fails | to_entries[] | select(.value >= 3) | "\(.key) failed \(.value)x"] as $f
    | if $s.round > $b.max_rounds then "budget: round \($s.round) > max_rounds \($b.max_rounds)"
      elif $s.verify_rounds > $b.max_verify_rounds
        then "budget: verify_rounds \($s.verify_rounds) > max_verify_rounds \($b.max_verify_rounds)"
      elif ($f | length) > 0 then "budget: \($f | join(", "))"
      elif $sec != null and $sec > $b.wall_clock_min * 60
        then "budget: \($sec / 60 | floor)m since started_at > wall_clock_min \($b.wall_clock_min)"
      else empty end')"
  [ -n "$r" ] && { echo "$r"; return 0; }
  return 1
}

# ---------------------------------------------------------------- T1 new -> planned

deps_met() { # id -> 0 met · 1 unmet (prints which) · 2 gh failed
  local deps dep merged
  deps="$("$BIN/issue-text" blocked-by <"$W/human.md")"
  [ -n "$deps" ] || return 0
  merged="$(gh pr list --base "$TARGET" --state merged --limit 1000 --json headRefName \
              -q '.[].headRefName' 2>&1)" || { echo "$merged"; return 2; }
  for dep in $deps; do
    printf '%s\n' "$merged" | grep -q "^impl/$dep-" \
      || { echo "#$dep has no merged impl/$dep-* PR into $TARGET"; return 1; }
  done
}

plan_failed() { # id why detail-file
  local n; n=$(( $(st_get "$1" .plan_attempts) + 1 ))
  st_update "$1" --argjson n "$n" '.plan_attempts = $n'
  { echo "Plan attempt $n failed: $2"; echo; cat "$3"; } >"$(feedback_file "$1")"
  if [ "$n" -ge 2 ]; then block "$1" "plan attempt $n failed: $2"; else noop "#$1 plan attempt $n failed: $2"; fi
}

t1() {
  local id="$1" base pending clone why msg sha
  msg="$(deps_met "$id")"
  case $? in
    1) block "$id" "unmet dependency: $msg"; return ;;
    2) noop "gh failed while checking dependencies: $msg"; return ;;
  esac

  # 1. pin base_sha
  base="$(st_get "$id" .base_sha)"
  if [ -z "$base" ]; then
    git fetch -q origin "$TARGET" || { noop "git fetch origin $TARGET failed"; return; }
    base="$(git rev-parse --verify -q "origin/$TARGET")" || { noop "no origin/$TARGET"; return; }
    st_update "$id" --arg b "$base" '.base_sha = $b' || return
    say "#$id base_sha pinned: $base"
  fi

  # check: a contract that passed on a tick that died before recording is reused
  pending="$(tdir "$id")/contract.md"
  if [ -f "$pending" ] && "$BIN/check-contract" --issue "$W/human.md" --contract "$pending" \
       --repo "$REPO_ROOT" --base "$base" >"$W/check.out" 2>&1; then
    say "#$id reusing the checked contract from an interrupted tick"
  else
    rm -f "$pending"
    # 2. planner
    clone="$(cdir "$id")/planner"
    mk_clone "$clone" "$base" || { noop "could not clone base $base"; return; }
    { issue_prompt "$id"
      if [ "$(st_get "$id" .plan_attempts)" -gt 0 ] && [ -f "$(feedback_file "$id")" ]; then
        printf '\n\n# Contract check output from your previous attempt\n\n'; cat "$(feedback_file "$id")"
      fi; } >"$W/planner.prompt"
    if ! spawn planner "$id" "$clone" "$W/planner.prompt" "$W/planner"; then
      rm -rf "$clone"; echo "(no usable output)" >"$W/planner.fb"
      plan_failed "$id" "planner killed, errored or unparseable" "$W/planner.fb"; return
    fi
    rm -rf "$clone"
    why="$(grep -m1 '^PLAN-STOP:' "$W/planner.txt")"
    if [ -n "$why" ] && ! grep -qF '<!-- LOOP CONTRACT v1 -->' "$W/planner.txt"; then
      block "$id" "$why"; return
    fi
    # 3. contract check
    if ! "$BIN/issue-text" extract-contract <"$W/planner.txt" >"$W/contract.md" 2>"$W/check.out"; then
      plan_failed "$id" "no contract in planner output" "$W/check.out"; return
    fi
    if ! "$BIN/check-contract" --issue "$W/human.md" --contract "$W/contract.md" \
         --repo "$REPO_ROOT" --base "$base" >"$W/check.out" 2>&1; then
      section "contract check" "$W/check.out"
      plan_failed "$id" "contract check failed" "$W/check.out"; return
    fi
    cp "$W/contract.md" "$pending"
  fi

  # 4. act: contract below the sentinel, unless the issue already holds exactly it
  if [ "$(issue_contract)" != "$(cat "$pending")" ]; then
    "$BIN/issue-text" compose "$W/human.md" "$pending" >"$W/body.md"
    gh issue edit "$id" --body-file "$W/body.md" >/dev/null || { noop "gh issue edit #$id failed"; return; }
    fetch_issue "$id" || { noop "gh issue view #$id failed"; return; }
    [ "$(issue_contract)" = "$(cat "$pending")" ] || { noop "#$id body lacks the contract after edit"; return; }
  fi
  # record
  sha="$(issue_hash)"
  rm -f "$(feedback_file "$id")"
  advance "$id" new planned ".contract_sha = \"$sha\"" && rm -f "$pending"
}

# ---------------------------------------------------------------- T2 planned -> gate-pending

round_failed() { # id why — failed round, back to planned; last gate/verifier output kept
  local fb r; fb="$(feedback_file "$1")"; r="$(st_get "$1" .round)"
  { echo "Round $r failed before the gate: $2. Nothing from it was kept."; echo
    [ -f "$fb" ] && cat "$fb"; } >"$fb.new" && mv "$fb.new" "$fb"
  st_update "$1" '.round += 1' && set_phase "$1" planned
  noop "#$1 round $r failed: $2"
}

t2() {
  local id="$1" base branch round clone sha remote pr last why rc
  base="$(st_get "$id" .base_sha)"
  branch="$(branch_of "$id")"

  # 1. round 1 only: green base, branch at base_sha, clock starts
  if [ "$(st_get "$id" .round)" -eq 0 ]; then
    say "#$id base check: pytest -q at $base"
    if ! "$BIN/gate" --base-only --repo "$REPO_ROOT" --sha "$base" --workdir "$(cdir "$id")/base" \
         --env-file "$ENV_FILE" >"$W/base.out" 2>&1; then
      section "base check" "$W/base.out"; rm -rf "$(cdir "$id")/base"
      block "$id" "red base: pytest -q fails at $base"; return
    fi
    rm -rf "$(cdir "$id")/base"
    git rev-parse -q --verify "refs/heads/$branch" >/dev/null \
      || git branch "$branch" "$base" || { noop "git branch $branch failed"; return; }
    st_update "$id" --arg t "$(now)" '.round = 1 | .started_at = (.started_at // $t)' || return
  fi
  round="$(st_get "$id" .round)"

  # 2. contract unchanged since T1
  [ "$(issue_hash)" = "$(st_get "$id" .contract_sha)" ] || { block "$id" "contract edited mid-run"; return; }
  contract_from_issue || { block "$id" "contract in the issue does not parse"; return; }
  if why="$(over_budget "$id")"; then block "$id" "$why"; return; fi

  # 3. implementing, round clone with .env
  set_phase "$id" implementing || return
  clone="$(cdir "$id")/round"
  mk_clone "$clone" "$branch" --branch || { round_failed "$id" "could not create the round clone"; return; }
  cp "$ENV_FILE" "$clone/.env"

  # 4. implementer
  { issue_prompt "$id"
    printf '\n\n# Contract\n\n'; cat "$W/contract.md"
    printf '\n\n# Round\n\n%s\n' "$round"
    if [ "$round" -gt 1 ] && [ -f "$(feedback_file "$id")" ]; then
      printf '\n# Previous gate or verifier output\n\n'; cat "$(feedback_file "$id")"
    fi; } >"$W/implementer.prompt"
  if ! spawn implementer "$id" "$clone" "$W/implementer.prompt" "$W/implementer"; then
    rm -rf "$clone"; round_failed "$id" "implementer killed, errored or unparseable"; return
  fi

  # 5. journal (driver checkout only), RESULT line
  { printf '\n## Round %s — %s\n\n' "$round" "$(now)"; cat "$W/implementer.txt"; echo; } \
    >>"$(tdir "$id")/journal.md"
  last="$(grep -v '^[[:space:]]*$' "$W/implementer.txt" | tail -1)"
  case "$last" in
    "RESULT: ESCALATE "*) rm -rf "$clone"; block "$id" "implementer: ${last#RESULT: }"; return ;;
    "RESULT: DONE") ;;
    *) rm -rf "$clone"; round_failed "$id" "no RESULT line"; return ;;
  esac

  # 6. fetch, push, remote == local, draft PR
  git fetch -q "$clone" "$branch:$branch" \
    || { rm -rf "$clone"; round_failed "$id" "fetch from the round clone failed (history rewritten?)"; return; }
  rm -rf "$clone"
  sha="$(git rev-parse "$branch")"
  git push -q origin "$branch" || { round_failed "$id" "git push failed"; return; }
  remote="$(git ls-remote origin "refs/heads/$branch" | cut -f1)"
  [ "$remote" = "$sha" ] || { round_failed "$id" "origin $branch is '$remote', local is $sha"; return; }
  pr="$(st_get "$id" .pr)"
  if [ -z "$pr" ]; then
    pr="$(gh pr list --head "$branch" --base "$TARGET" --state all --json number -q '.[0].number')" \
      || { noop "gh pr list failed (phase stays implementing)"; return; }
    if [ -z "$pr" ]; then
      gh pr create --draft --base "$TARGET" --head "$branch" --title "#$id: $(issue_title)" \
        --body "Built by drive.sh for issue #$id. Verifier verdicts are posted as comments." >/dev/null \
        || { noop "gh pr create failed (phase stays implementing)"; return; }
      pr="$(gh pr list --head "$branch" --base "$TARGET" --state all --json number -q '.[0].number')"
    fi
    st_update "$id" --argjson n "${pr:-null}" '.pr = $n' && [ -n "$pr" ] || { noop "PR number unknown"; return; }
    say "#$id PR #$pr"
  fi

  # 7. implementation gate
  say "#$id gate at $sha"
  "$BIN/gate" --repo "$REPO_ROOT" --sha "$sha" --base "$base" --contract-json "$W/contract.json" \
    --workdir "$(cdir "$id")/gate" --env-file "$ENV_FILE" >"$W/gate.out" 2>&1
  rc=$?
  rm -rf "$(cdir "$id")/gate"
  section "gate" "$W/gate.out"

  # 8. record
  if [ "$rc" -eq 0 ]; then
    advance "$id" planned gate-pending ".head_sha = \"$sha\""
  else
    { echo "Implementation gate FAILED on $sha (round $round):"; echo; cat "$W/gate.out"; } >"$(feedback_file "$id")"
    st_update "$id" '.round += 1' && set_phase "$id" planned
    noop "#$id gate failed, round $round"
  fi
}

# ---------------------------------------------------------------- T3 gate-pending -> verified

t3() {
  local id="$1" head clone attempt why="" verdict=""
  head="$(st_get "$id" .head_sha)"
  contract_from_issue || { block "$id" "contract in the issue does not parse"; return; }
  if why="$(over_budget "$id")"; then block "$id" "$why"; return; fi

  # 1-3. fresh clone at head_sha; discard and re-run once on side effects or bad shape
  for attempt in 1 2; do
    clone="$(cdir "$id")/verify"
    mk_clone "$clone" "$head" || { noop "could not clone $head"; return; }
    cp "$ENV_FILE" "$clone/.env"
    build_venv "$clone" >"$W/venv.err" \
      || { rm -rf "$clone"; noop "venv build failed for the verifier: $(oneline <"$W/venv.err")"; return; }
    { issue_prompt "$id"; printf '\n\n# Contract\n\n'; cat "$W/contract.md"; } >"$W/verifier.prompt"
    if ! spawn verifier "$id" "$clone" "$W/verifier.prompt" "$W/verifier"; then
      why="verifier killed, errored or unparseable"
    elif [ -n "$(git -C "$clone" status --porcelain)" ] || [ "$(git -C "$clone" rev-parse HEAD)" != "$head" ]; then
      why="verifier changed its clone (dirty tree or HEAD moved); verdict discarded"
      git -C "$clone" status --porcelain | head -20
    elif ! "$BIN/parse-verdict" --contract-json "$W/contract.json" <"$W/verifier.txt" >"$W/verdict.json"; then
      why="$(cat "$W/verdict.json")"
    else
      verdict="$(jq -r .verdict "$W/verdict.json")"
    fi
    rm -rf "$clone"
    [ -n "$verdict" ] && break
    say "#$id verifier attempt $attempt: $why"
  done
  [ -n "$verdict" ] || { block "$id" "verifier failed twice: $why"; return; }

  # 4. post verbatim, cut at 60k (full text is in this tick's log)
  { printf 'sha: %s\n\n' "$head"; head -c "$MAX_COMMENT" "$W/verifier.txt"
    [ "$(wc -c <"$W/verifier.txt")" -gt "$MAX_COMMENT" ] \
      && printf '\n\n[cut at %s chars; full text in .loop/.ticks/]\n' "$MAX_COMMENT"
  } >"$W/comment.md"
  gh pr comment "$(st_get "$id" .pr)" --body-file "$W/comment.md" >/dev/null \
    || { noop "gh pr comment failed"; return; }

  # 5. record
  if [ "$verdict" = PASS ]; then
    advance "$id" gate-pending verified ".verified_sha = \"$head\""
  else
    { echo "Verifier verdict on $head:"; echo; cat "$W/verifier.txt"; } >"$(feedback_file "$id")"
    st_update "$id" --argjson f "$(jq -c .fails "$W/verdict.json")" \
      '.verify_rounds += 1 | .round += 1
       | reduce $f[] as $c (.; .fails[$c] = ((.fails[$c] // 0) + 1))' && set_phase "$id" planned
    noop "#$id NEEDS_WORK (failed: $(jq -r '.fails | join(",")' "$W/verdict.json"))"
  fi
}

# ---------------------------------------------------------------- T4 verified -> merged

t4() {
  local id="$1" pr verified state head mergeable i
  pr="$(st_get "$id" .pr)"; verified="$(st_get "$id" .verified_sha)"
  gh pr view "$pr" --json state,headRefOid,isDraft,mergeable >"$W/pr.json" \
    || { noop "gh pr view #$pr failed"; return; }
  state="$(jq -r .state "$W/pr.json")"; head="$(jq -r .headRefOid "$W/pr.json")"
  # 1. already merged -> record
  if [ "$state" = MERGED ]; then
    advance "$id" verified merged; rm -rf "$(cdir "$id")"; return
  fi
  # 2. new code needs a new gate
  if [ "$head" != "$verified" ]; then
    echo "PR head moved to $head after $verified was verified; re-gating." >"$(feedback_file "$id")"
    advance "$id" verified planned; return
  fi
  # 3. ready; mergeable (poll up to 60s while UNKNOWN)
  if [ "$(jq -r .isDraft "$W/pr.json")" = true ]; then
    gh pr ready "$pr" >/dev/null || { noop "gh pr ready #$pr failed"; return; }
  fi
  mergeable="$(jq -r .mergeable "$W/pr.json")"
  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    [ "$mergeable" = UNKNOWN ] || break
    sleep 5
    mergeable="$(gh pr view "$pr" --json mergeable -q .mergeable)" || mergeable=UNKNOWN
  done
  [ "$mergeable" = MERGEABLE ] || { block "$id" "PR #$pr is $mergeable"; return; }
  # 4. squash into traces-ui: our message, no closing keywords, only the verified sha
  gh pr merge "$pr" --squash --subject "#$id: $(issue_title)" --body "" --match-head-commit "$verified" \
    || { noop "gh pr merge #$pr failed"; return; }
  # 5. record
  advance "$id" verified merged; rm -rf "$(cdir "$id")"
}

# ---------------------------------------------------------------- tick

tick() {
  local id="$1" p
  W="$(mktemp -d "${TMPDIR:-/tmp}/drive-tick.XXXXXX")"
  AGENT_PGID=""; WATCHDOG=""
  trap '[ -n "$AGENT_PGID" ] && kill -KILL -- "-$AGENT_PGID" 2>/dev/null
        [ -n "$WATCHDOG" ] && kill "$WATCHDOG" 2>/dev/null
        rm -rf "$W"; [ -n "$TR" ] || TR="NO-OP"; echo "TICK-RESULT: $TR"' EXIT
  trap 'say "interrupted"; exit 130' INT TERM

  kill_stale_agent "$id"
  if [ "$(phase "$id")" = implementing ]; then
    say "#$id recovery: the previous tick died while implementing"
    rm -rf "$(cdir "$id")/round"
    round_failed "$id" "driver died mid-round"
    TR=""
  fi
  fetch_issue "$id" || { noop "gh issue view #$id failed"; return; }
  p="$(phase "$id")"
  say "#$id phase=$p round=$(st_get "$id" .round)"
  case "$p" in
    new) t1 "$id" ;;
    planned) t2 "$id" ;;
    gate-pending) t3 "$id" ;;
    verified) t4 "$id" ;;
    *) noop "#$id phase $p has no transition" ;;
  esac
}

# ---------------------------------------------------------------- run

stop() { say "STOP($1) $2"; exit "$1"; }

exec 3>&1
mkdir -p "$STATE_DIR" "$TICK_DIR"

[ -n "$TICKETS" ] || { echo "TICKETS is required, e.g. TICKETS=\"33 34\"" >&2; exit 3; }
for t in git gh jq uv perl sandbox-exec "$CLAUDE_BIN"; do
  command -v "$t" >/dev/null || { echo "missing tool: $t" >&2; exit 3; }
done
[ -f "$ENV_FILE" ] || { echo "missing ENV_FILE=$ENV_FILE (copied into agent and gate clones)" >&2; exit 3; }

# Lock: mkdir is atomic. Live holder -> exit; dead holder -> take it.
if ! mkdir "$LOCK" 2>/dev/null; then
  holder="$(cat "$LOCK/pid" 2>/dev/null)"
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
    echo "drive.sh already running (pid $holder)" >&2; exit 3
  fi
  say "taking stale lock (pid ${holder:-?} is gone)"
  rm -rf "$LOCK"; mkdir "$LOCK" || exit 3
fi
echo $$ >"$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT
trap 'exit 130' INT TERM

for p in $PHASES; do gh label create "agent:$p" --force --color BFD4F2 >/dev/null 2>&1; done

RUN_ID="$(date +%Y%m%d-%H%M%S)"
say "=== drive start $RUN_ID · TICKETS=\"$TICKETS\" · MAX_TICKS=$MAX_TICKS · clones: $LOOP_CLONES"
n=0
while :; do
  cur=""
  for id in $TICKETS; do [ "$(phase "$id")" = merged ] || { cur="$id"; break; }; done
  [ -n "$cur" ] || stop 0 "all merged: $TICKETS"
  [ "$(phase "$cur")" = blocked ] \
    && stop 1 "#$cur blocked (reason: issue comment / tick log). Fix it, then edit or delete $(st_file "$cur")."
  [ "$n" -ge "$MAX_TICKS" ] && stop 2 "MAX_TICKS=$MAX_TICKS reached"
  n=$((n + 1))
  log="$TICK_DIR/$RUN_ID-$(printf '%03d' "$n")-$cur.log"
  say "tick $n/$MAX_TICKS #$cur  ($log)"
  ( TR=""; tick "$cur" ) >"$log" 2>&1
  res="$(tail -1 "$log")"
  case "$res" in TICK-RESULT:*) ;; *) res="TICK-RESULT: NO-OP (tick printed no result; see log)" ;; esac
  printf '%s\ttick %s\t#%s\t%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$n" "$cur" "$res" >>"$LOOP/drive.log"
  say "$res"
done
