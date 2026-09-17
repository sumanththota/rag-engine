# rag-engine

FastAPI + Postgres/pgvector RAG service — a Python/pgvector port of a Go RAG implementation, ported with the help of parallel coding agents as a deliberate exercise in agent-driven codebase migration.

## Status

In progress. See the sibling Go repo's `docs/MIGRATION.md` for the full migration rulebook this port follows: target architecture, coding-convention mappings (Go idiom → Python idiom), the Qdrant → Postgres/pgvector store mapping, and the dependency-ordered migration batches.

## Stack

- FastAPI, `httpx` for outbound HTTP (Ollama embeddings, OpenRouter/Groq chat)
- Postgres + `pgvector` for vector storage and similarity search
- SSE streaming for chat responses, matching the original HTMX frontend contract

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in API keys and DATABASE_URL
```

## Docs

Each doc below owns one thing — if you're about to restate content that
already lives elsewhere, link it instead.

| Doc | Owns |
|---|---|
| [CONTEXT.md](CONTEXT.md) | Domain glossary (Trace, Step, Annotation) — terms only, no implementation detail |
| [CONVENTIONS.md](CONVENTIONS.md) | Implementation conventions: module map, naming, error handling, logging |
| [docs/adr/](docs/adr/) | Architecture decision records — the canonical record of *why*, one decision per file |
| [docs/evals/intent.md](docs/evals/intent.md) | Eval pipeline: why it exists, the phase 1–6 roadmap, decisions unique to that roadmap |
| [docs/evals/phase1-spec.md](docs/evals/phase1-spec.md) | Phase 1 ("Capture + Review") spec: user stories, implementation decisions, testing decisions |
| [docs/roadmap/production-gaps.md](docs/roadmap/production-gaps.md) | Production-readiness gaps outside eval/observability scope |
