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
   RETRO GATE: before picking a NEW ticket (not a fix-round on one already open),
   confirm the immediately-prior merged ticket's .loop/<id>/retro.md exists and is
   written. If it doesn't, write it now, from the journal — do not let a second
   ticket's activity push a first ticket's retro further out. Found live: #11's
   retro sat unwritten through the entirety of #10 and #12 running, despite the
   whole point of "capture immediately" being to prevent exactly this.
5. ADVANCE ONE STEP based on state:
   - ready-for-agent  -> create branch impl/<id>-* yourself (subagent worktrees get
     temporary worktree-* branches and will not land on impl/ on their own); push it with
     `git push -u origin impl/<id>-*` (the branch must exist on the remote before a PR can
     be opened against it); open draft PR early (the verifier posts its verdict there;
     with no PR the verdict has to travel through a human clipboard and gets paraphrased);
     spawn IMPLEMENTER (cheap model, isolation: worktree) and CONFIRM `git worktree list`
     gained an entry — isolation only fires for a dispatched subagent, so a directly-run
     session silently shares your working directory with every other agent. Instruct:
     touch only owned regions; use test-<id>-* fixtures; do NOT boot the shared dev
     server on :8080 — verify via pytest, spinning up a test instance on an ephemeral
     port where a criterion needs one; stamp a start time in the first journal entry and
     append one entry per iteration; set label agent:gate-pending ONLY with every
     self-test green.
   - agent:gate-pending -> FOUR CHECKS FIRST, EVERY TIME — not just the first time
     this ticket reaches this state; re-run all four on every round, since a branch
     that was level two rounds ago is not evidence it's level now. Fail any and label
     agent:blocked:
     (a) `git diff --name-only master...impl/<id>-*` — every path must fall inside the
         owned regions named in LOOP.md. An out-of-region edit is an escalation trigger,
         not something the implementer may justify in the journal and carry on from.
         If you approve one, record the approval in the journal — an approved escalation
         and a skipped one look identical afterwards.
     (b) `git fetch origin`, then compare `origin/impl/<id>-*`, local `impl/<id>-*`,
         and `verify/<id>-*` — all three must be the SAME sha before you spawn anything.
         Do not trust a local ref alone and do not trust an agent's claim of "pushed."
         Fast-forward verify to match, then push it:
         `git push origin verify/<id>-* || git push origin verify/<id>-* --force`
         (plain push first; force only if that is rejected as non-fast-forward).
         This recurred 3 distinct ways on one ticket (#11) even after being
         fixed once — wrong branch entirely, a branch committed but never pushed, and
         a verify branch fast-forwarded locally but never pushed to origin, so the next
         session's verifier saw the old one again. Treat "in sync" as unverified until
         checked against origin, every round, permanently.
     (c) for every criterion in LOOP.md whose verify_via names HTTP or a UI element,
         grep the new/changed test files for a test client, a route call, or that
         element. Zero matches on any such criterion is a block, named specifically —
         "criterion 3 (verify_via: HTTP) has no route-level test in the diff." This is
         a mechanical check on imports and calls, not a read of test quality. Found live
         on #11: six green tests, all calling ThreadStore directly, zero touching a
         route — four of six criteria were untested at the level they were written.
     (d) for any criterion whose verify_via names a UI behavior with no JS test harness
         available in this repo (check: no playwright/jest/selenium config present), a
         pytest cannot be the acceptance evidence. Require instead, in this order:
           1. A static grep confirming the relevant JS function calls the required route.
              Absence is a definite fail. Presence is necessary but not sufficient.
           2. A pytest asserting the route's response actually contains the data the JS
              needs to read — proves the API contract, not the UI behavior itself.
           3. A manual check with pasted actual output (console log, screenshot, or
              response body) in the journal — not "confirmed working." Self-report of
              a browser check is not evidence; the artifact of the check is. The
              ORCHESTRATOR runs this one (the implementer has no browser): start
              `preview_start` config `flp-test-instance` (port 8099, never :8080),
              drive it with the built-in browser, and post the raw output BOTH in the
              journal and as a PR comment headed "orchestrator browser evidence (check
              (d) level 3)" — the verifier cannot read journal.md but can read the PR.
              Stop the instance afterwards.
         All three, not the first one alone. This repo has no JS test harness at all —
         that gap itself is a standing item, not something to route around silently.
     Then spawn VERIFIER (strong model, on verify/<id>-*, Write and Edit disallowed). Run verifier_command. The VERIFIER posts its own raw
     verdict to the PR with gh — you must not relay it, since you only receive its summary
     and relaying would paraphrase the thing that gates the merge.
     PASS -> label agent:verified, mark PR ready. NEEDS_WORK -> label agent:in-progress.
     A criterion marked BLOCKED-ON-<id> in LOOP.md is excluded from the gate-pending and
     PASS tests only under that LOOP.md's carve-out conditions; the PR must not say
     `Closes #N` while one is open. Reaching agent:verified with one open is still a
     stop-and-report for the human, never a merge.
   - agent:verified -> before merging, confirm verify/<id>-* has an EMPTY diff against
     impl/<id>-*; a verifier that edited anything has invalidated its own verdict.
     Then (human, or auto if within blast-radius policy) merge; on merge, re-evaluate DAG:
     any issue whose depends_on are all merged -> label ready-for-agent.
     Write retro.md; update INDEX.md; promote checked learnings into CONVENTIONS.md.
6. VERIFY EVERY PUSH (standing, permanent — applies after ANY agent, implementer or
   verifier, claims work is committed/pushed): never trust the claim. Check origin
   directly:
     git ls-remote origin <branch> | cut -f1
     git rev-parse <branch>
   These must match. If they don't, push it yourself before proceeding. This has now
   failed 3 distinct ways on #11 alone — wrong branch entirely, right branch never
   pushed, verify branch not fast-forwarded — each one invisible from the agent's own
   report. Treat "pushed" as unverified until confirmed against origin, every single
   time, for every agent.
7. MODEL-TIER OVERRIDE: if a ticket's verify-round rejection rate exceeds ~40%
   (rejections / total verify rounds), the NEXT fix-round implementer spawns one
   tier up from whatever this ticket's model_routing says — for THIS ticket only.
   Record the override in this ticket's own LOOP.md model_routing field. Do not
   change implementer.md's frontmatter default; that stays whatever it was, for
   every other ticket. This is the first real job model_routing has had all
   night — everywhere else it's inert documentation; a per-ticket override
   recorded here is the one place it's actually read and actually meant.
8. STOP CONDITIONS (hard): count this feature's journal entries as iterations; if
   max_iterations is exceeded, or any escalation_trigger fired, label agent:blocked, write
   the reason to journal.md, and STOP for a human. Nothing measures max_tokens or
   wall_clock — read them off the journal yourself at each tick.
   ESCALATION TRIGGER 2 NUANCE: "the same criterion fails in 3 rounds" was written
   to catch a STUCK implementer hitting the same wall — evidence the spec itself is
   wrong. It can also fire on 3 rounds that each found a genuinely DIFFERENT defect
   which all happen to map to the same criterion's sub-clauses (found live on #10:
   a missing OAuth scope, an unguarded parse call, then a cookie-attribute gap —
   three distinct bugs, one criterion). Both are real trigger conditions and both
   still STOP for a human — but say which kind it was in your report. A stuck
   implementer needs a spec rewrite; three distinct defects under mutation testing
   with the underlying route confirmed correct is a case for a human to approve one
   bounded extra round, not to redesign the ticket.

Never advance more than one step. Never let the implementer's "tests pass" be the accept signal.
