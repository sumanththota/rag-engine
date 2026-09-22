# Feature Loop — rules

The specification `drive.sh` implements. Every rule is a file read, a command or an exit
code. Judgment belongs to agents, never here. If no rule applies, the tick is a NO-OP —
never an invented transition.

## Actors

`drive.sh` is the only process that pushes, labels, comments, merges or edits an issue.
Agents do one job each and return text on stdout.

Every agent is launched from the driver checkout, so prompts and limits never come from
the ticket branch. Prompt on stdin — the tool flags are variadic and swallow a trailing
positional prompt:
```
claude -p --model sonnet --output-format json \
  --append-system-prompt-file .loop/agents/<role>.md \
  --setting-sources user --settings .loop/agents/sandbox.json \
  --allowedTools "<allowed>" --disallowedTools "<denied>" < prompt.txt
```

| Role | Runs in | Allowed | Denied |
|---|---|---|---|
| planner | fresh clone at `base_sha` | Read,Grep,Glob,Bash | Write,Edit |
| implementer | round clone on `impl/<id>-<slug>` | Read,Grep,Glob,Bash,Write,Edit | — |
| verifier | fresh clone at `head_sha` | Read,Grep,Glob,Bash | Write,Edit |

**The boundary is the OS sandbox, not the tool list.** Probed 2026-09-22:
- `--disallowedTools Write,Edit` removes those tools entirely.
- `Bash(...)` deny patterns match the command string only: `/usr/bin/git push` and
  `git -c x=y push` got through. Not a boundary; not used.
- `sandbox.json` limits every Bash process: network only to PyPI + localhost (GitHub
  unreachable for `curl`, `gh`, `/usr/bin/git`; Postgres on :5433 reachable); writes only
  inside the working dir, temp dirs and — for a worktree — its shared `.git` (home and
  other dirs blocked).
- So agents cannot push, merge, label or comment, by any binary.

**Clones, not worktrees.** A worktree's `.git` is shared with the driver and the sandbox
lets agents write it — they could move the driver's refs. Each agent instead gets
`git clone --local` (hardlinked objects, cheap) with its own `.git`. `drive.sh` fetches results from
the clone. Planner and verifier edits via Bash stay inside a clone that is discarded.

Timeout: a watchdog kills the agent's process group after `AGENT_TIMEOUT` (default 30m).
A killed or unparseable agent = a failed attempt of that phase.

## State

`.loop/state/<id>.json` in the driver checkout is the only state. Written atomically
(tmp + `mv`). GitHub labels mirror `phase` for humans; `drive.sh` never reads them.

```json
{"phase": "new", "round": 0, "verify_rounds": 0, "plan_attempts": 0,
 "base_sha": null, "head_sha": null, "verified_sha": null, "pr": null,
 "contract_sha": null, "started_at": null, "fails": {}}
```

Phases: `new` → `planned` → `implementing` → `gate-pending` → `verified` → `merged`.
Any → `blocked` (terminal for the run). No state file = `new`.

## Run

- **Lock.** `mkdir .loop/state/lock`, PID inside. Held by a live PID → exit. Dead PID →
  take it.
- **Scope.** `TICKETS="33 34 …"`, in the order given. Nothing else is touched.
- **One ticket at a time.** Current ticket = first in `TICKETS` not `merged`. It runs to
  `merged` or `blocked` before the next starts.
- **Dependencies.** Parsed only from `## Blocked by` above the sentinel. Met when that
  issue's PR is merged into `traces-ui`, in scope or not. Unmet → `blocked`.
- **Stop.** Current ticket `blocked` → exit 1. All `merged` → exit 0. `MAX_TICKS` → exit 2.

## Each tick

Apply exactly one transition to the current ticket. Every transition is
**check → act → record**: before acting, check whether the side effect already happened
(PR exists, PR merged, contract written); if so, only record it.

`phase: implementing` at tick start means the previous tick died: remove the round
clone, count a failed round, set `planned`.

Print `TICK-RESULT:` as the last line.

## T1 · `new` → `planned`

1. Pin `base_sha` = `git rev-parse origin/traces-ui`.
2. Spawn **planner** with the issue text above the sentinel. It prints contract YAML,
   or `PLAN-STOP: <reason>` → `blocked`.
3. **Contract check** (`.loop/bin/check-contract`), all must hold:
   - YAML matches the schema (see Contract)
   - every issue acceptance criterion (the n-th list item under the issue's
     `## Acceptance criteria`) is covered by ≥1 contract criterion with `from_issue: n`
   - every `owned` path exists at `base_sha` or ends in ` (NEW)`
   - every `oracles` path exists at `base_sha` and matches `protected`
   - `behavioral` → has `verify_via` + `mutation_target`; `mechanical` → has `check`
   - no criterion needs a browser
4. Pass → replace everything after the sentinel in the issue body with the contract,
   store `contract_sha`, set `planned`. Fail → `plan_attempts` +1, re-plan with the
   check output; at 2 → `blocked`.

## T2 · `planned` → `gate-pending`

1. **Round 1 only:** `pytest -q` in a fresh clone at `base_sha` must exit 0, else
   `blocked` ("red base"). Create `impl/<id>-<slug>` at `base_sha`. Set `started_at`.
2. Contract hash in the issue must equal `contract_sha`, else `blocked` ("contract
   edited mid-run").
3. Set `implementing`. Create the round clone, copy `.env` in.
4. Spawn **implementer** with: issue text, contract, round number, previous gate or
   verifier output verbatim.
5. Append its output to `.loop/<id>/journal.md` in the driver checkout (never on the
   ticket branch). Last line `RESULT: ESCALATE <reason>` → `blocked`. No `RESULT:` line →
   failed round.
6. Fetch the branch from the clone and push it. `git ls-remote origin <branch>` must
   equal `git rev-parse <branch>`. No PR yet → open a draft PR into `traces-ui`.
7. **Implementation gate** (`.loop/bin/gate`), in a fresh clone at the pushed
   sha with `.env` and a fresh `.venv`, `BASE_SHA` exported; all must hold:
   - `git diff --name-only <base_sha>...<sha>` is non-empty
   - every path is in `owned`; none matches `protected`
   - `pytest -q` exits 0, no `-k`
   - every `mechanical` criterion's `check` exits 0
8. Pass → `head_sha` = sha, `gate-pending`. Fail → `round` +1, `planned`; gate output is
   the next implementer's input.

## T3 · `gate-pending` → `verified`

1. Spawn **verifier** in a fresh clone at `head_sha` with: issue text, contract.
2. **Evidence of no change:** clone clean (`git status --porcelain` empty) and HEAD ==
   `head_sha`. Else discard the verdict, re-run once; again → `blocked`.
3. **Parse.** Line 1 exactly `PASS` or `NEEDS_WORK`, then one `C<n>: PASS|FAIL` per
   behavioral criterion. Anything else → re-run once; again → `blocked`.
4. Post to the PR: `sha: <head_sha>` + the verdict verbatim (cut at 60k; full text in
   `.loop/.ticks/`).
5. `PASS` → `verified_sha` = `head_sha`, `verified`. `NEEDS_WORK` → `verify_rounds` +1,
   `fails[C<n>]` +1 per FAIL, `round` +1, `planned`; the verdict is the next
   implementer's input.

## T4 · `verified` → `merged`

1. PR already merged → record `merged`.
2. PR head ≠ `verified_sha` → `planned` (new code needs a new gate).
3. `gh pr ready`. Poll `mergeable` up to 60s while `UNKNOWN`. Not `MERGEABLE` → `blocked`.
4. Squash-merge into `traces-ui` with a `drive.sh`-written message (`#<id>: <title>`, no
   closing keywords). Never into `master`.
5. Record `merged`. Remove clones.

No re-test: one ticket at a time means `traces-ui` has not moved since `base_sha`, and
`verified_sha` is the exact code that passed.

## Budgets → `blocked`

From the contract, checked before every spawn:
- `round` > `max_rounds`
- `verify_rounds` > `max_verify_rounds`
- any `fails[C<n>]` ≥ 3
- now − `started_at` > `wall_clock_min`

Reason posted as an issue comment.

## Contract

In the issue body, after the sentinel. Everything above the sentinel is the human's.

````
<!-- LOOP CONTRACT v1 -->
```yaml
owned: [path, path (NEW)]
protected: [glob]          # any diff touching these fails the gate
oracles: [path]            # answer keys, committed before the run; must match protected
criteria:
  - id: C1
    from_issue: 1          # 1-based index of the issue's acceptance criterion
    kind: behavioral       # verifier judges; needs verify_via + mutation_target
    verify_via: <command or HTTP surface>
    mutation_target: <mechanism to break>
  - id: C2
    from_issue: 2
    kind: mechanical       # gate runs check; exit 0 = pass
    check: <shell command; runs at clone root, $BASE_SHA set, .venv present>
budgets: {max_rounds: 6, max_verify_rounds: 3, wall_clock_min: 90}
```
````

## TICK-RESULT

Last line of every tick, exactly one of:
```
TICK-RESULT: NO-OP
TICK-RESULT: ADVANCED <id> <from> -> <to>
TICK-RESULT: BLOCKED <id> <one-line reason>
```
