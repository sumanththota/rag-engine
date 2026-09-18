---
id: "12"
title: "Migrate pre-login local Threads into the server on first login"
goal: "A user's anonymous localStorage Threads are uploaded and idempotently upserted into their server-side history right after login."
spec_link: "https://github.com/sumanththota/rag-engine/issues/12"   # immutable audit anchor
dag:
  depends_on: ["11"]   # needs threads/thread_messages tables and a working save/list path to upsert into
  blocks: []
acceptance_criteria:   # copied verbatim from the issue body; each must be able to FAIL
  - "POST /threads/sync accepts a batch of local Threads (id/title/messages) and upserts each into threads/thread_messages for the logged-in user"
  - "Logging in a second time with the same local Threads does not create duplicates (idempotent by Thread id)"
  - "Client calls this endpoint automatically right after login succeeds"
  - "A Thread created anonymously, then synced after login, appears identically in the server-side Thread list (same id, title, messages, in order)"
verifier_command: "pytest -q tests/test_threads.py -k sync"
escalation_triggers:
  - "schema/migration touches an existing table"
  - "the SAME criterion fails in 3 separate verify rounds"
  - "any edit outside app/threads.py or the agent's own main.py region"
budgets: { max_iterations: 8, max_verify_rounds: 3, max_tokens: 400000, wall_clock: "2h" }
model_routing: { implementer: "haiku", verifier: "opus", planner: "opus" }
state: "ready-for-agent"   # GitHub label is already set, but depends_on ["11"] is not yet merged —
                            # the orchestrator's readiness check (label AND depends_on merged) keeps
                            # this from actually being picked until #11 lands
branches: { impl: "impl/12-sync-threads-login", verify: "verify/12-sync-threads-login" }
---
## Context (progressive disclosure — links, not inlined bodies)
- No client-side sync-tracking by design: every login re-uploads everything; the upsert is a no-op for anything already stored.
- Hotspot files & owned regions: extends app/threads.py (from #11) — coordinate with #11's owned region; client-side login hook that triggers the upload (file not specified in the issue — implementer should locate the existing post-login client code and flag if ambiguous).
