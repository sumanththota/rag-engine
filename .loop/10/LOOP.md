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
- Cross-reference: ADR-0003 (docs/adr/0003-optional-cookie-auth-split-identities.md) — email-merge rationale for splitting auth_identities out of users.
- Uses authlib's Starlette client for OAuth2 authorization-code flow (state/CSRF and PKCE via the library, not hand-rolled).
- Hotspot files & owned regions: extends app/auth.py (from #9) — coordinate with #9's owned region, do not restructure it.
