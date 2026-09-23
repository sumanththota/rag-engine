## Outcome: merged · verify rounds: 7 (6 NEEDS_WORK, then PASS at c91b64a) · budget: max_verify_rounds 3 exceeded

Implementer passes: 7 (Session 1–7 in journal; 16 journal entries total once
orchestrator/human entries are counted — max_iterations 8 not exceeded).

Wall-clock: ~41h elapsed on the calendar (first journal entry 2026-09-18T03:30Z,
PASS comment on PR #15 2026-09-19T20:42Z) against a 2h budget. Elapsed, not
active, time — nothing measured it. Tokens: not captured.

Rounds 4–7 ran past `max_verify_rounds: 3`; the journal's orchestrator entries
cite human instruction for each continuation. The escalation trigger "edit
outside owned regions" fired once (`pyproject.toml`, 2026-09-18) and was
resolved by an explicit human approval, not self-approved.

## Rounds

Of the 6 rejections, 4 are posted on PR #15 (rounds 1, 4, 5, 6) and were code
defects. The other 2 were git-infrastructure failures (branch not pushed;
`verify/11-*` not fast-forwarded) with no verdict on the PR and no record in
the journal — which round numbers they were is not recoverable from either.
Round 7 passed.

Defect classes found, and where each surfaced:

1. **Test-order fragility, recurrence** (round 1). Tests 3–6 relied on test 2's
   `finally` cleanup and failed alone or filtered (`AuthError: email already
   registered`).
2. **Store-vs-HTTP test gap** (round 1, again at the orchestrator's pre-gate
   check). All six tests called `ThreadStore` directly; criteria whose
   `verify_via` said HTTP had no HTTP evidence, and doing it over HTTP exposed a
   new thread minted every turn plus missing list/detail/delete routes.
3. **JS/backend split** (orchestrator pre-gate checks, before rounds 3–4).
   Server `thread_id` was never written into client state, `GET /threads`
   was never called on load, and hydration hardcoded `messages: []`. A green
   pytest suite could not see any of it; the browser check that closed it was
   done by the orchestrator, not the implementer.
4. **Cross-user write vulnerability** (round 4). `chat_start` reused any
   `thread_id` it was handed and `write_threads()` had no owner or `deleted_at`
   guard, so user B could write into user A's Thread. Reads were filtered;
   writes were not. Round 4 also caught two more conditional-guarded
   assertions and a stale `uv.lock`.
5. **Incomplete bounds-check rollout** (rounds 5 and 6). An out-of-bigint
   `thread_id` returned 500. Round 5 found it on the two chat routes, the fix
   caught `ThreadsError` at those two sites, and round 6 found the same 500 on
   `GET`/`DELETE /threads/{id}`. Fixed structurally in a third pass with one
   `_is_valid_thread_id()` called from all four routes.

Round 5 also found that the criterion-6 test was unfalsifiable (see learning 1).

## Learnings -> promote

- [ ] -> CONVENTIONS.md §7: a negative-result test ("X was not written / not
      returned / not changed") must target a resource that could plausibly be
      affected, not one guaranteed absent by construction. The criterion-6 test
      spoofed `thread_id=999999999`, which can never exist, so an anonymous
      write into it fails on the foreign key whether or not the code is right —
      the correct and the broken implementation both pass.
      CHECK: for each test asserting non-occurrence, confirm the resource it
      points at is seeded and real before the action and unchanged after, and
      break the code under test (mutant that performs X) to confirm the test
      goes red.
- [ ] -> CONVENTIONS.md §7: a validity or bounds check on a client-supplied
      value lives in one function that every route calls, not reimplemented or
      re-caught per route. The thread_id bounds check took three passes — two
      route pairs, then the shared function — and a sibling route was missed
      each time until it was made structural.
      CHECK: `grep -n "<param_name>" app/main.py` for every route taking the
      param, and confirm each one calls the shared validator; add a boundary
      test (max+1) per route, not per shared function.
- [ ] -> .claude/commands/loop.md (already landed as step 6, c672af0; recorded
      here so the promotion is not lost): after any agent claims "pushed",
      verify against origin directly, never on the claim.
      CHECK: `git ls-remote origin <branch> | cut -f1` equals `git rev-parse
      <branch>`, and `git diff verify/<id>-* impl/<id>-*` is empty before gating.
- [ ] -> .claude/commands/loop.md ticket-writing step: a ticket whose acceptance
      list mixes pytest-reachable server logic with browser-only frontend
      behavior is split at write-time, not discovered mid-loop. (Gate-pending
      check (d), b949984, handles the same gap after the fact; this moves it
      before the first implementer pass.)
      CHECK: at ticket write time, tag each criterion's `verify_via` as
      pytest or browser; if both appear, split into two tickets with a
      `depends_on` edge, and note the repo still has no JS harness.

None of these are checked off — promotion has not happened, this records the
candidates and their CHECK lines.

Already promoted during this ticket, not repeated above: CONVENTIONS.md §7
test-isolation CHECK (`run the file alone and with -k`), the conditional-guarded
assertion rule, the write-side ownership rule, and the `[build-system]` note.

## Follow-up tickets (not learnings; not yet filed)

Non-blocking items from the round-7 PASS:

- Criterion-3 test (`test_delete_thread_soft_deletes_and_removes_from_list`)
  never reads `deleted_at`. A hard delete passes it; only
  `test_cannot_write_into_soft_deleted_thread` catches one, indirectly. Add a
  direct `deleted_at` assertion.
- `write_message`'s SQL-level `AND deleted_at IS NULL` guard is untested —
  removing it passes all 9 tests, since the route-level `get_thread` already
  blocks.
- `chat_stream`'s new early `404` return sits after `trace_id_var.set(...)` and
  before the `try/finally` that resets it, so the trace token is not reset on
  that path.

## Superseded

None. Does not edit `.loop/9/retro.md`.
