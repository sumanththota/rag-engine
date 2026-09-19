# Ticket #12 Progress Journal

## Orchestrator — 2026-09-19T21:51:17Z

Scaffolded impl/12-sync-threads-login from origin/master (6f09ba3). depends_on #11 merged (PR #15/#16).
Criterion 3 is BLOCKED-ON-18 (see LOOP.md carve-out ruling). Implementer not yet spawned. Implementer: stamp start time in first entry.

## Implementer — 2026-09-19T22:10:00Z (start)

**Did:** Read LOOP.md, verified baseline (115 tests passing), examined existing code.

**Evidence:** 
- Baseline green: `.venv/bin/pytest -q` → 115 passed
- Owned regions identified: app/threads.py, tests/test_threads.py, app/main.py (lines 978-979), app/templates/index.html (afterLogin and ?login=google boot detection)

**Next:** Implement POST /threads/sync endpoint, add sync_threads method to ThreadStore, implement afterLogin() in index.html with boot-time ?login=google detection, add tests.

**Token estimate:** ~150k

## Implementer — 2026-09-19T22:15:00Z (iteration 1)

**Did:**
1. Added ClientThreadMessage and ClientThread BaseModel classes to app/threads.py for request validation
2. Added sync_threads method to ThreadStore: idempotent upsert by thread id (checks if thread exists, creates if missing; checks if message exists, creates if missing)
3. Added POST /threads/sync endpoint in main.py (lines 978-997) — requires auth, accepts list of ClientThread, returns {"synced": count}
4. Added afterLogin() function in index.html: calls POST /threads/sync with local threads, then calls hydratThreadsFromServer()
5. Updated boot code to detect ?login=google and call afterLogin() instead of calling hydrate directly
6. Exposed window.afterLogin = afterLogin for testability (TESTABILITY SEAM from LOOP.md)
7. Added 4 route-level tests: test_sync_threads_accepts_batch_and_upserts, test_sync_threads_idempotent_by_thread_id, test_sync_threads_anonymous_then_login, test_sync_threads_requires_login

**Evidence:**
- All sync tests pass individually and together: `.venv/bin/pytest tests/test_threads.py -k sync -v` → 13 passed
- Full suite: `.venv/bin/pytest -q` → 119 passed (115 baseline + 4 new sync tests)
- Owned regions only: no edits outside app/threads.py, tests/test_threads.py, app/main.py (lines 978-997), app/templates/index.html (afterLogin and ?login=google detection)
- Tests are route-level (use AsyncClient) and HTTP-falsifiable (exercise POST /threads/sync, GET /threads, GET /threads/{id} to verify data)

**Check (d) Level 1 — Static Grep Evidence:**

Searched in app/templates/index.html:

1. **afterLogin() calls /threads/sync** — Line 1471:
   ```javascript
   const syncResp = await fetch("/threads/sync", {
   ```
   
2. **Sync await/then chain completes BEFORE hydratThreadsFromServer() is invoked inside afterLogin()** — Line 1488 (after sync try/catch block):
   ```javascript
   // THEN hydrate from server (after sync completes)
   try {
     await hydratThreadsFromServer();
   ```
   Lines 1471-1488: fetch("/threads/sync") with await, then try/catch for sync, THEN await hydratThreadsFromServer() inside the same function. Sync is invoked at 1471-1481, hydrate at 1488.

3. **Boot code does NOT call hydratThreadsFromServer() independently ahead of afterLogin() on ?login=google** — Lines 1505-1516:
   ```javascript
   if (params.has("login") && params.get("login") === "google") {
     // OAuth callback: sync local threads then hydrate
     afterLogin().catch(function (err) {
       console.warn("afterLogin failed:", err);
     });
   } else {
     // Normal boot: hydrate if already logged in (for session persistence)
     hydratThreadsFromServer().catch(function (err) {
   ```
   On ?login=google (line 1506), boot calls afterLogin() (line 1508), NOT hydrate directly. Only on non-login loads (line 1511) does boot call hydrate. ✓

4. **Every auth path calls the one afterLogin()** — Currently implemented for ?login=google (line 1508). Password path (#18) must also call the same function. For testing without #18, window.afterLogin is exposed (line 1518) for console invocation.

5. **window.afterLogin = afterLogin exposed for testability** — Line 1518:
   ```javascript
   window.afterLogin = afterLogin;
   ```

**Next:** Run tests in isolation (single file, -k sync, reordered), then commit.

**Token estimate:** ~100k remaining
