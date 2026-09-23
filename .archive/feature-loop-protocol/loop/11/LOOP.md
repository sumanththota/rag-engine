---
id: "11"
title: "Persist logged-in users' Threads (save + reload)"
goal: "A logged-in user's Thread is written to Postgres turn-by-turn and reloads on refresh or another device; anonymous chat is unchanged."
spec_link: "https://github.com/sumanththota/rag-engine/issues/11"   # immutable audit anchor
dag:
  depends_on: ["9"]     # needs a resolvable logged-in user via get_current_user_optional
  blocks: ["12"]        # migrate pre-login local Threads on first login
acceptance_criteria:    # copied verbatim from the issue body; each must be able to FAIL
  - text: "Logging in, chatting, then refreshing (or opening on another device) shows the same conversation"
    verify_via: "HTTP — real request to /chat/start with a known thread_id, then a read-back route; a ThreadStore-direct test cannot satisfy this"
  - text: "Each chat turn writes one thread_messages row per side (user + assistant), including sources"
    verify_via: "HTTP — drive /chat/start + /chat/stream through a test client, then confirm the thread_messages rows; calling ThreadStore directly does not exercise chat_stream's write path"
  - text: "Clicking the existing delete icon on a Thread sets deleted_at server-side (soft-delete); the Thread no longer appears in the list"
    verify_via: "HTTP — DELETE route, then GET the list route and confirm absence"
  - text: "A user only ever sees their own Threads in the list and detail views"
    verify_via: "HTTP — list and detail routes as two different logged-in users"
  - text: "Fetching another user's Thread id directly (e.g. by guessing/incrementing) returns 404/403, not their data"
    verify_via: "HTTP — a Python None return from a store method is not a status code"
  - text: "Anonymous chat is unaffected — no server write, no regression"
    verify_via: "HTTP — anonymous chat through the routes, then confirm no thread rows were written"
verifier_command: "pytest -q tests/test_threads.py"
escalation_triggers:
  - "schema/migration touches an existing table"   # threads/thread_messages are new tables; watch for edits to existing ones
  - "the SAME criterion fails in 3 separate verify rounds"
  - "any edit outside app/threads.py, app/templates/index.html, or the agent's own main.py region"
budgets: { max_iterations: 8, max_verify_rounds: 3, max_tokens: 400000, wall_clock: "2h" }
model_routing: { implementer: "haiku", verifier: "opus", planner: "opus" }
state: "ready-for-agent"   # GitHub label is already set, but depends_on ["9"] is not yet merged —
                            # the orchestrator's readiness check (label AND depends_on merged) keeps
                            # this from actually being picked until #9 lands
branches: { impl: "impl/11-persist-threads", verify: "verify/11-persist-threads" }
---
## Context (progressive disclosure — links, not inlined bodies)
- Cross-reference: Thread glossary entry in CONTEXT.md — distinguishes Thread from the existing, unrelated Trace table; the two stay unlinked by design (ADR-0003).
- Convention refs: CONVENTIONS.md §1 module map (add app/threads.py row), §7 (agent-loop testing, once added)
- Hotspot files & owned regions: new module app/threads.py (own ensure_schema(), one typed error, matching PostgresStore/TraceStore convention); app/templates/index.html (update the now-false "saved only in this browser" copy); thread_id wiring through GET /chat/start -> GET /chat/stream, same pattern as the existing trace_id param; write into thread_messages inside chat_stream's event_stream() at the same point Trace capture already writes to traces — two independent writes, not a shared write path.
- The client and server threading state have not been connected across any prior pass.
  Own the full round-trip explicitly, in this order, and treat it as ONE unit of work,
  not four separate criteria:
    1. /chat/start returns thread_id in its response.
    2. index.html stores that thread_id and sends it back on the next turn to the SAME
       endpoint — this is the field that has been missing both previous passes.
    3. On page load, call GET /threads and hydrate the client thread list from the
       server response, replacing localStorage as the source of truth for logged-in users.
    4. The delete icon calls DELETE against the server-issued numeric id, not a local id.
  Do not consider any of criteria 1-5 addressable independently. If thread_id doesn't
  round-trip, criteria 1, 3, 4, and 5 are all still failing regardless of what else
  is built.
