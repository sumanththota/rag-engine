# Loop Engineering — design v4 (toward unattended)

Working design as of 2026-09-21. Supersedes nothing yet: `docs/feature-loop-protocol.html`
(v3) is still the shipped manual, and `.claude/commands/loop.md` is still the shipped
orchestrator. This file records what v4 should be and why, so the next session does not
re-derive it.

**Read first:** `.loop/README.md`, `.loop/INDEX.md` (ticket ledger), then this file.

---

## The goal, stated precisely

Point the loop at a GitHub backlog and walk away for hours. No approvals mid-run, no
"stop and report", no human merge between tickets. The human writes the issues up front
and reads results afterwards.

v1–v3 were built the opposite way — they stop for a human at several gates. Every
decision below is about closing that distance without losing what the gates were
protecting.

**The organising principle:** lean does not mean fewer safeguards. It means fewer
*prose* safeguards, replaced by fewer, stronger *mechanical* ones. And every human
decision moves to before the run starts. A gate at T=0 costs an hour once; a gate at
T=3h costs the night.

---

## The one unresolved question

> **What in this system can say "no" that is not a model deciding to?**

Today: nothing. Every check is a model choosing to run it and choosing to report
honestly. The repo's own history says that fails — implementer self-reports have been
false 3× across #10 and #12; mutation counts were under-run 2×; #18 violated a
`CONVENTIONS.md` rule that existed by name before the ticket started.

Remove the human and the verifier's PASS *becomes* the merge. So this question is the
whole design, not a detail of it.

Two candidate non-model gates, and they are not exclusive:

| | `.loop/drive.sh` runs the checks | GitHub Actions runs them |
|---|---|---|
| Is it a model? | no | no |
| Cost | ~40 lines, available now | workflow **+ a Postgres service container** |
| Runs against | local worktree, between ticks | the pushed sha, every push |
| Can gate the merge itself | **no** — same process that merges | yes, via branch protection |

The Postgres row is the real cost: this repo's tests hit a live database by design
(`tests/test_traces.py`, and `CONVENTIONS.md` §7 documents it as deliberate). CI here is
a day of work, not an afternoon.

Sequence that follows: **`drive.sh` gates first** because it is free and closes "a model
chose not to run the check". **CI later** because only it can gate a merge independently
of the thing doing the merging.

---

## Roles and boundaries

The current design collapses roles. On #18 the orchestrator wrote the contract, edited
the test file twice, produced the browser evidence, ran the gate checks against its own
edits, and would now also merge — four roles in one, and the system's core rule broken
at the top.

Boundaries only hold if they are structural, not requested. `verifier.md` already does
this correctly with `disallowedTools: Write, Edit`. Apply it everywhere.

| Role | Owns | Hard constraint | Exists? |
|---|---|---|---|
| **Non-model gate** | every mechanical check | cannot be skipped or argued with | no |
| **Planner** | owned regions, `verify_via`, `mutation_target` | no journal access; **cannot write criteria** | **no — phantom** |
| **Orchestrator** | dispatch + label transitions | **no `Write`, no `Edit`, at all** | yes, overloaded |
| **Implementer** | code + tests, own worktree | out-of-region edits fail the gate, not a review | yes (`haiku`) |
| **Verifier** | only the judgment the gate cannot do | read-only; must not read `journal.md` | yes (`sonnet`) |

Supporting principles, already in the user's learnings vault:
- *"The orchestrator never does implementation work itself. Keep the coordinating layer
  thin; delegate execution to scoped, disposable workers."*
- *"Isolate parallel work units physically — never share mutable state across concurrent
  agents."*

### The planner is a phantom role

Every manifest declares one — `model_routing: { ..., planner: "opus" }` in all six
`LOOP.md` files including the one written today — and `.claude/agents/` contains only
`implementer.md` and `verifier.md`. The v3 manual assigns it real work ("planner runs a
spec-completeness sweep").

This vacancy is why the orchestrator invented a contract for #32 unasked, and why
#33–#40 are currently unreachable by the loop: **no step in `loop.md` turns an
unlabelled issue into a pickable ticket.** The state machine has no entry point.

`agent:reset` is a second phantom: the v3 manual defines it as the design's only
no-human recovery transition. Zero hits in `loop.md`; the label does not exist in the
repo. Every failure routes to `agent:blocked`, and blocked ends the run.

### Planner scope — translation, never authorship

A contract *defines* acceptance. If a model writes the contract, does the work, and
grades the work, the separation is cosmetic. Direct evidence: #10 lost **two of four
rounds** to errors in the contract itself (named `parse_id_token`, whose real signature
needs `nonce`; omitted the `openid` scope). Retro: *"A hazard list in a manifest is
itself untested code; the implementer followed it faithfully."*

So split the contract by what each half depends on:

| | Depends on | Who writes it | When |
|---|---|---|---|
| **WHAT** — criteria, escalation triggers, budgets | your intent | **human** | all of them, one sitting, up front |
| **WHERE/HOW** — owned regions, `verify_via`, `mutation_target` | current code layout | **planner** | after each merge |

Mechanically enforceable, unlike "write a good contract":
- criterion count in contract **==** criterion count in issue body
- each criterion's text matches the issue **byte-for-byte**
- every criterion has non-empty `verify_via` and `mutation_target`
- every owned-region path exists, or is explicitly marked NEW

**The planner must never read `journal.md`.** `verifier.md` already bans this for itself
("the implementer's account of its own work"). It matters more for a planner: journal
content would propagate into the standard the *next* implementer is graded against.
Legitimate sources are the merged code on master (ground truth), the issue's criteria
(human), and `CONVENTIONS.md`. The journal already has a sanctioned path into future
work — journal → retro → CHECK → `CONVENTIONS.md` — and that path has a verification
step that reading the journal directly skips.

**Rejected: a planner that prompts itself.** A model that writes its own instructions and
then follows them has no fixed point. That is exactly what happened on #32 — the
orchestrator wrote `state: "ready-for-agent"` into the file the next tick reads,
authoring its own authorisation.

---

## Contract format — option A, in the issue body

Decided: **no separate `LOOP.md`.** The issue carries the contract below a `---` marker.
This deletes a file type, the phantom planner's authorship role, and the `state:` field
that has drifted from the GitHub label twice (#18 retro L4; `.loop/32/LOOP.md:61`).

One source of truth each: **the issue is the contract, GitHub labels are the state.**

Worked example: see `.loop/33-contract-draft.md` (written this session, not yet applied
to the issue).

Tail sections: `## Owned regions`, `## Pre-step` (if any), `## Acceptance criteria`
(each with `verify_via` + `mutation_target`), `## Escalation triggers`, `## Budgets`.

---

## Sequencing — contracts are written in batches bounded by structural change

Rule: **a contract can be written as soon as the surface it names is stable. A ticket
that reshapes that surface is a batch boundary.**

The DAG:

```
#33 ──┬──▶ #34 ──┬──▶ #37
      │          ├──▶ #38 ──┐
      │          └──▶ #39   ├──▶ #40
      ├──▶ #35 ─────────────┘
      └──▶ #36
```

#33 creates `app/traces_ui.py` and moves ~330 lines into it, so #34–#40's owned regions
name a module that does not exist yet. Writing them today is guessing — and wrong
owned-region lists are the most expensive failure class in this repo's history.

Therefore: **#33 alone, then #34–#38 in one batch.**

**The move that keeps it at two batches:** #33 must lay down named region markers that
downstream contracts point at — `# region: trace-list-rows (#38)`,
`# region: sidebar-filters (#34)`, `# region: annotation-panel (#39)`,
`# region: detail-body (#35)`, `# region: styles (#40)`. Marker names survive edits;
line numbers do not. Without this, #34's merge invalidates #37's contract and the two
batches become four.

---

## Scope: six tickets, not eight

The review UI is **server-rendered Python f-strings** (`_sidebar_html`,
`_traces_page_html` etc. in `app/main.py`), not a JS app. So:

- **#33–#38** — every criterion is assertable with the existing `TestClient`. No JS
  harness needed. (Earlier "add Playwright" advice was overscoped.)
- **#39** — 4 of 5 criteria are HTTP. The keyboard-binding one needs a browser.
- **#40** — genuinely needs a layout engine ("at 375px … at least 90% of viewport
  width"). No pytest can produce that.

Run #33–#38 through the loop; do #39/#40 by hand. Do **not** let the loop build the
Playwright harness that would then police the loop — `.loop/32/LOOP.md` proposed exactly
that bootstrap.

---

## #33's acceptance is mechanical — the golden snapshot

#33's criterion is "nothing observable changes". That is the worst case if a model reads
tests to decide, and the **best** case if verified by byte diff.

Before the implementer is spawned, capture rendered HTML for the traces routes from the
base branch and commit it. Acceptance = byte-identical after. No model judgment, no
hollow-assertion risk.

**The implementer must not author, regenerate or edit the snapshots.** An implementer
that can rewrite the oracle has no oracle.

Open: who runs that pre-step — the human by hand, or the orchestrator as a first tick.

---

## Merge strategy

Per-ticket PR into an **integration branch** (`traces-ui`), never straight to `master`.
One final integration → master PR that the human merges.

Rejected: accumulating all six tickets into one large PR. Review effectiveness collapses
with diff size, so it yields neither review quality nor autonomy — the human is still the
bottleneck, with a harder job. The per-ticket-into-integration-branch shape is the
stacked-PR pattern, and it matches the #33 → #34 → #38 → #40 chain.

The integration branch is also the cheapest blast-radius control available: every
failure mode is bounded to a branch that can be deleted.

---

## What is built and verified (branch `flp/driver`, commit 44477c9)

Worktree: `../rag-engine-flp-driver`, branched off `origin/master`. **Not merged.**
Created as a worktree because ≥4 other Claude sessions share the main checkout — moving
HEAD there would have yanked the branch out from under them.

| Claim | Status |
|---|---|
| `/loop` resolves headlessly via `claude -p` | verified |
| `drive.sh` stop conditions: drain / blocked / silent / tick cap | verified, 6/6 against stubs |
| `drive.sh` refuses to start when a ticket is `agent:blocked` | verified against the real tracker |
| Real orchestrator emits `TICK-RESULT: NO-OP` | verified, after adding `loop.md` step 9 |
| `acceptEdits` alone is insufficient — Bash stalls silently | verified |
| Full end-to-end drain run | **not proven** — killed at tick 1 |

Committed in 44477c9:
- `.loop/drive.sh` — the tick driver. One `claude -p "/loop"` per tick; a subprocess per
  tick is what makes context genuinely fresh (`CronCreate` and `/goal` both enqueue into
  the already-running session and would not).
- `loop.md` step 1 — reads state by label instead of the hardcoded "#9-#12", which had
  gone stale across the entire #33–#40 backlog.
- `loop.md` step 9 (new) — mandatory machine-readable `TICK-RESULT:` last line.
- `loop.md` `agent:verified` — the seven-condition blast-radius auto-merge policy,
  written out. Previously the file named a policy that did not exist.

Uncommitted in the worktree:
- `.claude/settings.json` — permission allowlist. **Known defective, see below.**
- `.loop/32/` — a 130-line contract the orchestrator wrote unbidden during a test tick.
  Kept as evidence; not authoritative.

---

## Known defects in the work above

1. **`.claude/settings.json` is a deadlock.** `git push` is in `ask`, and `loop.md`'s
   first action on a ready ticket is `git push -u origin impl/<id>-*`. A `claude -p` tick
   cannot answer a prompt, so tick 1 stalls, tick 2 stalls, driver exits 3. It also omits
   `gh issue edit` entirely — the label writes are the whole state machine — and puts
   `gh pr merge` in `ask`, making the auto-merge policy unreachable.
2. **`drive.sh`'s blocked-check is repo-global.** It halts on *any* issue labelled
   `agent:blocked`. #32 is parked there permanently, so it exits 1 before tick 1, every
   run. Scope it to this run's tickets.
3. **No entry point.** Nothing promotes an unlabelled issue to `ready-for-agent`. With 1
   and 2 fixed, the run would produce two `NO-OP` ticks, exit 0, and report "backlog
   drained" having touched nothing.

---

## Decisions taken

- Auto-merge within a blast radius, not a human merge per ticket.
- Attended terminal run first, not detached.
- Protocol changes live on a branch off master, not on `master` directly.
- Contract lives in the issue body (option A), not a separate `LOOP.md`.
- #33 accepted by golden snapshot captured before any work.
- CI + fixes land before the first real run; then #33 alone with `MAX_TICKS=10`.
- Six tickets through the loop; #39/#40 by hand.
- Integration branch, not `master`.
- Build the planner, scoped to translation only.

## Decisions still open

1. **Verifier model.** Currently `sonnet` — changed from `opus` in `bcc0d6b`
   ("verifier + planner default sonnet"), immediately before being handed merge rights.
   Every retro describing a verifier catch was produced by **opus**. With no other gate
   in place, this is the only thing standing between a `haiku` implementer and the branch.
2. **Who runs #33's snapshot pre-step** — human, or orchestrator first tick.
3. **`drive.sh` gate vs CI first** — see the table at the top.
4. Whether to read the six contracts before the run, or rely on the completeness script.

---

## Next steps, in order

1. Fix the three known defects (allowlist, blocked-scope, entry point).
2. Decide the non-model gate: put the mechanical checks in `drive.sh` now, or invest in
   CI + Postgres service container.
3. Write `.claude/agents/planner.md` — translation-only scope, no journal access.
4. Write the completeness script that gates a contract before `ready-for-agent`.
5. Strip `loop.md`: move the mechanical checks out, delete the "Found live on #N"
   narration (~40 lines; it is already in the retros in better detail), drop step 6
   (restates check (b)), step 7 (fires at essentially every ticket given the 74%
   historical rejection rate), and step 8's trigger taxonomy (its only output is which
   kind of stop to report to a human, and there is no human in the target design).
6. Rewrite #33's issue body with the contract tail; capture and commit the snapshots.
7. Run #33 alone, `MAX_TICKS=10`, hands off. Read `.loop/.ticks/`.

Step 7 is the experiment that matters. **0 of 5 past tickets completed without a human
decision** (19 verify rounds, 14 rejections, 74%; first-round PASS: zero). Until one
ticket runs clean end to end, "six tickets overnight" has no evidence behind it.

---

## Session findings worth keeping

- The orchestrator **violated its own gate**: `loop.md` step 4 says STOP if nothing is
  `ready-for-agent`; with #32 briefly unlabelled it instead authored a full contract, and
  wrote `state: "ready-for-agent"` into it. A prose orchestrator will not idle in an
  undefined state — it infers the missing transition and takes it, plausibly enough that
  it is hard to notice.
- A required behaviour **simply was not produced**: before step 9 existed, a headless tick
  reported its outcome only in prose. An outcome a human can read is not an outcome a
  caller can read.
- **`SIGKILL` skips bash `EXIT` traps.** A killed test left a GitHub label wrong. An
  interrupted tick mid-ticket leaves more: a pushed branch, a draft PR, a worktree, and a
  stale label with no journal to be stale against — and the 30-minute staleness rule needs
  a journal to exist.
- **GitHub's `--label` filter lags** sub-second changes. Not a driver bug; relevant to any
  check that reads a label it just wrote.
- The 7-field criterion schema from the v3 manual was used on **exactly one ticket** (#12,
  36 fields) and **zero** on every other, including the contract written today.
  `mutation_target` is the field that tells the verifier which mechanism to delete;
  without it, "run a mutation" means the verifier inventing its own adversary.
- **Shared Postgres.** All eight tickets touch the traces table; `db_scope` is a naming
  convention, not isolation; the table is not in git. `pg_dump` before any run.
