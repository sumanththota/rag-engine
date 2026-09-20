---
id: "12"
title: "Migrate pre-login local Threads into the server on first login"
goal: "A user who chatted anonymously (Threads in localStorage only), then logs in, has those Threads uploaded and upserted server-side exactly once per Thread, then sees the server copy via the existing hydrate path."
spec_link: "https://github.com/sumanththota/rag-engine/issues/12"   # immutable audit anchor
dag:
  depends_on: ["11"]   # needs the threads/thread_messages tables and ThreadStore (list/get/create/write_message)
  blocks: ["18"]        # #18's login-form submit handler calls the afterLogin() this ticket defines
readiness:   # orchestrator-scored 2026-09-20 against the FLP v3 rubric
  acceptance_criteria: 6   # 1-4 verbatim from issue (4 reworded re: "same id", see HAZARD 1); 5-6 orchestrator-added
  context: 5               # upsert key, ordering hazard, and afterLogin()/#18 circularity all pre-resolved with sources below
acceptance_criteria:   # 1-4 copied from the issue body (4 reworded — see HAZARD 1 below); 5-6 ADDED by the orchestrator 2026-09-20 so the verifier, which reads only this list, can see them. Each must be able to FAIL.
  - text: "POST /threads/sync accepts a batch of local Threads (client_thread_id/title/messages) and upserts each into threads/thread_messages for the logged-in user"
    verify_via: "HTTP — POST /threads/sync with a test client and a real session cookie (test-12-* fixtures); assert the response, then independently GET /threads/{id} and confirm title + messages match the submitted payload, in order. A ThreadStore-direct call cannot satisfy this — must go through the route."
    attacker_perspective: false
    seeds_precondition: false
    shared_surface: "none"
    output_shape: "ThreadSyncResult"   # new shared model: [{client_thread_id, id}, ...] — lets the client remap ids; every response this route returns routes through it, not a hand-built dict
    assertion_depth: true   # criterion cites a specific mechanism (upsert) — verifier reads that the test asserts real thread_messages rows exist, not just a 200
    mutation_target: "the ON CONFLICT (user_id, client_thread_id) upsert clause in ThreadStore.sync_threads — remove it (make sync unconditionally INSERT) and confirm the round-2 dedup test (criterion 2) goes red"
  - text: "Logging in a second time with the same local Threads does not create duplicates (idempotent by Thread id)"
    verify_via: "HTTP — call POST /threads/sync twice with an identical payload (same client_thread_id both times); assert list_threads for that user returns exactly one thread for it and thread_messages count does not double after the second call"
    attacker_perspective: false
    seeds_precondition: true   # seeds the real synced thread via call 1, then negates duplication on call 2
    shared_surface: "none"
    output_shape: "none"
    assertion_depth: true
    mutation_target: "the (user_id, client_thread_id) partial unique index / ON CONFLICT target on `threads` — drop it and confirm this test goes red (a second row created)"
  - text: "BLOCKED-ON-18 — Client calls this endpoint automatically right after login succeeds"
    verify_via: "UI — check (d), carve-out per .loop/18/LOOP.md: #18 owns the login form/submit handler that IS the login-success trigger; it doesn't exist yet. This ticket's evidence ceiling is (1) grep confirming `window.afterLogin` is defined in index.html and calls fetch('/threads/sync', ...) BEFORE calling hydratThreadsFromServer() (sync THEN hydrate, never reversed — see readiness note carried from the issue); (2) pytest asserting /threads/sync behaves correctly when called (criteria 1/2/4 cover this); (3) orchestrator browser evidence: with a session cookie set manually (no form yet), invoke `window.afterLogin()` from the console and paste the resulting network calls (POST /threads/sync, then GET /threads) into the PR. The 'automatically, right after a real login submit' half of this criterion is EXCLUDED from this ticket's PASS and is re-checked by #18 once its submit handler exists and calls this same function — do not attempt to build a login form here to close it fully; that is #18's owned region."
    attacker_perspective: false
    seeds_precondition: false
    shared_surface: "none"
    output_shape: "none"
    assertion_depth: true   # cites a specific ordering guarantee (sync before hydrate)
    mutation_target: "the call order inside window.afterLogin() — swap hydrate before sync and confirm the ordering-sensitive test (asserting the sync fetch resolves before the /threads GET fires) goes red"
  - text: "A Thread created anonymously, then synced after login, appears identically in the server-side Thread list (title, messages, in order) as ONE thread — not duplicated across repeated syncs. (Reworded from the issue's 'same id' — see HAZARD 1: local ids are client-generated strings, server ids are bigserial integers, so literal id equality is impossible; 'same' means one server thread, content-identical, keyed by client_thread_id.)"
    verify_via: "HTTP — build a local-shaped payload (client_thread_id, title, >=3 ordered user/assistant messages with sources) submitted in one batch; POST /threads/sync, then GET /threads/{id} and assert title/messages/order match byte-for-byte, including after a second identical sync"
    attacker_perspective: false
    seeds_precondition: false
    shared_surface: "none"
    output_shape: "none"
    assertion_depth: true
    mutation_target: "the `id ASC` tiebreaker added to ThreadStore.get_thread's message ORDER BY (see HAZARD 2) — revert to `ORDER BY created_at ASC` alone and confirm a same-transaction multi-message sync test goes red on ordering"
  - text: "ORCHESTRATOR-ADDED: /threads/sync only ever writes into the CALLER's own user_id (from the session cookie), never a client-supplied one — a crafted payload cannot target another user's threads"
    verify_via: "HTTP — as user A, POST /threads/sync with a payload that includes any user-identifying field if the route accepts one (it must not); separately, seed a real thread for user B, then have user A sync a client_thread_id that collides with user B's, and confirm user B's thread is untouched (still user B's, unchanged) and user A got a NEW thread instead"
    attacker_perspective: true
    seeds_precondition: true   # seeds user B's real thread first, then confirms user A's sync did not touch it
    shared_surface: "none"
    output_shape: "none"
    assertion_depth: true
    mutation_target: "user_id sourced from `_get_current_user_optional` in the /threads/sync route — change it to read a client-supplied field instead and confirm this test goes red"
  - text: "ORCHESTRATOR-ADDED (standing, every feature): the FULL suite is green"
    verify_via: "pytest -q, no -k filter — verifier_command below is narrower than the blast radius of a threads.py/main.py change (found on #9 and #10: a scoped run missed a regression the full suite caught)"
    attacker_perspective: false
    seeds_precondition: false
    shared_surface: "none"
    output_shape: "none"
    assertion_depth: false
    mutation_target: "none"
verifier_command: "pytest -q tests/test_threads.py"   # was "-k sync" — orchestrator correction 2026-09-20: the verifier
                                                        # found this filter silently excluded test_after_login_defined_in_index_html
                                                        # and test_get_thread_orders_messages_with_id_tiebreaker (neither name contains
                                                        # "sync"), so criteria 3 and 4's only real guards never ran under this command.
                                                        # Run the whole file instead — no filter to accidentally exclude a criterion's test.
escalation_triggers:
  - "schema/migration touches an existing table, OTHER than the pre-authorized `client_thread_id` column + partial unique index on `threads` described in Context below"
  - "the SAME criterion fails in 3 separate verify rounds"
  - "any edit outside the owned regions listed under Context"
  - "a criterion names verify_via: HTTP or UI and the diff's tests never import a test client or call a route/element for it"   # mechanical: loop.md check (c)
  - "any code path in /threads/sync that reads a user id from the request body or query string instead of the session-derived current user"   # closes ORCHESTRATOR-ADDED criterion 5 specifically
  - "any code in this diff that DEFINES the login submit flow, or builds login-form UI"   # #18 owns that; this ticket only defines afterLogin() itself
budgets: { max_iterations: 8, max_verify_rounds: 3, max_tokens: 400000, wall_clock: "2h" }
model_routing: { implementer: "haiku", verifier: "sonnet", planner: "sonnet" }
# verifier/planner changed opus -> sonnet 2026-09-20 (user request, standing default in
# verifier.md + loop.md, not a per-ticket override). Rounds 1-2's verifiers ran on opus
# per the default at the time — this only affects verifier rounds from here on.
isolation:
  worktree: true          # CHECKED at spawn via `git worktree list`
  venv_rebuilt: true       # CHECKED — pyvenv.cfg path must match the worktree, never copied
  env_via: ".worktreeinclude"   # repo has one, covers `.env`
  db_scope: "test-12-*"
  file_regions:
    - "app/threads.py"
    - "app/main.py"                          # only inside `# region: threads-sync (#12)` / `# endregion`
    - "app/templates/index.html"             # only inside `// region: after-login (#12)` / `// endregion`
    - "tests/test_threads.py"
    - ".loop/12/journal.md"
state: "ready-for-agent"   # depends_on ["11"] is merged; GitHub label set alongside this file
branches: { impl: "impl/12-thread-sync", verify: "verify/12-thread-sync" }
---
## Context (progressive disclosure — links, not inlined bodies)

- Cross-reference: Thread glossary entry in CONTEXT.md; CONVENTIONS.md §7 (agent-loop testing conventions, test-<id>-* fixtures).
- Prior art this ticket extends: `app/threads.py` (`ThreadStore`, from #11) and the existing client hydrate path `hydratThreadsFromServer()` in `app/templates/index.html` (~L1377), which already replaces localStorage threads with server threads on page load for a logged-in user. This ticket adds the piece before it: uploading what's still ONLY in localStorage, then calling that same hydrate function — sync, THEN hydrate, never the reverse (a hydrate-first call would overwrite local threads with the server's old list before the upload lands).

- **HAZARD 1 — "same id" in the issue is unsatisfiable literally (orchestrator finding, 2026-09-20, source-read against `app/threads.py` and the client's `uuid()` helper at index.html ~L694):** local Thread ids are client-generated strings (`crypto.randomUUID()` or `"t-"+Date.now()+...`); `threads.id` is Postgres `bigserial`. There is no value that is simultaneously a valid client id and a valid server id. Read the issue's "same id" as: the upload of one local Thread produces (or updates) exactly ONE server Thread, matched across repeated syncs by a NEW `client_thread_id` column (below), not by numeric equality. After hydrate replaces the client's list with server-issued numeric ids, the original string id is gone from the client entirely — that's expected, not a defect.

- **PRE-AUTHORIZED (orchestrator, 2026-09-20) — schema migration, narrowly scoped, inside `ThreadStore.ensure_schema()`'s existing idempotent `CREATE ... IF NOT EXISTS` style:**
  ```sql
  ALTER TABLE threads ADD COLUMN IF NOT EXISTS client_thread_id text;
  CREATE UNIQUE INDEX IF NOT EXISTS threads_user_client_id_idx
    ON threads (user_id, client_thread_id) WHERE client_thread_id IS NOT NULL;
  ```
  This is the ONLY schema change authorized for this ticket. Do not alter `thread_messages`, do not drop/rename any existing column. **HAZARD (Postgres partial-index inference):** `INSERT ... ON CONFLICT (user_id, client_thread_id) DO UPDATE` will raise "no unique or exclusion constraint matching the ON CONFLICT specification" against a *partial* unique index unless the `ON CONFLICT` clause repeats the predicate: `ON CONFLICT (user_id, client_thread_id) WHERE client_thread_id IS NOT NULL DO UPDATE SET title = EXCLUDED.title, updated_at = now()`. Verified against Postgres's documented conflict-inference rules for partial indexes — test this explicitly, it fails silently-obscurely otherwise (a 500, not a clean upsert).

- **HAZARD 2 — message ordering under a same-transaction bulk insert (orchestrator finding, 2026-09-20):** `ThreadStore.get_thread` currently orders messages `ORDER BY created_at ASC` only. Postgres's `now()` is fixed for the lifetime of a transaction — if the sync route inserts a Thread's several messages inside one `conn.transaction()` (recommended, for upsert atomicity), every message in that batch gets an IDENTICAL `created_at`, and `ORDER BY created_at ASC` alone no longer preserves submission order (criterion 4 needs "in order"). Fix by adding an `id ASC` tiebreaker: `ORDER BY created_at ASC, id ASC` (id is bigserial, so insertion-ordered regardless of timestamp ties). This is an edit to an EXISTING, already-tested (#11) query — it's inside the owned `app/threads.py` region, but call it out explicitly in the journal so the verifier knows it's intentional, not drift. The standing full-suite criterion (6) is what catches a regression here.

- **Circular-looking edge with #18, resolved (see HAZARD 3 above / criterion 3):** #18 depends on this ticket for `afterLogin()`; this ticket's own criterion 3 ("calls automatically right after login succeeds") can't be fully proven until #18's submit handler exists to DO the calling. Order is #12 first (criterion 3 carried here as BLOCKED-ON-18 with a reduced evidence bar), then #18, which re-checks the full behavior with the real form. Do not build any login-form UI here to try to close criterion 3 yourself — that is #18's owned region and its escalation trigger.

- **`afterLogin()` contract (must match exactly, #18 calls this and only this):** `window.afterLogin = async function () { /* 1. read current localStorage threads; 2. POST /threads/sync with {client_thread_id, title, messages} per thread; 3. THEN await hydratThreadsFromServer(); */ }`. Expose it as `window.afterLogin` (matching the existing `window.startAnswerStream` pattern at index.html ~L1160) — no further `window.*` exposure. #10's callback also 302s to `/?login=google`, which a full page load already runs `hydratThreadsFromServer()` for on its own (~L1458) — `afterLogin()` is for the same-page, no-reload case #18's form needs (and is what #10's redirect target should ALSO eventually call once #18 exists; that wiring is out of scope here).

- **OWNED REGIONS (exhaustive — loop.md check (a) diffs against exactly this list):**
  - `app/threads.py` — add `sync_threads()`, the `client_thread_id` column/index in `ensure_schema()`, the `ORDER BY` tiebreaker fix in `get_thread()`, and the new `ThreadSyncResult`/local-thread-in pydantic models. Do not change `create_thread`, `write_message`, `list_threads`, or `soft_delete_thread`'s signatures.
  - `app/main.py` — new `POST /threads/sync` route ONLY, between new markers `# region: threads-sync (#12)` / `# endregion: threads-sync`, placed near the existing `/threads` routes (~L894). No other route touched.
  - `app/templates/index.html` — new `window.afterLogin` function ONLY, between new markers `// region: after-login (#12)` / `// endregion: after-login`, inside the existing IIFE (must be defined after `hydratThreadsFromServer` so it can call it). Do not modify `hydratThreadsFromServer`, `loadState`/`saveState`, or any chat-turn code.
  - `tests/test_threads.py` — add `sync`-named tests; `test-12-*` fixtures; do not edit #11's existing tests.
  - `.loop/12/journal.md`.
  - NOT owned, still escalates: any other file, any other region of `main.py`/`index.html`, `pyproject.toml`, `config.py`, any table other than the one pre-authorized `threads` migration above.
