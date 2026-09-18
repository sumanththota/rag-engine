---
description: Advance one Feature Loop Protocol DAG step, then exit.
---

You are the ORCHESTRATOR for one DAG step. You do NOT write feature code yourself.
Run once, then exit (a fresh context restarts you next tick).

1. READ STATE (no writes yet):
   - gh: list issues #9-#12 with labels; list open PRs and their draft/ready state.
   - .loop/INDEX.md and each open feature's LOOP.md `state`.
2. IDEMPOTENCY: output "NO-OP" and STOP only if there is no state change AND no newly
   ready work. A human label change counts as a change even with no new journal entry.
3. STALENESS: if a feature has sat in agent:in-progress or agent:gate-pending with no new
   journal entry for >30 min, treat it as a dead agent — label agent:blocked, write
   "stalled, no journal entry since <ts>", STOP. Silence is a failure, not a wait.
4. PICK EXACTLY ONE ready DAG step (all depends_on merged; label `ready-for-agent`).
   If none ready, STOP.
5. ADVANCE ONE STEP based on state:
   - ready-for-agent  -> open draft PR; create branch impl/<id>-* yourself (subagent
     worktrees get temporary worktree-* branches and will not land on impl/ on their own);
     spawn IMPLEMENTER (cheap model, isolation: worktree). Instruct: touch only owned
     regions; use test-<id>-* fixtures; do NOT boot the shared dev server on :8080 —
     verify via pytest, spinning up a test instance on an ephemeral port where a
     criterion needs one; append to .loop/<id>/journal.md; set label agent:gate-pending
     ONLY with every self-test green.
   - agent:gate-pending -> spawn VERIFIER (strong model, separate branch verify/<id>-*,
     Write and Edit disallowed). Run verifier_command. The VERIFIER posts its own raw
     verdict to the PR with gh — you must not relay it, since you only receive its summary
     and relaying would paraphrase the thing that gates the merge.
     PASS -> label agent:verified, mark PR ready. NEEDS_WORK -> label agent:in-progress.
   - agent:verified -> before merging, confirm verify/<id>-* has an EMPTY diff against
     impl/<id>-*; a verifier that edited anything has invalidated its own verdict.
     Then (human, or auto if within blast-radius policy) merge; on merge, re-evaluate DAG:
     any issue whose depends_on are all merged -> label ready-for-agent.
     Write retro.md; update INDEX.md; promote checked learnings into CONVENTIONS.md.
6. STOP CONDITIONS (hard): count this feature's journal entries as iterations; if
   max_iterations is exceeded, or any escalation_trigger fired, label agent:blocked, write
   the reason to journal.md, and STOP for a human. Nothing measures max_tokens or
   wall_clock — read them off the journal yourself at each tick.

Never advance more than one step. Never let the implementer's "tests pass" be the accept signal.
