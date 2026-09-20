# Retro — #12: Migrate pre-login local Threads into the server on first login

**Outcome:** merged (PR #26, `impl/12-thread-sync` → `master`, merge commit `5860f7c`).
**Resolution:** `bounded_round` — see below.
**Iterations:** 4 implementer rounds (round 1 initial build, round 2 mutation-hardening,
round 3 re-verification, round 4 a human-approved bounded extra round) plus one full
scaffold-restart partway through the ticket's life (see "Restart" below). ~30+ journal
entries across the full history.
**Verifier rounds:** 3 of the `max_verify_rounds: 3` budget — round 1 NEEDS_WORK, round 2
NEEDS_WORK/STOP (same failure class recurring), round 3 PASS.
**Tokens:** not captured at the per-round granularity; harness-reported subagent tokens for
the round-3 verifier alone were ~128k.

## What happened

This ticket ran the full length of FLP v3's escalation ladder in one run:

1. **Rounds 1–2**: implementer built `/threads/sync`, `ThreadStore.sync_threads`, the
   `client_thread_id` upsert, and `window.afterLogin()`. Orchestrator's own mutation
   pre-check (not the verifier) found criteria 3 and 4's tests were **hollow** — criterion
   3's ordering check did a raw substring search over the region's source text, which the
   function's own leading *comment* satisfied regardless of the real call order below it;
   criterion 4's tiebreaker check similarly could be satisfied by text sitting in a
   docstring `inspect.getsource()` returns but that Python never executes. Two independent
   verifier rounds each found a NEW way to satisfy the same hollow text-match (comment
   syntax, then docstring syntax) after the previous round patched the prior escape —
   **the same failure class recurring 3–4 times**, which FLP v3's STOP table treats as a
   respec-or-bounded-round trigger, not a 5th patch-and-hope round.
2. **STOP → human decision**: escalated per loop.md step 8's nuance — this was judged
   closer to "spec/test-strategy is wrong" than "implementer is stuck," because two
   independent verifiers and one live orchestrator browser run had already confirmed the
   *production code* was correct throughout; only the *automated proof* kept being gameable.
   Human chose **investment over respec/accept**: replace the text-match proxies with real
   execution — a `node -e` run of the actual `afterLogin()` region (comments don't survive
   to execution, so there's no text position left to game) and a real Postgres MVCC-forced
   tie for the ordering tiebreaker (a genuine behavioral divergence, not a source-text scan).
3. **Round 4 (the approved bounded round)**: executed exactly that plan. Both replacement
   tests mutation-tested clean by both the implementer and, independently, two rounds of
   orchestrator re-verification.
4. **A ticket-level restart happened separately, before this arc**: `.loop/12` was fully
   reset from scratch under FLP v3 partway through this ticket's life (see the `docs/12-scaffold`
   PR #25 / commit `9e85eab`) — the retro above covers the FLP-v3-era run only; an earlier,
   now-discarded pre-v3 attempt is not reflected here.
5. **This session (2026-09-20, continuing an existing run)**: picked up at `agent:gate-pending`
   with round 4 already green in the implementer's own report. Re-ran all four gate-pending
   checks fresh (not trusting a pass from an earlier round) and found **check (b) failing
   again** — `verify/12-thread-sync` was still pinned at the round-3 checkpoint, 3 commits
   behind `impl/12-thread-sync` after round 4 + a journal repair. Fast-forwarded and pushed
   it, then dispatched verifier round 3, which PASSED cleanly. Criterion 3 correctly reports
   as `BLOCKED-ON-18` (unmet) per its carve-out — that half is #18's to close.
6. **Merge gated on a human**, per loop.md's explicit rule that reaching `agent:verified`
   with a `BLOCKED-ON-<id>` criterion still open is a stop-and-report, never an automatic
   merge — asked, got "merge now," merged.

## Key learnings

1. **A hollow assertion doesn't announce itself as one green run — it takes an adversarial
   mutation to expose it, and even then, a hastily-patched hollow check can just move to a
   different comment/doc syntax rather than becoming a real check.** The fix that actually
   held was switching evidentiary *method* (real execution / real MVCC divergence), not
   iterating on the same text-match method. When a `mutation_target` keeps surviving under a
   *different* disguise each round against the *same* criterion, that's the signal to change
   how the property is checked, not to patch the check again.
2. **Verify-branch staleness recurred a fifth time across #11 and #12 combined** — every
   single gate-pending tick in this ticket's history required re-checking and often
   re-fixing check (b), even mid-ticket, even after it had passed earlier in the same
   ticket's life. Treating "checked once" as durable is the actual bug; loop.md's "every
   round, unconditionally" framing for check (b) is correct and should not be loosened.
3. **`.worktreeinclude`'s `.env` copy is not automatic for an orchestrator-created worktree**
   (plain `git worktree add`, as opposed to a subagent's isolated spawn) — discovered live
   this session, cost real time, now captured as a CHECK in `CONVENTIONS.md` §7.
4. **An implementer does not reliably treat an owned, append-only file as append-only, even
   under an explicit literal instruction not to rewrite it.** Round 4's implementer replaced
   647 of 662 prior `journal.md` lines with its own new section; caught only by the
   orchestrator diffing commits directly, not by trusting the implementer's "documented
   round 4 results" self-report. `journal.md` was inside the ticket's owned regions, so this
   wasn't an out-of-region drift trigger — it's a distinct failure mode (an in-region
   convention violation) that loop.md's check (a) doesn't currently name. Worth a future
   mechanical check (line-count-never-decreases, or a diff-against-prior-tail check) if this
   recurs on another ticket; logged here rather than promoted yet, per L5's own restraint
   rule (one incident isn't a pattern).
5. **A narrow `verifier_command` (`-k sync`) silently excluded the very tests written for
   criteria 3 and 4** — both matched the file but not the keyword filter, so an early round's
   verifier scoring PASS on the filtered command would have been reading zero evidence for
   two of six criteria. Caught and fixed (command widened to the full file, no filter) before
   it caused a false PASS. General lesson already generalized in this ticket's own
   `acceptance_criteria` #6 ("run the FULL suite, not a scoped command") — the same caution
   applies one level down, to `verifier_command` itself: prefer no `-k` filter unless every
   criterion's test names are confirmed to match it.

## CHECK

CHECK: promoted to `CONVENTIONS.md` §7 — orchestrator-created worktrees (`git worktree add`
directly, not a subagent's isolated spawn) do not get `.worktreeinclude`'s automatic `.env`
copy; copy it and rebuild the venv by hand every time, verified before handing the worktree
to an agent or running tests in it.
