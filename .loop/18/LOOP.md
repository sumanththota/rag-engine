---
id: "18"
title: "Login UI: form + submit calling afterLogin()"
goal: "An anonymous user sees a login form; a successful login calls #12's afterLogin() exactly once and switches to the logged-in state; a failed login shows an error and calls nothing."
spec_link: "https://github.com/sumanththota/rag-engine/issues/18"   # immutable audit anchor
dag:
  depends_on: ["9", "12"]   # #9: POST /login, GET /me. #12: DEFINES afterLogin() — #18 only calls it, never defines or duplicates it
  blocks: []
readiness:   # orchestrator-scored 2026-09-19 against the FLP rubric
  acceptance_criteria: 4   # all four are falsifiable, but all are browser behavior with no JS harness — evidence is check (d), not a pytest
  context: 4               # scope boundaries and the afterLogin() ownership split are explicit; #12's final afterLogin() shape is not yet known
acceptance_criteria:   # copied verbatim from the issue body; each must be able to FAIL. ALL are UI behavior -> loop.md check (d) applies to every one (three levels, all required).
  - text: "The client renders a login form (email, password) for anonymous users (/me returns {\"user\": null})"
    verify_via: "UI — check (d): (1) grep index.html for the form inputs and the /me branch that shows/hides it; (2) pytest that GET /me returns {\"user\": null} with no cookie and {\"user\": {...}} with one; (3) orchestrator browser artifact: DOM/screenshot with form present when logged out"
  - text: "Submitting the form calls POST /login; on success it calls afterLogin() exactly once, and never on a failed login"
    verify_via: "UI — check (d): (1) grep that the submit handler fetches /login and calls afterLogin() only in the success branch, and does not define it; (2) pytest that POST /login returns 200 + cookie for good credentials; (3) orchestrator browser artifact: console log counting afterLogin() calls (wrap it, do not edit it) across one good and one bad login"
  - text: "A failed login (401) shows a clear error message and does not call afterLogin()"
    verify_via: "UI — check (d): (1) grep for the 401 branch that renders a message; (2) pytest that POST /login with bad credentials returns 401 with an error body the JS can read; (3) orchestrator browser artifact: screenshot/DOM text of the message plus a zero call count"
  - text: "Once logged in, the form is no longer shown"
    verify_via: "UI — check (d): (1) grep for the code path hiding the form on login and on a /me user; (2) covered by the /me pytest above; (3) orchestrator browser artifact: DOM after login AND after a page reload with the cookie"
  - text: "ORCHESTRATOR-ADDED (standing, every feature): the FULL suite is green"
    verify_via: "pytest -q, no -k filter"
verifier_command: "pytest -q tests/test_login_ui.py"   # level-2 contract tests only; the UI behavior itself is NOT proven by this command
escalation_triggers:
  - "schema/migration touches an existing table"
  - "the SAME criterion fails in 3 separate verify rounds"
  - "any edit outside the owned regions listed under Context"
  - "any code in this diff that DEFINES afterLogin() or duplicates its sync/hydrate logic"   # #12 owns it
  - "a criterion names verify_via: HTTP or UI and the diff's tests never import a test client or call a route/element for it"   # mechanical: loop.md check (c); for UI criteria check (d) applies instead
budgets: { max_iterations: 8, max_verify_rounds: 3, max_tokens: 400000, wall_clock: "2h" }
model_routing: { implementer: "haiku", verifier: "opus", planner: "opus" }
state: "ready-for-agent"   # 2026-09-20: #12 merged (5860f7c) and #9 already merged — both depends_on satisfied. This ticket's criterion 2 (submit calls afterLogin() on success) is what closes #12's own BLOCKED-ON-18 carve-out; that's the correct order, do not re-derive afterLogin() here.
branches: { impl: "impl/18-login-ui", verify: "verify/18-login-ui" }
---
## Context (progressive disclosure — links, not inlined bodies)
- **Circular-looking edge, resolved:** #12 defines `afterLogin()` and gates its own criterion 3 on this ticket; this ticket depends on #12 for the function. Order is #12 first (criterion 3 carried as BLOCKED-ON-18), then #18, then criterion 3 is re-checked once with the real form. See .loop/12/LOOP.md carve-out ruling.
- **OWNED REGIONS (exhaustive — loop.md check (a) diffs against exactly this list):**
  - `app/templates/index.html`: the login form markup, its submit handler, and the show/hide toggle ONLY. Wrap all new JS between `// region: login-ui (#18)` and `// endregion: login-ui` comment markers so the diff is checkable. It must CALL `afterLogin()` and nothing else from #12's code.
  - `tests/test_login_ui.py` (new file) — level-2 contract tests, `test-18-*` fixtures.
  - `.loop/18/journal.md`.
  - NOT owned, still escalates: any Python under `app/`, `pyproject.toml`, any index.html change outside the markers (UI styling beyond usable, signup UI, logout UI, Google button — explicitly out of scope per the issue).
- **The whole client script is one IIFE** (index.html ~L645): new code must live inside it to reach `afterLogin()`; nothing here is reachable from the console unless #12's `window.afterLogin` seam exists (it will, per .loop/12/LOOP.md) — do not add further `window.*` exposure.
- **No JS harness in this repo** (no package.json, playwright/jest/vitest config): a pytest cannot be the acceptance evidence for any criterion here. Check (d) levels 1-3 apply to all four. The orchestrator produces level 3 with `preview_start` config `flp-test-instance` (port 8099) and posts it to the PR. That gap is a standing item, not something to fix in this ticket.
- Issue #12's readiness note: "afterLogin() must do sync → THEN hydrate" — this ticket must not call hydrate independently around it.
