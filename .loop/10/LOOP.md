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
  context: 5               # config gap, mock-only constraint, 3 live-verified hazards and pre-authorized edits all spelled out
acceptance_criteria:   # 1-4 copied verbatim from the issue body; 5-6 ADDED by the orchestrator 2026-09-19 (not in the issue) so the verifier, which reads only this list, can see them. Each must be able to FAIL.
                       # Google is MOCKED everywhere (no real credentials): patch authlib's token exchange / id_token parse. A pass proves OUR handling of a Google response, never Google itself.
  - text: "GET /auth/google/login redirects to Google's consent screen with state set"
    verify_via: "HTTP — test client GET /auth/google/login with follow_redirects=False; assert a 3xx, Location host is accounts.google.com, and the query string carries a non-empty `state`"
  - text: "GET /auth/google/callback validates state, verifies email_verified, and sets the same session cookie #9 issues for password login"
    verify_via: "HTTP — test client GET /auth/google/callback. Mock ONLY the network layers (`fetch_access_token`, `parse_id_token`) — NEVER `authorize_access_token`, which would skip the real state check. Must cover: (a) valid state + email_verified:true -> the response has EXACTLY ONE Set-Cookie named `session`, it is the #9 auth cookie (same name/attrs as POST /login; GET /me with it returns that user); (b) mismatched/missing state -> 4xx (not an uncaught 500), no auth cookie; (c) email_verified:false -> 4xx, no auth cookie, no user row created"
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
  - "any edit outside the owned regions listed under Context (app/auth.py, tests/test_auth.py, main.py `# region: google-oauth`, the signup handler's AuthError mapping, and the pre-authorized config.py / pyproject.toml / .env.example edits)"
  - "a criterion names verify_via: HTTP or UI and the diff's tests never import a test client or call a route/element for it"   # mechanical: loop.md check (c)
budgets: { max_iterations: 8, max_verify_rounds: 3, max_tokens: 400000, wall_clock: "2h" }
model_routing: { implementer: "haiku", verifier: "opus", planner: "opus" }
state: "ready-for-agent"   # GitHub label set; depends_on ["9"] is merged.
branches: { impl: "impl/10-google-oauth", verify: "verify/10-google-oauth" }
---
## Context (progressive disclosure — links, not inlined bodies)
- **PRE-FLIGHT GAP (orchestrator check 2026-09-19, read before starting):** no Google OAuth config exists anywhere.
  - `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` (and a redirect URI) absent from `.env.example`, `.env` (key names checked), and `app/config.py` `Settings`.
  - `authlib` not in `pyproject.toml` (only `itsdangerous`, `httpx`, `fastapi` present). Authlib's Starlette client also needs Starlette `SessionMiddleware` for `state`; none is registered.
  - Real credentials do not exist → tests MUST mock Google (token exchange + userinfo/id_token); do not attempt a live consent flow. Mark clearly which criteria are covered by mocks only.
  - **PRE-AUTHORIZED (orchestrator, 2026-09-19):** add placeholder keys `GOOGLE_CLIENT_ID=`, `GOOGLE_CLIENT_SECRET=` and `GOOGLE_REDIRECT_URI=` (empty values, no real credentials) to `.env.example` WITHOUT escalating. Together with the three narrowly-scoped edits in the next bullet, these are the ONLY pre-authorized out-of-region edits. (Note `.env.example` may already carry an uncommitted `SECRET_KEY` line from the user — append, do not overwrite or revert it.)
  - **PRE-AUTHORIZED (orchestrator ruling 2026-09-19, after prototyping authlib 1.8.0 + Starlette in a scratch venv) — these are the ONLY further out-of-region edits, each narrowly scoped; record each in the journal as "pre-authorized, LOOP.md ruling":**
    1. `app/config.py` `Settings`: add exactly three OPTIONAL fields, default `""`: `google_client_id` (alias `GOOGLE_CLIENT_ID`), `google_client_secret` (`GOOGLE_CLIENT_SECRET`), `google_redirect_uri` (`GOOGLE_REDIRECT_URI`). Nothing else in config.py.
    2. `pyproject.toml`: add `authlib>=1.3` to `dependencies`, nothing else. (`itsdangerous`, needed by SessionMiddleware, is already a dependency.)
    3. `SessionMiddleware`: register it INSIDE the `# region: google-oauth` block via `app.add_middleware(...)` (works there — verified in the prototype). No change outside the region.
  - **HAZARD 1 — cookie-name collision (verified live in the prototype, not theoretical):** #9's auth cookie is named `session` (`SESSION_COOKIE_NAME` in app/auth.py) and Starlette's `SessionMiddleware` ALSO defaults to `session`. With the default, the real callback emits a second header `session=null` AFTER the auth cookie and wipes the login. Register it as `SessionMiddleware(app, secret_key=settings.secret_key, session_cookie="oauth_state", ...)`. A test that mocks `authorize_access_token` hides this completely (the state is never popped, so no clobbering header appears) — that is why criterion 2 requires the real state check and asserts EXACTLY ONE `session` Set-Cookie on the callback response.
  - **HAZARD 2 — where to mock:** state validation happens inside `authorize_access_token`, before the token exchange. Patch `oauth.google.fetch_access_token` and `oauth.google.parse_id_token` (both AsyncMock) so the state check stays real. Register the client with EXPLICIT `authorize_url` / `access_token_url` / `jwks_uri` (no `server_metadata_url`, which fetches Google's discovery doc over the network), with `code_challenge_method: "S256"`.
  - **HAZARD 3 — uncaught OAuthError:** a wrong/missing state makes authlib raise `MismatchingStateError` (an `OAuthError`). The callback must catch it and return a 4xx, not let it surface as a 500.
  - Everything else outside owned regions STILL ESCALATES as normal.
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
  - `app/config.py` (3 optional Google fields ONLY) and `pyproject.toml` (`authlib>=1.3` ONLY) — pre-authorized, see rulings above.
  - NOT owned, still escalates: `app/templates/index.html`, any other config.py/pyproject.toml change, anything else.
