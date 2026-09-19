---
id: "12"
title: "Migrate pre-login local Threads into the server on first login"
goal: "A user's anonymous localStorage Threads are uploaded and idempotently upserted into their server-side history right after login."
spec_link: "https://github.com/sumanththota/rag-engine/issues/12"   # immutable audit anchor
dag:
  depends_on: ["11"]   # needs threads/thread_messages tables and a working save/list path to upsert into
  blocks: ["18"]   # login UI calls the afterLogin() this ticket defines
readiness:   # orchestrator-scored 2026-09-19 against the FLP rubric, after the fixes below
  acceptance_criteria: 4   # 1,2,4 are HTTP-falsifiable; 3 is browser-only and gated on #18 (rated down for that)
  context: 5               # hazard, shared-call-site ruling and check (d) levels all spelled out
acceptance_criteria:   # 1-4 copied verbatim from the issue body; 5 ADDED by the orchestrator 2026-09-19 (standing criterion, not in the issue). Each must be able to FAIL.
  - text: "POST /threads/sync accepts a batch of local Threads (id/title/messages) and upserts each into threads/thread_messages for the logged-in user"
    verify_via: "HTTP — POST /threads/sync through a test client as a logged-in user, then GET /threads and /threads/{id} to confirm the rows"
  - text: "Logging in a second time with the same local Threads does not create duplicates (idempotent by Thread id)"
    verify_via: "HTTP — POST the same batch twice, then GET /threads and confirm one row per Thread id; a ThreadStore-direct call cannot satisfy this"
  - text: "Client calls this endpoint automatically right after login succeeds"
    verify_via: "BROWSER — no JS harness in this repo; loop.md check (d) three-level evidence (see Context). GATED on #18: not met until #18 lands."
  - text: "A Thread created anonymously, then synced after login, appears identically in the server-side Thread list (same id, title, messages, in order)"
    verify_via: "HTTP — sync a client-shape payload, then GET /threads/{id} and assert identical id/title/ordered messages"
  - text: "ORCHESTRATOR-ADDED (standing, every feature): the FULL suite is green"
    verify_via: "pytest -q, no -k filter — verifier_command below is narrower than the blast radius of a main.py/threads.py/index.html change (found on #9: full suite caught a regression the scoped run missed)"
verifier_command: "pytest -q tests/test_threads.py -k sync"
escalation_triggers:
  - "schema/migration touches an existing table"
  - "the SAME criterion fails in 3 separate verify rounds"
  - "any edit outside the owned regions listed under Context (app/threads.py, tests/test_threads.py, main.py `# region: threads-sync`, index.html afterLogin()/?login=google only)"   # RE-AUTHORIZED (orchestrator 2026-09-19), no escalation: edits to app/templates/index.html SCOPED TO afterLogin() and the ?login=google boot-time detection ONLY. Any other index.html change — UI, styling, unrelated JS — still escalates.
  - "a criterion names verify_via: HTTP or UI and the diff's tests never import a test client or call a route/element for it"   # mechanical: loop.md check (c)
budgets: { max_iterations: 8, max_verify_rounds: 3, max_tokens: 400000, wall_clock: "2h" }
model_routing: { implementer: "haiku", verifier: "opus", planner: "opus" }
state: "ready-for-agent"   # GitHub label set; depends_on ["11"] is merged. Criterion 3 stays BLOCKED on #18 (ruling on how that interacts with gate-pending is outstanding).
branches: { impl: "impl/12-sync-threads-login", verify: "verify/12-sync-threads-login" }
---
## Context (progressive disclosure — links, not inlined bodies)
- No client-side sync-tracking by design: every login re-uploads everything; the upsert is a no-op for anything already stored.
- **ONE shared call site (orchestrator ruling 2026-09-19):** the post-login sync fires from a SINGLE client function (e.g. `afterLogin()` in app/templates/index.html) that every auth path calls — NOT duplicated per auth method. Same shared-function principle as #11's thread_id bounds check. Two entry points must reach it: (a) password `POST /login` success; (b) Google OAuth, which is a full-page redirect (no fetch success handler): #10's callback 302s to `/?login=google`, and boot code detects that param and calls the same `afterLogin()`. Do not add sync code inside a per-method handler.
- **HAZARD — ordering vs hydration:** `hydratThreadsFromServer()` (index.html ~L1377) REPLACES localStorage threads with server threads whenever `/me` returns a user. On an OAuth redirect reload it runs at boot, so if it runs before the sync, anonymous Threads are silently overwritten = data loss. `afterLogin()` must do sync → THEN hydrate, and boot must not call hydrate independently ahead of it on a login-marker load.
- **No JS harness (confirmed 2026-09-19: no package.json, playwright/jest/vitest config, or JS tests in the repo)** → the hydration-race requirement and criterion 3 are browser behavior. Apply `.claude/commands/loop.md` check (d) EXACTLY as written for #11 — all three levels, in order, none skippable. Do NOT pretend a pytest proves the browser ordering, and do NOT leave it silently unverifiable:
  1. **Static grep (absence = definite fail; presence necessary, not sufficient):** in app/templates/index.html, `afterLogin()` calls `/threads/sync`; the sync `await`/`.then` chain completes BEFORE `hydratThreadsFromServer()` is invoked inside `afterLogin()` (paste the two line numbers); boot code does not call `hydratThreadsFromServer()` independently ahead of `afterLogin()` on a `?login=google` load; every auth path calls the one `afterLogin()`.
  2. **Pytest on the server-side contract:** seed a REAL local Thread payload (real id, title, non-empty ordered messages, in the shape the client sends) as an anonymous user BEFORE login; real `POST /login`; `POST /threads/sync` with that payload; then assert THAT SAME Thread is in `GET /threads` and `GET /threads/{id}` with identical id, title and messages in order. "No error" / "sync returned 200" does NOT count. Must be able to FAIL (e.g. drop the upsert → red; record it in the journal). Proves the API contract, not the browser ordering.
  3. **Manual artifact pasted in the journal (self-report is not evidence):** in a real browser, seed a real Thread in localStorage, log in (password path via `fetch('/login')` in the console + calling `afterLogin()` — works without #18's form; and a `/?login=google` load), then paste actual output: localStorage `state.threads` BEFORE and AFTER, and the `GET /threads` response body, showing the seeded Thread survived with identical id/title/messages. "Confirmed working" is a fail.
  This repo having no JS harness is a standing gap (loop.md), not something to route around; note it in the journal, don't fix it here.
- **CRITERION 3 IS GATED ON #18 (login UI):** index.html has no login form or auth code at all (only `/me` in hydration); #9 shipped API endpoints only. Issue #18 (https://github.com/sumanththota/rag-engine/issues/18, depends_on #9 and #12) adds the form and calls `afterLogin()`. Criterion 3 ("Client calls this endpoint automatically right after login succeeds") **MUST NOT be marked met until #18 has landed** — by implementer, verifier, or orchestrator. Until then report it as BLOCKED/UNVERIFIED, never "met". #12 owns the definition of `afterLogin()` (sync → then hydrate) and the `?login=google` boot-marker path; #18 only calls it. Do NOT build a login form in #12. Criteria 1, 2, 4 are not gated. This criterion is browser-only and NOT reachable by `pytest -k sync` (same JS/backend gap as #11 retro). It gets the same check (d) three-level treatment as the hydration race above — but "met" additionally waits on #18, so until then the honest status is BLOCKED, not "unverifiable" and not "met". State exactly how any claim about it was verified. (Split already done per #11 retro: #18 is the `depends_on` edge for the browser-side login UI.)
- **OWNED REGIONS (exhaustive — loop.md check (a) diffs against exactly this list):**
  - `app/threads.py` — extend (from #11); reuse #11's shared thread_id bounds-check function, do not duplicate it.
  - `tests/test_threads.py` — add `sync`-named tests; use `test-12-*` fixtures; do not edit #11's existing tests.
  - `app/main.py` between `# region: threads-sync (#12)` and `# endregion: threads-sync` (between the DELETE /threads/{thread_id} route and the traces section). Markers exist on master; put `POST /threads/sync` there.
  - `app/templates/index.html`: `afterLogin()` and the `?login=google` boot-time detection ONLY (client post-login code is in index.html; no other file).
  - `.loop/12/journal.md`.
  - NOT owned, still escalates: any other index.html change, `app/config.py`, `pyproject.toml`, anything else.
