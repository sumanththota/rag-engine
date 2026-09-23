---
id: "9"
title: "Password auth: signup, login, logout, session cookie"
goal: "A user can sign up with email+password, log in, see logged-in state, and log out; anonymous chat is unaffected."
spec_link: "https://github.com/sumanththota/rag-engine/issues/9"   # immutable audit anchor
dag:
  depends_on: []            # can start immediately
  blocks: ["10", "11"]      # google oauth, persist threads
acceptance_criteria:        # copied verbatim from the issue body; each must be able to FAIL
  - "POST /signup creates a users row (argon2id-hashed password) + a provider='password' auth_identities row"
  - "POST /login verifies the password and sets the signed session cookie"
  - "POST /logout clears the cookie"
  - "Depends(get_current_user_optional) returns None for an anonymous/invalid/expired cookie, never raises"
  - "Existing anonymous chat flow is unaffected (no regression)"
  - "APP_ENV/SECRET_KEY added to Settings; missing SECRET_KEY raises ConfigError at boot, same as HANDBOOK_PATH/DATABASE_URL today"
verifier_command: "pytest -q tests/test_auth.py"
escalation_triggers:
  - "schema/migration touches an existing table"   # users/auth_identities are new tables; watch for edits to existing ones
  - "the SAME criterion fails in 3 separate verify rounds"
  - "any edit outside app/auth.py, app/config.py, or the agent's own main.py region"
budgets: { max_iterations: 8, max_verify_rounds: 3, max_tokens: 400000, wall_clock: "2h" }
model_routing: { implementer: "haiku", verifier: "opus", planner: "opus" }
state: "agent:gate-pending"   # mirrors the GitHub label; every acceptance criterion is green (see .loop/9/journal.md)
branches: { impl: "impl/9-password-auth", verify: "verify/9-password-auth" }
---
## Context (progressive disclosure — links, not inlined bodies)
- Cross-reference: ADR-0003 (docs/adr/0003-optional-cookie-auth-split-identities.md) — why auth is additive/optional, and why identity is split into two tables.
- Convention refs: CONVENTIONS.md §1 module map (add app/auth.py row), §7 (agent-loop testing, once added)
- Hotspot files & owned regions: new module app/auth.py (own ensure_schema(), one typed AuthError, matching PostgresStore/TraceStore convention); app/config.py Settings (APP_ENV, SECRET_KEY)
- Password hashing: argon2id (argon2-cffi). Cookie: itsdangerous-signed, HttpOnly, SameSite=Lax, Secure unless APP_ENV=development, sliding 30-day idle expiry.
