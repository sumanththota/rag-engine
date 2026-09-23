# Retro — #18: Login UI: form + submit calling afterLogin()

**Outcome:** merged (PR #29, `impl/18-login-ui` → `master`, merge commit `ed9f6cd`; a
second PR #30 for `verify/18-login-ui` was also opened and merged — redundant, harmless,
see "Key learnings" below).
**Resolution:** n/a — never reached `agent:blocked`. Clean two-round run.
**Iterations:** 1 implementer round (full feature build) + 2 orchestrator-direct fix rounds
(one region-boundary relocation, one test-assertion round dispatched to a fresh implementer,
one further orchestrator-direct tightening of that same fix) — no human decision needed at
any point.
**Verifier rounds:** 2 of the `max_verify_rounds: 3` budget — round 1 NEEDS_WORK, round 2 PASS.

## What happened

1. **Implementer round 1** built the login form, submit handler, and show/hide toggle inside
   `app/templates/index.html`'s `// region: login-ui (#18)` markers, plus
   `tests/test_login_ui.py` (4 HTTP contract tests + 6 static/grep tests). Self-reported no
   out-of-region edits and all criteria met.
2. **Orchestrator gate-pending check (a) caught a real out-of-region edit** the implementer's
   own report didn't disclose: a 2-line `checkLoginStatus()` page-load invocation had landed
   after the `// endregion: login-ui (#18)` marker. Fixed directly (a boundary relocation, not
   a design decision) rather than spending a verify round or a human escalation on it.
3. **Orchestrator produced check (d) level-3 evidence** (no JS harness in this repo) via a
   real `flp-test-instance` browser run, covering all four UI criteria with actual network
   traces, DOM screenshots, and a wrapped `afterLogin()` call counter — posted to PR #29.
4. **Verifier round 1: NEEDS_WORK.** `test_18_index_html_failure_branch_no_afterlogin` had
   every assertion gated behind `if if_resp_ok_match:` with no `else: fail` — a silent-skip
   hollow guard. The verifier proved it by mutating the code so the extraction regex no
   longer matched, and the test still passed with zero assertions run. This is the *exact*
   anti-pattern already documented in this repo's own `CONVENTIONS.md` §7 before this ticket
   ever started — the implementer wrote code violating a standing, pre-existing convention.
5. **Fix round dispatched, scoped to the test file only** (production code was never in
   question). The fix-round implementer made the extraction failure a hard `assert` and added
   a new assertion that the failure branch calls `showLoginError(...)`.
6. **Orchestrator found a second-layer hollow-check bug in the fix, before spending a verify
   round on it**: the new `showLoginError` assertion used an open-ended `.*?showLoginError\('
   scan that could also match an *unrelated* `showLoginError(...)` call later in the same
   function (the network-error `catch` block's fallback message) instead of the real
   401-branch call. Tightened directly to anchor on the `errMsg` variable name, which is
   unique to the response-derived 401 message (the two other call sites use hardcoded string
   literals). Verified with fresh, self-run mutations before re-dispatching the verifier.
7. **Verifier round 2: PASS.** Built its own independent mutants (not trusting either the
   implementer's or the orchestrator's description of the fix) and confirmed all three attack
   vectors are genuinely killed. Noted three additional non-blocking mutants that survive the
   static/grep layer but are covered by the level-3 browser evidence layer — logged as a
   standing gap (no JS harness), not a defect in this round.
8. **Merge**: no `BLOCKED-ON-<id>` criteria on this ticket, so no stop-and-report gate;
   labeled `agent:verified`, PR marked ready, merged by the human.

## Key learnings

1. **A hollow-assertion bug can recur one layer deeper inside its own fix.** Round 1 found a
   *structural* hollow guard (assertions gated behind an unchecked `if`); the immediate fix
   for it introduced a *content* hollow guard (an assertion satisfiable by the wrong call
   site). Fixing a mutation-testing finding is not itself immune to needing mutation-testing —
   the orchestrator caught this one only by re-running the same adversarial method against
   its own fix before trusting it, not by inspection.
2. **This repo's own documented convention (`CONVENTIONS.md` §7, written after an earlier
   ticket) was violated by name in this ticket** — "no test's only meaningful assertion may
   sit behind a runtime conditional" is not a hypothetical risk here, it's a rule this exact
   ticket broke on its first pass. A future improvement worth considering (not implemented
   here, per L5's restraint): a mechanical grep for `if \w+_match:\s*\n\s+assert` patterns in
   new test files, run as part of gate-pending check (a), rather than relying on a verifier to
   catch it via mutation each time.
3. **A second PR (#30, `verify/18-login-ui`) got opened and merged alongside the intended one
   (#29, `impl/18-login-ui`).** Harmless here since `verify/18-login-ui` was an exact ref
   match of `impl/18-login-ui` at merge time (empty diff, per the standing pre-merge check),
   so merging it was a no-op on top of #29. But it's worth noting for the ledger: nothing in
   this protocol run opened PR #30 deliberately — worth a human glance on why GitHub or a
   prior action created it, so it doesn't happen silently on a ticket where the two branches
   *aren't* identical at merge time.
4. **The orchestrator's own GitHub-label bookkeeping drifted from the LOOP.md file state at
   least once this ticket** — a `agent:gate-pending` state written into `.loop/18/LOOP.md`'s
   comment field was not mirrored to the actual GitHub issue label, leaving `agent:in-progress`
   stuck alongside a later `agent:verified` label until caught on a subsequent state re-read.
   Same category of lesson as #10/#11/#12's repeated branch-sync findings: a file-level
   comment describing intended state is not the same as confirming the live external
   resource (GitHub label, remote branch sha) actually reflects it — re-check both, every
   transition, not just once.

## CHECK

CHECK: no new standing rule promoted from this ticket — the `CONVENTIONS.md` §7 rule this
ticket violated already existed; the lesson here is about *catching a second-order hollow
check inside a fix for a first-order one*, which is closer to "the orchestrator's own
diligence held" than "a new mechanical guard is needed." Logged as retro content per L5's
restraint, not promoted.
