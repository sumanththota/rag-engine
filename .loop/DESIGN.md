# Feature Loop — design v4 (unattended)

As of 2026-09-22. Branch `flp/driver`, not merged. `docs/feature-loop-protocol.html` (v3)
is still the shipped manual.

Read with: `RULES.md` (what `drive.sh` enforces), `CHECKLIST.md` (what's left before the
first run), `INDEX.md` (ticket history).

---

## Goal

Point the loop at a set of GitHub issues and walk away for hours. No approvals, no human
merge between tickets. The human writes issues before the run and reads results after.

**Principle:** every human decision moves to T=0, and every safeguard that can be a
script is a script. A gate at T=0 costs an hour once; a gate at T=3h costs the night.

## The problem this design answers

> What can say "no" that is not a model deciding to?

In v3, nothing. Every check was a model choosing to run it and choosing to report
honestly, and the history shows that fails: implementer self-reports false 3× (#10, #12),
mutation counts under-run 2×, a `CONVENTIONS.md` rule broken on #18, an orchestrator that
wrote its own authorisation on #32. With no human, a verifier's PASS *is* the merge.

**Answer:** the orchestrator is code. Anything that can be a command is run by that code
and decided by an exit code. Models only do work that needs judgment.

---

## Architecture

```
┌──────────────── drive.sh — orchestrator, code ─────────────────┐
│  state file → spawn agent → run gate → write state             │
│                                                                │
│  planner ─▶ contract check ─▶ implementer ─▶ gate ─▶ verifier  │
│                                  ▲            └─fail─┤         │
│                                  └──── NEEDS_WORK ───┘         │
│                                                                │
│  agents return stdout → drive.sh writes issue / journal / PR   │
└────────────────────────────────────────────────────────────────┘
```

Same shape as Anthropic's planner/generator/evaluator harness, with two additions: a
model-free gate between generator and evaluator, and agents that never write shared
artifacts directly.

| Role | Is | Does | Cannot |
|---|---|---|---|
| **drive.sh** | bash | state, branches, clones, push, PR, labels, gates, merge | judge |
| **planner** | sonnet | issue → YAML contract on stdout | write files, read journals |
| **implementer** | sonnet | code + tests, commits | push, `gh`, touch `protected` |
| **verifier** | sonnet | judges behavioral criteria, own mutations | write, read journals |

### Decisions that shape everything

- **State file, not labels.** `.loop/state/<id>.json` is the only state. Labels mirror it
  for humans; `drive.sh` never reads them. Labels can't hold counters, lag on read, and
  drifted from other state twice (#18, #32).
- **One ticket at a time.** The current ticket runs to merged or blocked before the next
  starts. `traces-ui` never moves under a ticket, so no staleness, no re-test at merge.
- **Driver supplies the agents.** Prompts live in `.loop/agents/`, launched with CLI
  flags for model and tools. The ticket branch can't change who the agents are or what
  they may do.
- **The OS sandbox is the boundary.** Agents run with no network except PyPI and
  localhost, and write only inside their own `git clone --local`. They cannot push,
  merge or touch GitHub by any binary. Tool deny-patterns were probed and are bypassable
  (`/usr/bin/git push`), so they are not relied on.
- **Rules only in the orchestrator.** Flaky test → failed round. Order → `TICKETS`.
  Stuck → budget → blocked, human looks after the run.
- **Fresh context per agent.** Each is its own `claude -p` process.
- **Idempotent transitions.** Each is check → act → record, so a crash anywhere resumes
  cleanly: the next tick sees what already happened and records it.

### Journals

The implementer's stdout is its journal entry; `drive.sh` appends it to
`.loop/<id>/journal.md` in the driver checkout. Journals never reach `traces-ui`, so the
planner — which reads code there — can't see them, and the verifier never gets them.
Lessons reach future tickets only through a human-written retro → `CONVENTIONS.md`, after
the run.

## Contract

Issue body = human text, then `<!-- LOOP CONTRACT v1 -->`, then a YAML block the planner
writes (schema: `RULES.md` § Contract).

- **Two kinds of criterion.** `mechanical` has a shell `check` the gate runs.
  `behavioral` has `verify_via` + `mutation_target` for the verifier. The planner prefers
  mechanical — #33's reference contract is 5 of 6.
- **Coverage is checked.** Every issue criterion must be covered (`from_issue`). The
  verifier also gets the issue text, so a weakened criterion is caught.
- **Planner reads code, not memory.** #10 lost 2 of 4 rounds to guessed API details.
- **Human doesn't read contracts.** The contract check gates them. A contract hash
  detects edits mid-run.

Reference: `.loop/33-contract-draft.md`.

## Gates

| Gate | Runs | Decides by |
|---|---|---|
| Contract check | after planner | schema, coverage, paths exist, oracles present |
| Base check | round 1 | `pytest` on `base_sha` exits 0 |
| Implementation gate | after implementer, fresh clone at pushed sha | non-empty diff ⊆ `owned`, `protected` untouched, `pytest` exit 0, every `check` exit 0 |
| Verifier | after gate | per-criterion PASS/FAIL, parsed; clone clean, HEAD unchanged |
| Merge | before merge | PR head == `verified_sha`, `MERGEABLE` |

Only the verifier is a model, and its output shape, side effects and repeat failures are
all checked mechanically.

**Oracles** (answer keys, e.g. #33's golden snapshots) are committed to `traces-ui` in
setup, before the run, and listed as `protected`. An implementer that can rewrite the
oracle has no oracle.

**Next step up: CI.** `drive.sh` can't gate a merge independently of itself. GitHub
Actions with branch protection can, but needs a Postgres service container — tests hit a
live DB by design (`CONVENTIONS.md` §7). After the first clean run.

## Scope and sequencing

Review UI is server-rendered Python f-strings in `app/main.py`, so `TestClient` covers
most criteria.

- **#33–#38 through the loop.** All criteria HTTP-assertable.
- **#39, #40 by hand.** Keyboard binding and 375px layout need a browser. The loop must
  not build the Playwright harness that would then police it.

```
#33 ──┬──▶ #34 ──┬──▶ #37
      │          ├──▶ #38 ──┐
      │          └──▶ #39   ├──▶ #40
      ├──▶ #35 ─────────────┘
      └──▶ #36
```

The planner runs per ticket at its `base_sha`, so it always reads merged code. #33 still
lays down `# region:` markers (`trace-list-rows`, `sidebar-filters`, `annotation-panel`,
`detail-body`, `styles`) so later tickets can reason about stable names, not lines.

## Merge strategy

Per-ticket squash-merge into `traces-ui`. One `traces-ui` → `master` PR at the end,
merged by the human. Small reviewable PRs; every failure confined to a deletable branch.

---

## Evidence base

- **0 of 5** past tickets finished without a human decision. 19 verify rounds, 14
  rejections (74%), zero first-round PASS.
- Without `mutation_target` the verifier invents its own adversary; with only the
  published one, the implementer can satisfy exactly that. Hence both (#12, #18).
- A prose orchestrator in an undefined state doesn't idle — it infers a transition and
  takes it (#32).
- A result a human can read isn't one a caller can read → parsed output everywhere.
- `SIGKILL` skips bash `EXIT` traps; a killed tick leaves branch, PR, worktree behind.
- `acceptEdits` alone stalls headless Bash.
- Shared Postgres traces table; `db_scope` is naming, not isolation. `pg_dump` first.
- macOS has no `timeout` or `flock`.
- Tool deny-patterns match the command string: `/usr/bin/git push` gets through. The OS
  sandbox blocks it (probed 2026-09-22).

## Rejected

- **Model orchestrator.** Wrote its own authorisation on #32; ~4 roles in one on #18.
- **Labels as state.** No counters, read lag, drift.
- **Parallel tickets.** Staleness and merge-result re-tests, for throughput we don't need.
- **Planner that prompts itself.** No fixed point.
- **One big PR.** Review quality collapses with diff size.
- **Playwright built by the loop.** The loop would build its own police.

## Open

1. `allowUnsandboxedCommands: false` should stop a model from asking to run a command
   outside the sandbox. Not probed — the session's own safety classifier blocked an
   escape test. Run it by hand before the first run (CHECKLIST §4).
2. When to add CI.

## Status

Rules and prompts written; `drive.sh` is still the v3 tick clock. Full run not done.
First: #33 alone, `MAX_TICKS=10`, attended. Remaining work: `CHECKLIST.md`.
