# Production readiness gaps

Brainstormed 2026-09-16 by reading `app/rag.py`, `app/store.py`, `app/chunk.py`,
`app/main.py` plus the project's `.md` docs. Scope: everything **except**
eval/observability, which is already tracked separately in
[docs/evals/intent.md](../evals/intent.md) (phases 2-6 of that roadmap) and is
being actively worked on. Nothing here is decided or scheduled — it's a
prioritized list to pull from later.

## P0 — production blockers

1. **No auth/access control** — `/chat/*`, `/docs/upload`, `/ingest`,
   `/traces`, `/dashboard` are all open. Anyone can upload documents, trigger
   LLM spend, or read trace data.
2. **Ingest blocks the HTTP request** — `POST /ingest`
   ([app/main.py:277](../../app/main.py)) synchronously chunks, embeds, and
   upserts inline. A large PDF holds the connection open for the full run,
   with no job queue, no progress reporting, and no retry on partial failure.
3. **No hybrid/keyword search** — retrieval is pure pgvector cosine distance
   ([app/store.py:67](../../app/store.py)). Exact terms (course codes, policy
   numbers, proper nouns) that embed poorly will be missed entirely.
4. **No reranker** — top-k is returned raw by cosine distance; no
   cross-encoder or LLM rerank pass to correct embedding recall/precision
   tradeoffs.
5. **No chunk overlap** — `chunk.build()` ([app/chunk.py:30](../../app/chunk.py))
   splits at hard 350-word boundaries with zero overlap. Answers whose
   supporting text spans a boundary lose context.

## P1 — real gaps, not urgent

6. **No document lifecycle** — no delete or re-ingest/versioning path. Chunk
   ids are assigned sequentially per ingest run, so re-ingesting a changed
   document likely duplicates or corrupts ids rather than cleanly replacing
   it.
7. **No rate limiting** on `/chat/*` or `/docs/upload`.
8. **Hardcoded single embedding model + dimension (384)** baked into the
   schema ([app/store.py:32](../../app/store.py)) — no multi-model support or
   migration path if the embedding model ever changes.
9. **No metadata filtering** beyond page number — no way to scope retrieval
   to a specific document, section, or date range.
10. **No multi-turn memory** — every chat turn is stateless; no handling for
    follow-up questions that reference prior turns.
11. **No citation/grounding enforcement** — the prompt includes `[Page N]`
    markers but nothing verifies the generated answer actually cites what it
    used, or stays within the retrieved context.

## P2 — nice-to-have at scale

12. No caching (repeat-query embeddings, LLM responses).
13. No cost/token tracking per model.
14. No retention/pruning policy (traces table, uploaded docs directory).
15. No multi-tenancy/namespacing — single global collection for all
    documents.

## Suggested first pick

Auth (1), async ingest (2), and hybrid search (3): auth because this is a
live open service, the other two because they're the actual ceiling on RAG
quality and scale today.
