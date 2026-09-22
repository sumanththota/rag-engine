#!/usr/bin/env bash
#
# drive.sh — the Feature Loop orchestrator.
#
# CURRENT: the v3 tick clock. It still runs `claude -p "/loop"`, which no longer exists
# on this branch — do not run it.
# TARGET (.loop/CHECKLIST.md §2): this script implements .loop/RULES.md itself and spawns
# .loop/agents/* only for planner/implementer/verifier work. See .loop/DESIGN.md.
#
# Stop conditions, all checked BEFORE each tick:
#   - any issue labelled agent:blocked         -> exit 1  (a human is needed)
#   - TICK-RESULT: BLOCKED from the last tick  -> exit 1
#   - MAX_TICKS reached                        -> exit 2  (runaway guard)
#   - two consecutive NO-OP ticks              -> exit 0  (backlog drained)
#   - two consecutive ticks with no TICK-RESULT-> exit 3  (orchestrator went silent;
#                                                          silence is a failure, not a wait)
#
# Usage:   .loop/drive.sh
# Config:  MAX_TICKS=40  TICK_SECONDS=30  PERMISSION_MODE=acceptEdits  .loop/drive.sh
#
# Ctrl-C at any point. A tick already in flight finishes its current tool call and dies
# with the shell; the protocol's state lives in GitHub labels and .loop/, never in the
# driver, so an interrupted run is resumed by simply starting this script again.

set -uo pipefail

MAX_TICKS="${MAX_TICKS:-40}"
TICK_SECONDS="${TICK_SECONDS:-30}"
# The orchestrator runs gh/git/pytest and spawns subagents. If ticks stall waiting on a
# permission prompt, add the specific commands to .claude/settings.json rather than
# reaching for bypassPermissions — an unattended agent with blanket approval can merge.
PERMISSION_MODE="${PERMISSION_MODE:-acceptEdits}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 3
LOG="$REPO_ROOT/.loop/drive.log"
TICK_DIR="$REPO_ROOT/.loop/.ticks"
mkdir -p "$TICK_DIR"

say() { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*"; }
log() { printf '%s\t%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >>"$LOG"; }

note() { say "$*"; log "$*"; }

finish() { # exit_code, message
  note "STOP($1) $2"
  say "log: .loop/drive.log   per-tick output: .loop/.ticks/"
  exit "$1"
}

blocked_issues() {
  gh issue list --state all --label agent:blocked --json number --jq '[.[].number] | join(" ")' 2>/dev/null
}

note "=== drive start · branch $(git branch --show-current) · max_ticks=$MAX_TICKS · every ${TICK_SECONDS}s"

consecutive_noop=0
consecutive_silent=0
tick=0

while :; do
  # ---- stop conditions, before spending anything on a tick ----
  if (( tick >= MAX_TICKS )); then
    finish 2 "tick cap reached ($MAX_TICKS). Raise MAX_TICKS to continue."
  fi

  if b="$(blocked_issues)" && [[ -n "$b" ]]; then
    finish 1 "agent:blocked on issue(s): $b — a human decision is required. Resolve, remove the label, then rerun."
  fi

  # ---- one tick ----
  tick=$((tick + 1))
  out="$TICK_DIR/tick-$(printf '%03d' "$tick").txt"
  note "tick $tick/$MAX_TICKS -> $out"

  claude -p "/loop" --permission-mode "$PERMISSION_MODE" >"$out" 2>&1
  rc=$?

  # The LAST TICK-RESULT line wins: subagent output can echo earlier ones.
  result="$(grep -a '^TICK-RESULT:' "$out" | tail -1)"

  if [[ -z "$result" ]]; then
    consecutive_silent=$((consecutive_silent + 1))
    consecutive_noop=0
    note "tick $tick: NO TICK-RESULT LINE (claude rc=$rc) [$consecutive_silent/2]"
    if (( consecutive_silent >= 2 )); then
      finish 3 "two consecutive ticks emitted no TICK-RESULT. Read $out — the orchestrator is erroring, or loop.md step 9 is not being followed."
    fi
    sleep "$TICK_SECONDS"
    continue
  fi

  consecutive_silent=0
  note "tick $tick: $result"

  case "$result" in
    *"NO-OP"*)
      consecutive_noop=$((consecutive_noop + 1))
      if (( consecutive_noop >= 2 )); then
        finish 0 "two consecutive NO-OP ticks — nothing ready to advance. Backlog drained or nothing labelled ready-for-agent."
      fi
      ;;
    *"BLOCKED"*)
      finish 1 "orchestrator blocked this tick: ${result#TICK-RESULT: }"
      ;;
    *"ADVANCED"*)
      consecutive_noop=0
      ;;
    *)
      consecutive_noop=0
      note "tick $tick: unrecognised TICK-RESULT form, treating as progress"
      ;;
  esac

  sleep "$TICK_SECONDS"
done
