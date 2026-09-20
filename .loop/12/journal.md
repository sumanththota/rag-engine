# Journal — Ticket #12 "Migrate pre-login local Threads into the server on first login"

## Round 4 — replace hollow comment/docstring-satisfiable checks with real-execution tests

**Start time: 2026-09-20 14:30 UTC**
**End time: 2026-09-20 15:15 UTC**

### Task
Replace two hollow text-search tests with genuine mutation-resistant implementations:
1. `test_after_login_defined_in_index_html` (criterion 3) — kept region-marker/definition checks, removed hollow ordering check
2. Added `test_after_login_real_execution_order` — executes window.afterLogin() via node with mocked localStorage/fetch/hydrate, asserts on OBSERVED call order
3. `test_get_thread_orders_messages_with_id_tiebreaker` (criterion 4) — replaced hollow text-search with real MVCC divergence test

### Implementation

#### Fix 1: test_after_login_real_execution_order
- Extracts the `// region: after-login (#12)` / `// endregion: after-login` block from index.html
- Wraps it with mocked `localStorage` (pre-populated with one test thread), `fetch`, and `hydratThreadsFromServer`
- Executes via `subprocess.run(["node", "-e", node_script])` 
- Tracks call order via push to `callOrder` array inside mocks
- Asserts that fetch('/threads/sync') is called BEFORE hydratThreadsFromServer()
- **Mutation test (FAILED as expected):** Swapped the fetch and hydrate calls in afterLogin → test correctly fails with "WRONG ORDER: hydrate called at position 0 but sync at 1"

#### Fix 2: test_get_thread_orders_messages_with_id_tiebreaker  
- Async test using test-12-* fixture pattern
- Creates a thread for a test user
- Manually inserts two thread_messages with IDENTICAL created_at via raw SQL (simulates same-transaction batch insert)
- Updates the first message after both exist (triggers Postgres MVCC/HOT behavior that can reorder physical heap rows)
- Calls thread_store.get_thread() and verifies message order matches insertion order (lower id first)
- **Mutation test (FAILED as expected):** Removed `, id ASC` from ORDER BY clause → test correctly fails with "First message must be msg1 (id=...) due to id ASC tiebreaker, but got id=..."

### Pass/Fail Evidence

**Fix 1 - test_after_login_real_execution_order:**
```
✓ PASS on correct code
✓ FAIL on mutation (call order swapped): "WRONG ORDER: hydrate called at position 0 but sync at 1. Call order: ['hydrate', 'fetch:/threads/sync']"
```

**Fix 2 - test_get_thread_orders_messages_with_id_tiebreaker:**
```
✓ PASS on correct code  
✓ FAIL on mutation (id ASC removed): "First message must be msg1 (id=...) due to id ASC tiebreaker, but got id=..."
```

### Full Suite Results
- test_threads.py: 16 tests, all pass (net +1 from original due to new test_after_login_real_execution_order)
- Full suite: **133 passed** (was 132 before round 4)
- No regressions in other tests

### Commits
All changes staged and ready to push to origin/impl/12-thread-sync
