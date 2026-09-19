---
id: "10"
title: "Google OAuth login, merged into the same account model"
goal: "Sign in with Google issues the same session cookie as password login (#9), auto-merging into an existing account by verified email."
spec_link: "https://github.com/sumanththota/rag-engine/issues/10"   # immutable audit anchor
dag:
  depends_on: ["9"]   # needs users/auth_identities schema, cookie issuance, find_or_create_user
  blocks: []
acceptance_criteria:   # copied verbatim from the issue body; each must be able to FAIL
  - "GET /auth/google/login redirects to Google's consent screen with state set"
  - "GET /auth/google/callback validates state, verifies email_verified, and sets the same session cookie #9 issues for password login"
  - "A Google login with an email matching an existing password-signup user adds a new auth_identities row to that same users.id (no duplicate user created)"
  - "A password-signup attempt against a Google-only email is rejected with a clear message, not silently overwritten"
verifier_command: "pytest -q tests/test_auth.py -k google"
escalation_triggers:
  - "schema/migration touches an existing table"
  - "the SAME criterion fails in 3 separate verify rounds"
  - "any edit outside app/auth.py or the agent's own main.py region"
budgets: { max_iterations: 8, max_verify_rounds: 3, max_tokens: 400000, wall_clock: "2h" }
model_routing: { implementer: "haiku", verifier: "opus", planner: "opus" }
state: "ready-for-agent"   # GitHub label is already set, but depends_on ["9"] is not yet merged —
                            # the orchestrator's readiness check (label AND depends_on merged) keeps
                            # this from actually being picked until #9 lands
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
- Hotspot files & owned regions: extends app/auth.py (from #9) — coordinate with #9's owned region, do not restructure it.
