## Outcome: merged 2026-09-19T23:59Z (PR #17, via verify/10 as PR #22) · verify rounds: 4 (3 NEEDS_WORK, then PASS at 81376af) · budget: max_verify_rounds 3 exceeded by one human-approved bounded round

Implementer passes: 4 (original; fix pass after round 1; fix pass after round 2, sonnet;
bounded round 4, sonnet). 10 journal entries on master, counting orchestrator and human
entries — max_iterations 8 not exceeded.

Wall-clock: ~2h55m elapsed (scaffold commit 2026-09-19T21:03Z, merge 23:59Z) against a 2h
budget. Elapsed, not active, time — nothing measured it. Tokens: the journal did not
capture them; the harness reported subagent totals at each completion — implementers
291,784 (haiku 150,998, sonnet 140,786), verifiers (opus) 388,193, ~680k in all against the
400k budget, orchestrator's own usage not included. `max_tokens` is a prompt to check, not a
tripwire, and it was exceeded without anything noticing.

Escalation triggers: "same criterion fails in 3 rounds" and `max_verify_rounds` both fired at
round 3 (2026-09-19T23:00Z). Per the loop.md TRIGGER 2 NUANCE this was NOT a stuck
implementer: three different defects, all under criterion 2, each found by mutation testing,
with the route confirmed byte-identical to POST /login on unmodified HEAD. The human approved
one bounded extra round (test-only, one assertion, "no round 5"), which passed. No out-of-region
edit occurred; the `config.py` / `pyproject.toml` / `.env.example` edits were pre-authorized in
LOOP.md and used as authorized.

## Rounds

- **Round 1 (1961990) NEEDS_WORK.** Criteria 2, 3, 5 failed, 1 weak; 4 of 5 mutations left all
  10 Google tests green (bare `pass` bodies, an `!= 404` assertion, criterion 3 via a direct
  store call). Also a real defect: the route called `parse_id_token(token)` without the
  required `nonce`, invisible behind an `AsyncMock` and a bare `except Exception`.
- **Round 2 (d012739) NEEDS_WORK.** State validation removed entirely stayed green — the
  rejection tests never patched the token exchange, so the bypass hit Google's live token
  endpoint, got `invalid_client` (also a 4xx) and passed. No test guarded a direct
  `parse_id_token` call. The client was registered without the `openid` scope, so against real
  Google no nonce, id_token or userinfo would exist and every sign-in would 400; the mocks
  handed authlib a pre-made `userinfo`, so the `parse_id_token` patch was dead code.
- **Round 3 (d59e873) NEEDS_WORK.** 11 of 12 mutations killed. The survivor dropped `HttpOnly`
  and changed `SameSite`/`Max-Age` on the callback cookie; the "same attrs as POST /login"
  clause of criterion 2 was asserted nowhere. Route correct, test-only gap.
- **Round 4 (81376af) PASS.** One assertion added; 17/17 mutations killed on an isolated copy,
  provenance printed, socket-blocked run green.

## What made this hard

- **Two of the first three rejections trace to orchestrator guidance errors, not implementer
  error.** LOOP.md told the implementer to patch `parse_id_token` (my prototype patched it too,
  which hid that its real signature needs `nonce`), and named `code_challenge_method` but never
  the `openid` scope. A hazard list in a manifest is itself untested code; the implementer
  followed it faithfully.
- **Every mock hid a different real defect.** Patching the validating step skipped state
  validation; a pre-made `userinfo` skipped the id_token path; unpatched rejection tests reached
  the network. Green tests stayed green through each.
- **A criterion written as a clause list** (state validated, email verified, same cookie, same
  attrs) was found one clause at a time across rounds instead of all at once.
- **The implementer gated with proof unrun.** After round 1's fix pass it reported 3 of the 5
  required mutations and still set `agent:gate-pending`.
- **Round 1's first mutation run silently imported the real, unmutated tree** — the verifier
  caught it only because it printed provenance.

## Learnings -> promote

- [ ] -> CONVENTIONS.md §7: mock a third-party call at the NETWORK layer, never at the step
      that validates; tests for the rejection path also assert the network mock was NOT
      called. Patching the validating function skips the check the criterion is about, and an
      unpatched rejection test can reach the real service and still see a 4xx.
      CHECK: `grep -n "authorize_access_token\|<validating fn>" tests/` shows it is not
      patched; every rejection test contains `assert_not_called()`; the verifier runs a
      "validation removed" mutant and it goes red.
- [ ] -> manifest template (LOOP.md): any hazard or mock guidance that names a library function
      cites its real signature or source (file:line or `inspect.signature` output), not a
      prototype's behavior. A prototype that mocks the same seam the implementer will mock proves
      nothing about that seam. Cost this ticket two rounds.
      CHECK: every HAZARD line naming a library call carries a `source:` or `signature:` reference.
- [ ] -> CONVENTIONS.md §7: an auth cookie and any middleware that sets cookies must not share a
      name. Starlette's `SessionMiddleware` defaults to `session`, the same name as the #9 auth
      cookie, and the real callback emits `session=null` after it.
      CHECK: `grep -n "SessionMiddleware(" app/` shows a `session_cookie=` different from
      `SESSION_COOKIE_NAME`, and a test asserts EXACTLY ONE `session` Set-Cookie on auth responses.
- [ ] -> manifest template: a criterion phrased as a clause list gets its mutation kill-list
      written into the manifest in round 1 — one mutation per clause — so the implementer sees
      the whole bar at once instead of one clause per round.
      CHECK: the LOOP.md `verify_via` lists a mutation per clause; the implementer's journal pastes
      one RED line per mutation; fewer than listed is an automatic block.
- [ ] -> CONVENTIONS.md (repo-level): no `close/fix/resolve #N` in scaffold or docs commits or
      non-ticket PR bodies. A scaffold commit saying "resolve #10/#12" closed #10 on merge before
      any code existed.
      CHECK: `git log origin/master..HEAD --format=%B | grep -iE '(close[sd]?|fix(e[sd])?|resolve[sd]?) +#[0-9]+'`
      prints nothing on any non-ticket branch.
- [ ] -> .claude/commands/loop.md: append the subagent token totals from each completion to the
      journal at every round. The `tokens` column has read "not captured" for three tickets; the
      numbers were available all along in the harness's completion notices.
      CHECK: every orchestrator round entry in journal.md has a `tokens:` line.

Already promoted during this ticket, not repeated above: verifier steps for mutant-import
provenance, socket-blocked runs for third-party calls, byte-for-byte comparison of "matches an
existing correct path" claims, and the relative-assertion caveat (a scoped run of a relative
assertion cannot see a shared helper regress — only the full suite did); the loop.md TRIGGER 2
NUANCE and "fewer mutations than required is an automatic block"; the MODEL-TIER OVERRIDE.

Model-tier data point, not a learning: the rate reached 3/3 after round 2 and rounds 3 and 4 ran
on sonnet. Defect scope narrowed each round (a full clause, then one attribute), but the manifest
guidance was corrected at the same time, so this does not isolate the tier's effect (n=1).

None of these are checked off — promotion has not happened, this records the candidates and
their CHECK lines.

## Follow-up tickets (not learnings; not yet filed)

- **Google itself was never exercised.** Every test mocks the token exchange and id_token parse;
  `GOOGLE_CLIENT_ID`/`SECRET`/`REDIRECT_URI` are empty and no live consent flow was run. A manual
  smoke test with real credentials is needed before this is trusted in production.
- A non-`OAuthError` failure inside `authorize_access_token` (a network error to Google) now
  surfaces as a 500, as HAZARD 4 intends. Decide whether a friendlier response is wanted.
- The scoped `pytest -q tests/test_auth.py -k google` pins cookie-attribute EQUALITY with
  POST /login, not absolute values; the absolute-value test is
  `test_set_session_cookie_attributes_match_spec` (tests/test_auth.py:167) and only the full suite
  runs it.

## Superseded

None. Does not edit `.loop/9/retro.md` or `.loop/11/retro.md`.
