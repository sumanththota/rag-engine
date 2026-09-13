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
