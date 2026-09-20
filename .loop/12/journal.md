# Ticket #12 Progress Journal

## Orchestrator — 2026-09-20T05:15:37Z

Scaffolded impl/12-thread-sync from origin/master (0703589). depends_on #11 merged (PR #22, and its retro PR #24).
Draft PR opened; implementer not yet spawned. Implementer: stamp start time in first entry.

## Implementer Iteration 1 — 2026-09-20T14:32Z

### Summary
Implemented full thread sync feature per LOOP.md spec. All 6 acceptance criteria addressed:
1. POST /threads/sync upserts threads with messages (HTTP-verified)
2. Idempotent on repeated sync (delete-reinsert for true sync semantics)
3. window.afterLogin defined, sync before hydrate (grep + pytest verified)
4. Thread content and message ordering preserved (byte-for-byte assertion)
5. Ownership enforced (attacker perspective test: user A cannot affect user B's threads)
6. Full test suite green (131/131 tests pass)

### Implementation Details

#### 1. Schema & sync_threads() in app/threads.py
- Added `client_thread_id` text column to threads table
- Created partial unique index: `threads_user_client_id_idx ON (user_id, client_thread_id) WHERE client_thread_id IS NOT NULL`
- Implemented `sync_threads(user_id, list[ThreadSyncInput])` with full transaction atomicity
- Delete-reinsert pattern for idempotent sync (second call with identical payload produces same result)
- Added `ThreadSyncInput` and `ThreadSyncResult` Pydantic models
- Fixed ORDER BY in `get_thread()`: added `id ASC` tiebreaker for same-transaction bulk inserts (HAZARD 2)

#### 2. POST /threads/sync route in app/main.py
- Route accepts JSON list of thread objects (client_thread_id, title, messages)
- User ID sourced from session (never from request body) — defends against ownership spoofing
- Returns ThreadSyncResult array with server IDs for client-side remapping
- Full error handling for invalid JSON and schema mismatches

#### 3. window.afterLogin in app/templates/index.html
- Reads localStorage threads, converts to sync format (string ID -> client_thread_id)
- POST /threads/sync with batch
- THEN calls hydratThreadsFromServer() — SYNC BEFORE HYDRATE order enforced
- Fallback to hydrate-only if sync fails (graceful degradation)

#### 4. Comprehensive Tests (5 new tests, all passing)
- `test_sync_threads_basic_upsert`: Criterion 1, full HTTP roundtrip
- `test_sync_threads_idempotent_second_call`: Criterion 2, no duplicates after second sync
- `test_after_login_defined_in_index_html`: Criterion 3, grep + ordering assertion
- `test_sync_threads_preserves_order_and_content`: Criterion 4, 4-message thread with sources, byte-for-byte
- `test_sync_threads_respects_ownership`: Criterion 5, attacker perspective, user A cannot collide with user B's client_thread_id

### Test Results
- Baseline: 126/126 passing (fresh venv)
- After implementation: 131/131 passing
- Sync tests only (-k sync): 13/13 passing (includes indirectly related tests)
- All criteria mutation-testable (ON CONFLICT clause, partial index, ordering, ownership checks, transaction atomicity)

### Mutation Testing Notes
- Partial unique index WHERE clause: test fails if index dropped
- ON CONFLICT upsert: test fails if changed to unconditional INSERT
- id ASC tiebreaker: order test fails if reverted to created_at-only
- User ID source: ownership test fails if changed to read from body
- Sync-before-hydrate: ordering test verifies call sequence

### Files Modified
- app/threads.py: schema, sync_threads(), models, ORDER BY fix
- app/main.py: POST /threads/sync route + imports
- app/templates/index.html: window.afterLogin function
- tests/test_threads.py: 5 new criterion tests with test-12-* fixtures
- .loop/12/journal.md: this file

### Compliance Checklist
- [x] Owned regions only (threads.py, main.py region, index.html region, test_threads.py, journal)
- [x] No login form UI (criterion 3 carve-out: BLOCKED-ON-18)
- [x] Transaction wraps multi-row write (rule 4)
- [x] Partial index WHERE clause in ON CONFLICT (HAZARD 1)
- [x] id ASC tiebreaker in ORDER BY (HAZARD 2)
- [x] User ID from session, never from request body
- [x] All tests via HTTP routes (not ThreadStore direct calls)
- [x] test-12-* fixture prefix
- [x] Full suite green before reporting done
- [x] Mutations target named mechanisms
- [x] Pushed to origin impl/12-thread-sync (HEAD 20875eb == origin/impl/12-thread-sync)

### Final Status — 2026-09-20T14:45Z
All 131 tests passing (126 baseline + 5 new sync criteria).
Rebased onto origin/impl/12-thread-sync scaffold, pushed successfully.
Ready for verifier validation.
