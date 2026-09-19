# rag-engine conventions

Reference this before building a new feature, instead of assuming a pattern.
It documents what this codebase actually does today (verified against
`app/*.py`), not aspirational style. See [CONTEXT.md](CONTEXT.md) for domain
vocabulary — this file is implementation conventions, not a glossary.

## 1. Module map / call stack

| Module | Role |
|---|---|
| `app/config.py` | `BaseSettings` + `.env` loading. `ConfigError` on invalid config. |
| `app/chunk.py` | Pure function, no I/O: `build(inputs, words_per_chunk)` → chunk boundaries. |
| `app/embed.py` | `OllamaClient` — embeddings over HTTP. `EmbedError`. |
| `app/llm.py` | `OpenAICompatibleClient` — chat completion + streaming (OpenRouter/Groq). `LLMError`. |
| `app/llamaparse.py` | LlamaCloud PDF-parse client. `LlamaParseError`. |
| `app/pdfextract.py` | Wraps llamaparse + local pypdf fallback, with a page-text cache. `PdfExtractError`. |
| `app/store.py` | `PostgresStore` — pgvector upsert/search over asyncpg. `StoreError`. |
| `app/rag.py` | `RagService` — orchestrator: ingest, query-rewrite, retrieve, prompt build. `RagError`/`RewriteError`. |
| `app/logging_utils.py` | JSON log formatter + per-request `trace_id` contextvar. |
| `app/main.py` | FastAPI app: routes, SSE streaming, HTTP-layer error classification. |
| `app/cli/eval.py` | Offline batch-eval harness → `docs/experiments.jsonl`, viewable at `/dashboard`. |
| `app/cli/ingest.py` | CLI ingestion entrypoint. |

**Chat turn call stack**: `POST /chat/start` (mints `trace_id`, renders stub) →
`GET /chat/stream` SSE (`event_stream()`) → `RagService.build_prompt()` →
optional `_rewrite_query_with_llm()` → `embed_client.embed()` →
`store.search()` → prompt assembly → `client.stream_answer()` (streamed
tokens via SSE).

**Ingest call stack**: `RagService.ingest()` → `store.ensure_schema()` →
`extract_by_page()` (pdfextract) → `chunk.build()` → per-chunk
`embed_client.embed()` + `store.upsert()`.

## 2. Naming

- **Loggers** are namespaced `rag.<module>` (`rag.ingest`, `rag.retrieve`,
  `rag.pdfextract`) — matches ops grep patterns from the Go source, don't
  invent a new namespace.
- **Data shapes** are Pydantic `BaseModel` (`SearchResult`, `ChatMessage`,
  `PageText`, `Chunk`) — not `@dataclass`, except for small
  caller-constructed bundles with no validation need (e.g. `QueryRewriter`
  in `app/rag.py` uses `@dataclass`).
- **Private helpers**: leading underscore, module-level, snake_case
  (`_shrink_text`, `_cache_key`, `_rewrite_query_with_llm`).
- **Private constants**: leading underscore, `UPPER_SNAKE_CASE`
  (`_EMBED_MODEL`, `_POLL_INTERVAL_SECONDS`, `_CACHE_DIR`).
- **Public functions/classes**: no underscore; classes PascalCase, functions
  snake_case.

## 3. Error handling

- **One typed exception per module**, always a plain `Exception` subclass
  with a docstring naming what it covers (`EmbedError`, `StoreError`,
  `LLMError`, `RagError`, `PdfExtractError`, `LlamaParseError`,
  `ConfigError`). Not Go-style `(result, error)` tuples.
- Module-internal failures (e.g. Postgres errors in `store.py`) are caught
  narrowly against a module-level tuple of expected low-level exceptions
  (see `_DB_ERRORS` in `app/store.py`) and re-raised as that module's typed
  exception — don't let raw `asyncpg`/`httpx` exceptions escape a module.
- **HTTP boundary classification happens once**, in `app/main.py`'s
  `classify_error()`: it switches on exception *type* first (each module's
  typed exception), falls back to message-substring sniffing only for
  cross-cutting cases Go itself classified by string (timeout, rate limit).
  It returns an `AppError(code, user_message, retryable)` — raw exception
  text is never sent to the client.

## 4. HTTP clients

- Every outbound `httpx.AsyncClient` carries an **explicit timeout** — no
  client relies on the library default (`app/embed.py`: `timeout=60.0`;
  `app/llm.py`: separate connect/read/write/pool timeouts;
  `app/llamaparse.py`: per-call timeouts, plus an outer
  `asyncio.timeout(...)` around the whole poll loop).

## 5. Logging

- Structured JSON via `app/logging_utils.py`'s `JsonFormatter` — never
  `print()`. Pass request-specific data through `extra={...}`, not
  string-interpolated into the message, so it becomes its own JSON field.
- Every log call in a request path carries `extra={"stage": "<stage>"}`
  (`ingest` / `http` / `retrieve` / `generate`) — a coarse ops-debugging
  tag. `trace_id` is stamped automatically via the contextvar filter, never
  passed manually.
- Log *lengths/counts*, not raw content (`prompt_chars=%d`,
  `context_chunks=%d`) — never log full prompt text, chunk text, or
  generated output. (Phase 1 eval tracing captures full payloads
  separately, in Postgres, not via this logging path — see
  [docs/evals/intent.md](docs/evals/intent.md).)

## 6. Business-rule code

- Code ported verbatim from the Go source and called out in comments as a
  "business rule, not boilerplate" (the prompt template, the query-rewrite
  prompt, the embed retry/shrink algorithm) must **not** be "cleaned up" or
  refactored incidentally — treat exact wording/whitespace as load-bearing,
  not style to normalize.

## 7. Agent-loop testing

- When tickets are implemented in parallel (separate git worktrees against
  the one shared dev Postgres — see `tests/test_traces.py`'s rationale for
  hitting a real DB), any new hardcoded test literal (email, id) is
  prefixed with the issue number: `test-9-...`, `test-10-...`. Same
  convention as the existing tests, just namespaced, so two parallel
  suites never collide on a shared table's unique constraint (e.g.
  `users.email`).
- An implementing agent's own "tests pass" is not the acceptance signal.
  A ticket's acceptance criteria are re-checked by an independent run
  after the agent's worktree is done, not taken on the agent's report.
- Agents never boot the shared dev server on `:8080` — it is one process
  shared across every parallel worktree. Verify through `pytest` only,
  spinning up a throwaway test instance on an ephemeral port where a
  criterion actually needs a live server.
- A worktree carries tracked files only: `.worktreeinclude` copies `.env`
  in (gitignored files matching its patterns; tracked files are never
  duplicated), but `.venv` is never copied — its absolute paths break.
  Rebuild it in the worktree before running anything:
  `python -m venv .venv && .venv/bin/pip install -e ".[dev]"` (same as the
  README's main setup). `pytest -q` should be green on that freshly-built
  venv before the agent starts its own work.
- `pyproject.toml` now has a `[build-system]` table and `[tool.setuptools]`
  packages config (added during ticket #11, approved by a human — see
  `.loop/11/journal.md`). The `PYTHONPATH=.` workaround noted in earlier
  tickets' retros is no longer required going forward; existing tests that
  still set it are unaffected by the change.
- Every new test file gets its own setup/cleanup scoped to its own fixture
  prefix. Do not rely on another test's teardown or ordering.
  CHECK: run the new test file alone, and filtered with -k, before claiming
  done.
- No test's only meaningful assertion may sit behind a runtime conditional
  (`if extracted_value: assert ...`). If the value can be absent, that absence
  IS the failure case and must assert failure, not skip silently.
  CHECK: grep new tests for `if .*:\s*$` followed by an assert on the next line;
  flag for human read, since this needs judgment a grep alone can't finish.
- Every endpoint that writes to a resource scoped by ownership (thread_id, user_id,
  etc.) must verify the caller owns the target BEFORE writing — not just on read.
  CHECK: for any write route accepting a foreign-key-style id from the client, confirm
  an ownership check exists before the write, and a test supplies another user's id
  and asserts the write is rejected (403/404), not merely that reads are filtered.

## See also

- [CONTEXT.md](CONTEXT.md) — domain glossary (Trace, Step, Annotation).
- [docs/adr/](docs/adr/) — architecture decision records.
- [docs/evals/intent.md](docs/evals/intent.md) — eval pipeline plan.
- The sibling Go repo's `docs/MIGRATION.md` — the original Go→Python
  migration rulebook these conventions were derived from; useful history,
  not required reading for new features going forward.
