---
id: "32"
title: "Optional login: signup UI, dismissible sign-in modal, logout (supersedes #18 blocking overlay)"
goal: "The app loads straight into a usable anonymous chat for everyone; login/signup live behind a dismissible header-triggered modal (Sign in / Create account), a logged-in user sees their email + Sign out in the header, and the always-shown #18 overlay is removed entirely, restoring ADR-0003 (auth additive, never gating)."
spec_link: "https://github.com/sumanththota/rag-engine/issues/32"   # immutable audit anchor
dag:
  depends_on: []   # backend routes (/signup, /login, /logout, /me from #9/#10, afterLogin() from #12) already merged; this ticket is frontend-only reuse
  blocks: []
readiness:   # orchestrator-scored 2026-09-21 against the FLP rubric
  acceptance_criteria: 4   # falsifiable, but almost entirely browser behavior — check (d) applies to most; this ticket ALSO introduces the JS harness that upgrades check (d)'s evidence bar
  context: 4               # backend contract is stable and unchanged; the only real unknown is whether `npx playwright install chromium` succeeds in the verifier's sandbox (no network -> falls back to check (d))
acceptance_criteria:   # reworded from the issue's 38 user stories into falsifiable criteria; 1-10 map to the spec, 11-12 are ORCHESTRATOR-ADDED
  - text: "On load, an anonymous user (GET /me -> {\"user\": null}) sees no modal/overlay of any kind, and the chat input is immediately focusable and usable"
    verify_via: "UI — Playwright: navigate with no session cookie, assert no element with the modal's role/selector is visible, assert the chat input is enabled and can receive focus. This is the root-cause regression #18 introduced (a non-dismissible full-screen overlay) — the single most important criterion in this ticket."
  - text: "The header shows a 'Sign in' control for an anonymous user; clicking it opens the auth modal, which is otherwise never shown on load or by any other trigger"
    verify_via: "UI — Playwright: assert header 'Sign in' button exists pre-click and the modal is absent/hidden; click it; assert the modal is now visible"
  - text: "The modal offers Sign in and Create account modes toggled in place (not separate pages), and closes via an X control, the Escape key, and a backdrop click — each leaving the current Thread's content unchanged"
    verify_via: "UI — Playwright: open modal, toggle between modes and assert both render; type into the chat input first, close via each of the three mechanisms in turn (three assertions), and confirm the typed content is still present after each"
  - text: "Signing up (email + password) returns 201, then the client automatically logs in with the same credentials (no separate manual login step) and the header switches to showing the email + 'Sign out' without a page reload"
    verify_via: "HTTP — existing tests/test_auth.py already covers POST /signup contract (201, duplicate 409, google-only 409) unchanged; UI — Playwright: submit Create account with a fresh test-32-* email, assert POST /signup then POST /login both fire (via a network listener), assert header shows the email with no navigation event"
  - text: "Signup failures render a clear inline message in the still-open modal with the typed email retained: duplicate email (409) and a Google-only email (409) show visibly distinct text"
    verify_via: "UI — Playwright: attempt signup with an email seeded as password-registered, assert an inline 'already registered' message and the modal remains open with the email field populated; repeat with an email seeded via find_or_create_user_with_google_identity, assert a distinct 'registered with Google' message"
  - text: "After signup or login, any Threads held only in localStorage are synced to the server (via #12's existing afterLogin()) and the visible Thread list reflects the server copy"
    verify_via: "UI — Playwright: as an anonymous user create a local Thread, then sign up; assert a network call to POST /threads/sync fires before the Thread list re-renders from the server (afterLogin()'s own sync-then-hydrate order is #12's, already tested there — this criterion only proves #32's modal actually triggers it, not the ordering itself)"
  - text: "Signing in with correct credentials logs in, closes the modal, and hydrates server Threads; wrong credentials (401) show a clear inline error and leave the modal open"
    verify_via: "UI — Playwright: sign in with a seeded test-32-* user's correct credentials, assert modal closes and header shows the email; separately sign in with a wrong password, assert an inline error and the modal still open"
  - text: "Signing out calls POST /logout, the header reverts to 'Sign in', and server-backed Threads are removed from the visible Thread list, replaced by client-side (localStorage) Threads"
    verify_via: "UI — Playwright: log in, confirm a server Thread is visible, sign out, assert header shows 'Sign in' again and the previously-visible server Thread is no longer in the list"
  - text: "On load, a valid session shows the logged-in header state immediately; an expired/invalid session or a failing /me call renders the anonymous state and never blocks chat"
    verify_via: "UI — Playwright: (a) load with a valid session cookie, assert header shows email without any click; (b) load with a tampered/expired cookie, assert anonymous header and a usable chat input; (c) route /me to fail (Playwright route interception), assert anonymous header, no thrown error, and chat input still usable"
  - text: "An anonymous user sees a single-line, non-blocking hint (near the Thread list) that signing in saves Threads across devices; a logged-in user does not see it"
    verify_via: "UI — Playwright: assert the hint element is visible and is a single line (no modal/overlay semantics) for an anonymous load; assert it is absent after login"
  - text: "ORCHESTRATOR-ADDED: this diff is frontend-only — no route, schema, or backend auth-logic change. The reused routes' behavior (POST /signup, POST /login, POST /logout, GET /me, POST /threads/sync) is provably identical to pre-#32 (same status codes, same response bodies)"
    verify_via: "pytest -q tests/test_auth.py tests/test_threads.py — unmodified from their current form; a diff to app/auth.py, app/main.py's auth routes, or app/threads.py, OR any change to these tests' assertions, is itself a FAIL of this criterion regardless of green output"
  - text: "ORCHESTRATOR-ADDED (standing, every feature): the FULL suite is green"
    verify_via: "pytest -q, no -k filter"
verifier_command: "pytest -q tests/test_login_ui.py tests/test_auth.py tests/test_threads.py"   # contract-level guard; the Playwright suite (criteria 1-3,5-10) is run SEPARATELY, see Context 'Playwright execution' below — the verifier attempts it via Bash and falls back to check (d) only if the sandbox has no network for the Chromium install
escalation_triggers:
  - "any edit to app/auth.py, app/threads.py, or app/main.py's auth/threads routes (/signup, /login, /logout, /me, /threads/sync) — this ticket is frontend-only per its own spec"
  - "any schema/migration of any kind"
  - "any edit outside the owned regions listed under Context"
  - "any code in this diff that DEFINES or duplicates afterLogin()'s sync-then-hydrate logic instead of calling #12's existing window.afterLogin()"
  - "the SAME criterion fails in 3 separate verify rounds"
  - "a criterion names verify_via: HTTP or UI and the diff's tests never call the relevant route/element/Playwright action for it"   # mechanical: loop.md check (c); check (d) applies additionally to the Playwright-covered criteria
  - "a new npm dependency added to package.json other than @playwright/test and its own transitive installer"
  - "removal or narrowing of the existing password-signup, login, logout, /me, or /threads/sync HTTP contract (see ORCHESTRATOR-ADDED criterion 11)"
budgets: { max_iterations: 8, max_verify_rounds: 3, max_tokens: 500000, wall_clock: "3h" }
model_routing: { implementer: "haiku", verifier: "sonnet", planner: "sonnet" }
isolation:
  worktree: true          # CHECK at spawn via `git worktree list`
  venv_rebuilt: true       # python -m venv .venv && .venv/bin/pip install -e ".[dev]" per CONVENTIONS.md §7
  env_via: ".worktreeinclude"
  db_scope: "test-32-*"
  file_regions:
    - "app/templates/index.html"   # only inside new `// region: auth-ui (#32)` / `<!-- region: auth-ui (#32) -->` markers; see below re: removing #18's old overlay markers
    - "package.json"                # NEW file — devDependency @playwright/test only
    - "playwright.config.ts"        # NEW file
    - "tests/e2e/**"                # NEW directory — Playwright specs
    - "tests/test_login_ui.py"      # edits limited to static-markup assertions that referenced the removed #18 overlay; the HTTP-contract assertions (GET /me, POST /login) must survive unchanged or be moved verbatim
    - ".loop/32/journal.md"
state: "ready-for-agent"
branches: { impl: "impl/32-optional-login-ui", verify: "verify/32-optional-login-ui" }
---
## Context (progressive disclosure — links, not inlined bodies)

- **This ticket supersedes #18's criterion 1, not #18's routes.** #18 built the
  always-shown, non-dismissible `#login-overlay` (index.html ~L573-701 CSS/markup,
  ~L1561-1653 JS) — that overlay IS the regression this ticket fixes (ADR-0003 says
  auth is additive, never gating; #18 shipped it gating). The old `<!-- region:
  login-ui (#18) -->` / `<!-- endregion: login-ui (#18) -->` markers (and their JS/CSS
  counterparts) are OWNED by this ticket for REMOVAL and replacement with new
  `region: auth-ui (#32)` markers — this is a pre-authorized exception to the normal
  "don't touch another ticket's region" rule, made explicit here so check (a) does not
  flag it as an out-of-region edit. Do not touch anything else in index.html.
- **`afterLogin()` (index.html ~L1655-1700, `<!-- region: after-login (#12) -->`) is
  NOT owned by this ticket and must not be edited.** #32 only calls
  `window.afterLogin()` after a successful login or signup — exactly like #18 did.
  Signup has no existing call site for it; wire it the same way #18's login submit
  handler does (call it once, on success, never on failure).
- **Backend is reused unchanged, per the issue's own "Implementation Decisions":**
  `POST /signup` returns 201 without setting a session cookie — the client must call
  `POST /login` with the same credentials immediately after a successful signup to
  establish the session (there is no backend auto-login). This is why ORCHESTRATOR-ADDED
  criterion 11 exists: it is easy to accidentally "fix" this by editing `/signup` itself,
  which is explicitly out of scope and an escalation trigger.
- **New JS test harness (Playwright) — this ticket closes the "no JS harness" gap
  every prior retro (#10, #11, #12, #18) flagged as standing.** `node` v26 and `npx`
  are both present in this environment (orchestrator-verified 2026-09-21). Add a
  minimal root `package.json` (devDependency `@playwright/test` only, no other JS
  tooling, no build step, no effect on `pyproject.toml`/Python packaging) and a
  `playwright.config.ts` pointed at a test server on an ephemeral port — reuse the
  `flp-test-instance` pattern (port 8099, never `:8080`) from prior tickets' check
  (d) level 3, but now as a real automated `webServer` config Playwright starts
  itself, rather than an orchestrator-manual step.
  **Playwright execution, this ticket only:** because a real harness now exists,
  criteria 1-3 and 5-10 (all UI-only) are evidenced primarily by real Playwright runs,
  not manual check (d). Order of evidence, most authoritative first:
    1. `npx playwright test` (headless Chromium) actually passing — the verifier
       should attempt `npx playwright install chromium && npx playwright test` itself
       via its own Bash access. If the verifier's sandbox has no network (the
       Chromium download fails), this step is unavailable to it — fall back to the
       three-level check (d) process from loop.md, run by the ORCHESTRATOR (which
       does have a browser), for whichever criteria the verifier could not exercise.
    2. Either way, `tests/e2e/**`'s source must still pass check (c)/(d)-1: grep
       confirms each spec actually drives the relevant route or DOM element, not a
       hollow assertion.
  Do not let "Playwright isn't installed in this sandbox" become silent skip — it is
  either run for real, or explicitly routed to the orchestrator's manual fallback and
  recorded as such in the journal. A criterion with neither is a FAIL, not a PASS.
- **`test-32-*` email/user-fixture prefix**, per CONVENTIONS.md §7 — this ticket runs
  in its own worktree against the shared dev Postgres alongside any other in-flight
  ticket; never reuse another ticket's literal.
- **Existing HTTP contract coverage is sufficient and must not be extended or
  weakened**: `tests/test_auth.py` (signup/login/logout/me, ~28 tests) and
  `tests/test_threads.py` (`/threads/sync`, from #12) already prove the backend
  contract this ticket's UI calls into. Per the issue's own "Testing Decisions", these
  "stay as regression checks and need little or no extension" — ORCHESTRATOR-ADDED
  criterion 11 makes that a hard gate, not a suggestion.
- **`tests/test_login_ui.py` needs surgery, not a rewrite.** Its HTTP-contract tests
  (`GET /me`, `POST /login`) are unaffected by the overlay's removal and must survive.
  Its static-markup checks that grep `index.html` for `#login-overlay`/`#login-form`
  will break once that markup is removed — update ONLY those assertions to match the
  new header/modal structure; do not delete the file's HTTP-contract tests.
- Cross-reference: `Anonymous user` glossary entry in CONTEXT.md; ADR-0003 (auth
  additive, not gating) — this ticket exists to restore conformance with it;
  CONVENTIONS.md §7 (agent-loop testing conventions).
- **Working-tree note from the issue itself:** "Working-tree changes to trace filtering
  are unrelated to this work" — if any uncommitted trace-filter changes are visible in
  the shared dev environment when this ticket's worktree is created, they belong to
  #33-#40's line of work, not this one; do not touch them.
