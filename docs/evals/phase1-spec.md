# Phase 1: Capture + Review — Spec

See [CONTEXT.md](../../CONTEXT.md) for vocabulary (Trace, Step,
Annotation), [CONVENTIONS.md](../../CONVENTIONS.md) for implementation
conventions to follow, and [intent.md](./intent.md) for the full phase
1–6 roadmap this spec is scoped from.

## Problem Statement

Development on the RAG assistant has been vibe-checking: change a prompt,
try a few questions by hand, ship if it "looks good." There's no durable
record of what the assistant actually did on a given chat turn, and no way
to go back and review a specific turn — especially a failed one — after
the fact. Regressions and real failure modes go unnoticed because nothing
is captured to review them against.

## Solution

Every chat turn is captured as a Trace — the question, its Steps (rewrite,
retrieve, generate) with full payloads, and an empty Annotation — written
to Postgres. A review UI lists recent Traces and lets the developer open
one, read exactly what happened, and Annotate it (PASS/FAIL + free-text
note + free-form tags). This turns "vibe-checking" into a readable record
and is the prerequisite for Phase 2's error analysis — nothing is scored
or judged automatically in this phase.

## User Stories

1. As the developer, I want every chat turn automatically captured as a Trace, so that I don't have to remember to record anything manually.
2. As the developer, I want to see the original question and the rewritten query (if any) for a turn, so that I can tell whether query rewriting helped or hurt retrieval.
3. As the developer, I want to see which retrieved chunks (with scores and page numbers) were used to answer a question, so that I can judge whether retrieval found the right content.
4. As the developer, I want to see the exact prompt sent to the generation LLM and the exact output it produced, so that I can judge answer quality against real inputs, not summaries.
5. As the developer, I want turns that failed partway (rewrite error, empty retrieval, stream error) to still be captured with an error explanation, so that failures aren't invisible — they're the traces I most want to review.
6. As the developer, I want to distinguish "query rewriting was never configured" from "query rewriting was configured but the LLM call failed," so that I don't waste time debugging a feature that was simply switched off.
7. As the developer, I want a list of recent Traces (a thread list), so that I can browse chat activity without querying Postgres by hand.
8. As the developer, I want to open a single Trace's full detail (all its Steps), so that I can inspect exactly what happened on that turn.
9. As the developer, I want to mark a Trace PASS or FAIL, so that I start building a record of which turns are good vs. bad.
10. As the developer, I want to attach a free-text note to a Trace when I annotate it, so that I can record *why* I judged it that way for later reference.
11. As the developer, I want to attach free-form tags to a Trace, so that I can loosely group similar issues before any fixed taxonomy exists.
12. As the developer, I want Trace capture to never add latency to the chat response, so that reviewing traces doesn't cost the live user experience anything.
13. As the developer, I want a Postgres write failure during capture to never break the chat response itself, so that eval infrastructure failures don't become user-facing outages.
14. As the developer, I want Trace capture scoped to chat turns only, not document upload/ingest, so that Phase 1 stays minimal and ingest keeps using its existing logging and batch-eval harness.
15. As the developer, I want the Trace schema to stay open to future step types (e.g. `tool_call`/`tool_response`), so that adding real tool-calling later doesn't require a schema migration.
16. As the developer, I want the review UI's step renderer to be generic (dispatching on step type), so that a future step type appears without a UI rewrite.
17. As the developer, I want Traces correlated with the app's existing structured logs via the same `trace_id`, so that I can cross-reference a Trace with its Grafana/Loki logs for deeper ops-level debugging.
18. As the developer, I want annotating a Trace to be a single simple action (a plain update), so that reviewing dozens of traces in a sitting doesn't feel laborious.
19. As the developer, I want the new review routes to follow the app's existing flat route-naming style, so the codebase stays consistent and predictable.
20. As the developer, I want confidence that adding trace capture doesn't change the existing chat product's HTTP/SSE behavior, so that this eval feature can't regress the thing it's supposed to be observing.

## Implementation Decisions

- **New `traces` table**, hybrid schema (see
  [ADR-0002](../adr/0002-hybrid-trace-schema.md)): flat columns
  `trace_id` (PK), `created_at`, `question`, and the Annotation fields
  `status` / `note` / `tags`; one `steps jsonb` column holding the ordered
  rewrite/retrieve/generate Step sequence.
- **Direct Postgres writes**, not derived from the Loki/Grafana logging
  stack (see [ADR-0001](../adr/0001-postgres-capture-not-loki.md)) —
  existing structured logs record only lengths/counts, never full
  payloads, and are append-only so can't hold a mutable Annotation.
- **`RagService` stays trace-agnostic.** `build_prompt` widens its return
  value to a new small result type bundling the prompt, retrieval
  results, and the rewrite outcome (status: ran / not configured / failed
  and fell back — plus original question and rewritten query when
  applicable). `RagService` takes no storage dependency; `app/main.py`'s
  `event_stream()` assembles and writes the Trace from that return value
  plus the accumulated generation output.
- **Generation output must be buffered.** Tokens are streamed via SSE
  today with nothing accumulating the full answer text anywhere — capture
  requires buffering them into a string during `event_stream()` to store
  as the generate Step's output.
- **Write timing: fire-and-forget**, via the same `BackgroundTask`
  mechanism `chat_stream` already uses to reset `trace_id_var` — runs
  after the SSE stream fully finishes. A slow or failed write never adds
  latency to, or breaks, the chat response.
- **Failures are captured, not dropped.** Partial/failed turns still
  produce a Trace; Steps and/or the Trace itself carry an error field.
- **Rewrite Step is always recorded**, with a status distinguishing: ran
  normally, not configured, or configured-but-failed-and-fell-back —
  otherwise the latter two are indistinguishable on review.
- **Capture scope: chat turns only** (`/chat/stream`) — not
  `/docs/upload` or `/ingest`.
- **New routes**: `GET /traces` (thread list), `GET /traces/{trace_id}`
  (detail + annotation form), `POST /traces/{trace_id}/annotate` (writes
  `status`/`note`/`tags` as a plain `UPDATE`). Flat naming, matching
  `/chat/start`, `/docs/upload`.
- **UI**: FastAPI + server-rendered HTML, matching the existing
  `index.html` / `dashboard.html` pattern. The trace-detail panel renders
  Steps generically by dispatching on `step.type` — today that's just
  rewrite/retrieve/generate, but a future `tool_call`/`tool_response` type
  renders without a UI change (no real tool-calling exists yet).
- **`trace_id` is reused as-is** as the Trace's identity — no separate id
  minted for eval purposes. One Trace per chat turn; no multi-turn/thread
  grouping concept yet.
- Follow [CONVENTIONS.md](../../CONVENTIONS.md) throughout: Pydantic
  `BaseModel` for new data shapes, a typed exception per new module if one
  is added, existing logger namespacing and `stage` field left untouched,
  structured JSON logging conventions for any new log lines.

## Testing Decisions

- A good test here asserts external behavior only — HTTP status, SSE
  event payloads, rendered HTML content, Postgres row state as read back
  through the app's own routes — never internal function call counts.
- **Single seam**: the existing HTTP/ASGI seam from
  `tests/test_main.py` — `create_app()` exercised via
  `httpx.AsyncClient(transport=ASGITransport(...))`. Both capture and
  review are verified through this one seam: drive a real chat turn, then
  read the resulting Trace back via `GET /traces/{trace_id}` rather than
  querying Postgres directly in the test.
- **Test database**: reuse the dev Postgres at `localhost:5433` (per
  `.env.example`'s `DATABASE_URL`) rather than provisioning a separate
  test database. This is a deliberate deviation from most existing
  `test_main.py` tests, which point dependencies at unreachable sockets
  specifically to avoid needing a live DB — capture/review tests need a
  real, reachable one to verify a write actually landed and reads back
  correctly.
- **Prior art**: `tests/test_main.py`'s `_unreachable_rag_service()` /
  `_app_for()` / `_async_client()` / `_extract_sse_data()` helpers — new
  tests follow the same shape, plus a new helper that drives a full chat
  turn and fetches its Trace back via `/traces/{trace_id}`.
- Cover: capture happy path (steps populated correctly); each rewrite
  status (ran / not configured / failed-fallback) produces the right
  marker; failure paths (empty retrieval, stream error) still produce a
  Trace with an error field; the annotation round-trip
  (`POST .../annotate` then `GET` reflects it); the thread list surfaces
  recent Traces.
- `RagService`'s widened return value also gets direct unit-seam coverage
  in `tests/test_rag.py` (the existing `AsyncMock` pattern), asserting the
  rewrite-outcome status for each of the three rewrite scenarios.

## Out of Scope

- Curated golden QA sets, automated/LLM-judge scoring, retrieval evals
  (Recall@k/Precision@k/MRR), generation evals, adopting
  RAGAS/LangSmith/Langfuse, multi-system comparison — all explicitly
  deferred in [intent.md](./intent.md) to phases 3–5.
- Phase 2 (error analysis) itself — a human process, not code, gated on
  Phase 1 shipping and producing real traces to read.
- Real LLM tool-calling — the UI's step renderer is future-proofed for
  it, but no tool-calling capability is being built here.
- Trace capture for the `/docs/upload` or `/ingest` flows.
- Multi-turn/conversation-level grouping — one Trace per chat turn only.
- Authentication/access control on the new `/traces` routes — matches the
  rest of the app's current no-auth posture.
- A retention/pruning policy for the `traces` table — unbounded growth is
  accepted for now.
- Any change to the existing `stage` log field or the Loki/Grafana setup.

## Further Notes

- This spec covers Phase 1 only. Phase 2+ stay intentionally undesigned —
  see intent.md's "Why" section on not skipping ahead of what real trace
  data reveals.
- No issue-tracker connector is configured for this session, so this spec
  wasn't published/labeled `ready-for-agent`. Run `/setup-matt-pocock-skills`
  to enable that for future specs.
