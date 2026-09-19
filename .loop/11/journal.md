# Ticket #11: Persist logged-in users' Threads (round 2)

**Start: 2026-09-19T18:36:33Z**

Round 1 identified several issues:
1. chat_start creates new thread unconditionally, breaking conversation continuity
2. Missing HTTP routes for thread list/detail/delete
3. Frontend using only localStorage instead of real routes
4. Test isolation issues

Will address in priority order.
