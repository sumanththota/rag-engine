---
id: "10"
title: "Google OAuth login, merged into the same account model"
goal: "Sign in with Google issues the same session cookie as password login (#9), auto-merging into an existing account by verified email."
spec_link: "https://github.com/sumanththota/rag-engine/issues/10"   # immutable audit anchor
dag:
  depends_on: ["9"]   # needs users/auth_identities schema, cookie issuance, find_or_create_user
  blocks: []
readiness:   # orchestrator-scored 2026-09-19 against the FLP rubric, AFTER the verify_via/criteria fixes below
  acceptance_criteria: 4   # 1-4 verbatim from issue; 5-6 orchestrator-added (see markers). Google is mocked, so 1-2 prove our wiring, not Google.
  context: 4               # config gap + mock-only constraint documented; new-dependency/config rulings still pending a human
acceptance_criteria:   # 1-4 copied verbatim from the issue body; 5-6 ADDED by the orchestrator 2026-09-19 (not in the issue) so the verifier, which reads only this list, can see them. Each must be able to FAIL.
                       # Google is MOCKED everywhere (no real credentials): patch authlib's token exchange / id_token parse. A pass proves OUR handling of a Google response, never Google itself.
  - text: "GET /auth/google/login redirects to Google's consent screen with state set"
    verify_via: "HTTP — test client GET /auth/google/login with follow_redirects=False; assert a 3xx, Location host is accounts.google.com, and the query string carries a non-empty `state`"
  - text: "GET /auth/google/callback validates state, verifies email_verified, and sets the same session cookie #9 issues for password login"
    verify_via: "HTTP — test client GET /auth/google/callback with mocked token/userinfo. Must cover: (a) valid state + email_verified:true -> cookie set, same cookie name/attrs as POST /login, and GET /me with it returns that user; (b) mismatched/missing state -> rejected, no cookie; (c) email_verified:false -> rejected, no cookie, no user row created"
  - text: "A Google login with an email matching an existing password-signup user adds a new auth_identities row to that same users.id (no duplicate user created)"
    verify_via: "HTTP — POST /signup (test-10-* email), then a mocked-Google GET /auth/google/callback with that email; assert users count for that email is still 1 and auth_identities has a google row on that same users.id. A find_or_create_user-direct call cannot satisfy this"
  - text: "A password-signup attempt against a Google-only email is rejected with a clear message, not silently overwritten"
    verify_via: "HTTP — Google-only user created via mocked callback, then POST /signup with that email; assert 4xx, body message names Google sign-in (not the generic 'email already registered'), and password_hash is still NULL afterward"
  - text: "ORCHESTRATOR-ADDED: a successful /auth/google/callback responds with a redirect to /?login=google (the documented server half of the #12 client-parity contract), cookie set on that same response"
    verify_via: "HTTP — callback with follow_redirects=False; assert 302 and Location == '/?login=google' AND Set-Cookie present on that response"
  - text: "ORCHESTRATOR-ADDED (standing, every feature): the FULL suite is green"
    verify_via: "pytest -q, no -k filter — verifier_command below is narrower than the blast radius of a main.py/auth.py change (found on #9: full suite caught a regression the scoped run missed)"
verifier_command: "pytest -q tests/test_auth.py -k google"
escalation_triggers:
  - "schema/migration touches an existing table"
  - "the SAME criterion fails in 3 separate verify rounds"
  - "any edit outside the owned regions listed under Context (app/auth.py, tests/test_auth.py, main.py `# region: google-oauth`, the signup handler's AuthError mapping)"
  - "a criterion names verify_via: HTTP or UI and the diff's tests never import a test client or call a route/element for it"   # mechanical: loop.md check (c)
budgets: { max_iterations: 8, max_verify_rounds: 3, max_tokens: 400000, wall_clock: "2h" }
model_routing: { implementer: "haiku", verifier: "opus", planner: "opus" }
state: "ready-for-agent"   # GitHub label set; depends_on ["9"] is merged. Held only by the pre-flight rulings under Context.
branches: { impl: "impl/10-google-oauth", verify: "verify/10-google-oauth" }
---
## Context (progressive disclosure — links, not inlined bodies)
- **PRE-FLIGHT GAP (orchestrator check 2026-09-19, read before starting):** no Google OAuth config exists anywhere.
  - `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` (and a redirect URI) absent from `.env.example`, `.env` (key names checked), and `app/config.py` `Settings`.
  - `authlib` not in `pyproject.toml` (only `itsdangerous`, `httpx`, `fastapi` present). Authlib's Starlette client also needs Starlette `SessionMiddleware` for `state`; none is registered.
  - Real credentials do not exist → tests MUST mock Google (token exchange + userinfo/id_token); do not attempt a live consent flow. Mark clearly which criteria are covered by mocks only.
  - **PRE-AUTHORIZED (orchestrator, 2026-09-19):** add placeholder keys `GOOGLE_CLIENT_ID=` and `GOOGLE_CLIENT_SECRET=` (empty values, no real credentials) to `.env.example` WITHOUT escalating. This is the ONLY out-of-region edit pre-authorized. (Note `.env.example` may already carry an uncommitted `SECRET_KEY` line from the user — append, do not overwrite or revert it.)
  - Everything else outside owned regions STILL ESCALATES as normal, including `app/config.py` (new Settings fields), `pyproject.toml` (`authlib` dependency) and any SessionMiddleware wiring outside your own main.py region — trigger "any edit outside app/auth.py or own main.py region". List these edits up front and escalate for a ruling (#9 precedent: pyproject dep additions, see .loop/9/journal.md). Do not silently proceed.
  - Make the new settings optional (default `""`) so the app and existing tests still boot without Google creds; `/auth/google/login` should fail with a clear error when unset rather than crash at import.
- Cross-reference: ADR-0003 (docs/adr/0003-optional-cookie-auth-split-identities.md) — email-merge rationale for splitting auth_identities out of users.
- **Client-parity (orchestrator ruling 2026-09-19):** issue #10 says Google sign-in is "indistinguishable except for the entry point", so OAuth success MUST reach the client the same way password login does, i.e. the #12 post-login sync hook must fire after it. #10 owns only the SERVER half: `/auth/google/callback` sets the cookie, then 302s to `/?login=google` (a documented, tested contract). #10 does NOT edit `app/templates/index.html` and does NOT implement the hook — #12 owns that (see .loop/12/LOOP.md). Add a pytest asserting the callback redirect target so the contract is verifier-checkable.
- Uses authlib's Starlette client for OAuth2 authorization-code flow (state/CSRF and PKCE via the library, not hand-rolled).
- **OWNED REGIONS (exhaustive — loop.md check (a) diffs against exactly this list):**
  - `app/auth.py` — extend (from #9), do not restructure existing functions.
  - `tests/test_auth.py` — add `google`-named tests; use `test-10-*` fixtures; do not edit #9's existing tests.
  - `app/main.py` between `# region: google-oauth (#10)` and `# endregion: google-oauth` (inside the `if auth_store is not None and settings is not None:` block, after `/me`). Markers exist on master; keep all new Google routes between them.
  - `app/main.py` `signup` handler: ONLY the `except AuthError` mapping, to return a distinct "this email uses Google sign-in" response for a Google-only email (criterion 4). No other change to signup/login/logout/me.
  - `.env.example`: append the two empty placeholder keys only (pre-authorized above).
  - `.loop/10/journal.md`.
  - NOT owned, still escalates: `app/config.py`, `pyproject.toml`, `SessionMiddleware` wiring, `app/templates/index.html`, anything else. (Human ruling on config/authlib/SessionMiddleware still outstanding — list them up front.)
