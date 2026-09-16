# Eval pipeline — intent

See [CONTEXT.md](../../CONTEXT.md) for this project's glossary (Trace,
Step, Annotation) — kept there, not restated here, so it doesn't drift out
of sync.

## Why

Development so far has been vibe-checking: change a prompt, try a few
questions, ship if it "looks good." That doesn't scale and doesn't catch
regressions. Both sources below make the same core argument: don't jump to
curated golden test sets or automated scoring — first get visibility into
what the agent actually does on real queries, by capturing and reviewing
traces. The failure modes that matter can only be discovered by reading
real data, not guessed in advance.

Sources read for this:
- [A pragmatic guide to LLM evals for devs](https://newsletter.pragmaticengineer.com/p/evals) — Gergely Orosz & Hamel Husain
- [AI Evals FAQ](https://hamel.dev/blog/posts/evals-faq/) — Hamel Husain & Shreya Shankar

## Current system (as of this doc)

- Stack: FastAPI (`app/main.py`) + Postgres/pgvector via asyncpg
  (`app/store.py`). No agentic tool-calling today.
- Pipeline per chat turn (`app/rag.py: RagService`): optional LLM
  query-rewrite/triage → embed → pgvector top-k search → prompt assembly →
  streamed LLM completion. In the NurtureBoss-style reference UI's terms,
  "tool calls" map onto our `rewrite` and `retrieve` steps — there's no
  real tool-calling yet, but the trace schema should stay open to add
  `tool_call`/`tool_response` step types later.
- `trace_id` correlation already exists (`app/logging_utils.py`): minted
  once per chat turn in `chat_start`, threaded through `chat_stream` via a
  query param, stamped on every log line underneath via a contextvar. New
  trace capture should key off this same id (as `thread_id`), not invent
  a new one.
- `app/cli/eval.py` → `docs/experiments.jsonl` already exists as an
  **offline batch-eval harness**: runs a fixed question list
  (`docs/eval_questions.txt`) through retrieval (+ optionally generation)
  and appends one JSON record per question, viewable at `/dashboard`. This
  is a different tool from what we're building (no live traffic, no
  annotation) but its chunk schema (`{text, page, score}`) is worth
  reusing for consistency.

## The pipeline (100-ft view)

Each phase is gated on the previous one actually existing — no skipping
ahead.

1. **Capture + Review** — trace schema (query, rewrite step, retrieve step
   with chunks+scores, generate step with prompt+output, empty
   `annotation`), stored in Postgres, browsable in a three-panel review UI
   (thread list / trace detail / annotation). Makes traces *readable*.
   Nothing is scored yet.
2. **Error analysis** (process, not code) — read ≥30 traces yourself,
   write free-text notes on the *first* thing that looks wrong per trace
   ("open coding"). Extend toward ~100 traces, with an agent suggesting
   similar cases after the first 30 for you to accept/reject. Group notes
   into 5-10 recurring themes ("axial coding"), count frequency per theme.
   This produces a failure taxonomy backed by real counts — it decides
   everything below.
3. **Retrieval evals** — only if the taxonomy says retrieval is the
   problem. IR metrics (Recall@k, Precision@k, MRR) against a
   query→relevant-chunk dataset, bootstrapped synthetically (extract facts
   from the handbook, generate a question per fact). Pure code, cheap to
   rerun.
4. **Generation evals** — only for failure modes that persist after
   obvious prompt fixes. Code-based assertions for deterministic failures
   (e.g. malformed citation format). Validated LLM-as-judge for subjective
   ones: hand-label 100-200 examples PASS/FAIL with a critique, split
   train/dev/test, iterate the judge prompt against dev, measure
   TPR/TNR on held-out test before trusting it.
5. **Repeatable eval set + CI** — confirmed failures from phases 2-4
   collected into one running set (100+ cases), rerun on meaningful
   prompt/model/retrieval-config changes.
6. **Flywheel** — periodically re-sample production traces (random +
   outliers, e.g. low retrieval score) and feed back into phase 2 every
   2-4 weeks or after incidents. The taxonomy evolves; evals evolve with
   it.

## Decisions confirmed for phase 1 (Capture + Review)

- **Storage**: Postgres table, same asyncpg pool the vector store already
  uses — not a JSONL file, and not derived from the local Loki/Grafana
  stack. Annotation writes are plain `UPDATE`s. See
  [ADR-0001](../adr/0001-postgres-capture-not-loki.md).
- **Trace schema shape**: flat columns for `trace_id` / `created_at` /
  `question` / the annotation fields (`status`, `note`, `tags`), plus one
  `steps jsonb` column for the pipeline (rewrite/retrieve/generate). See
  [ADR-0002](../adr/0002-hybrid-trace-schema.md).
- **Capture point**: inside `RagService` + `chat_stream`'s
  `event_stream()` in `app/main.py`, reusing the existing `trace_id`.
  `RagService` itself stays trace-agnostic — `build_prompt` widens its
  return value (a new small result type bundling the prompt, retrieval
  results, and rewrite outcome) instead of taking a trace-writer
  dependency; `event_stream()` assembles and writes the Trace.
- **Write timing**: fire-and-forget, via a `BackgroundTask` that runs
  after the SSE stream finishes — the same mechanism `chat_stream`
  already uses to reset `trace_id_var`. A slow or failed write never adds
  latency to, or breaks, the chat response.
- **Capture scope**: chat turns only (`/chat/stream`), not
  `/docs/upload` or `/ingest` — ingestion already has its own logging and
  the separate `app/cli/eval.py` batch harness.
- **Failure handling**: partial/failed turns are captured too, not
  dropped — steps and/or the trace carry an error field. These are
  exactly the highest-signal cases for phase 2's error analysis.
- **Rewrite step is always recorded**: even when no rewriter is
  configured, or the LLM rewrite call fails and falls back to the raw
  question, the step records *why* (ran / not configured / failed and
  fell back) — otherwise those two cases are indistinguishable in review.
- **Routes**: new `/traces` (thread list) + `/traces/{trace_id}` (detail
  + annotate), flat naming matching the app's existing route style
  (`/chat/start`, `/docs/upload`). Annotation write is a
  `POST /traces/{trace_id}/annotate`.
- **UI**: FastAPI + server-rendered HTML, matching the existing
  `index.html` / `dashboard.html` pattern in `app/templates/`. The
  trace-detail panel renders steps generically, dispatching on
  `step.type` — today that's just rewrite/retrieve/generate, but a
  future `tool_call`/`tool_response` step type (see "Current system"
  above — no real tool-calling exists yet) renders without a UI change
  once one is added.
- **Annotation shape**: binary PASS/FAIL (not a 1-5 scale) + a free-text
  note as the primary artifact (this *is* open coding) + free-form tags —
  no fixed taxonomy yet, since one doesn't exist until phase 2 produces
  it.
- **Process weight**: solo-dev context — the user is the "benevolent
  dictator," no multi-annotator/inter-annotator-agreement process needed.
  Keep phase 1 lightweight; don't build clustering/taxonomy tooling before
  there's any data to cluster.

## Explicitly out of scope for now

- Curated golden question/answer sets
- Automated scoring / LLM-as-judge
- Adopting a full framework (RAGAS, LangSmith, Langfuse)
- Multi-system comparison / experiment tracking

## Next step

Implement phase 1 (Capture + Review) per the decisions above: the
`traces` table, the widened `RagService` return value, capture wiring in
`event_stream()`, and the `/traces` review UI. Phase 2 (error analysis)
and beyond stay un-designed until phase 1 ships and produces real traces
to read — see "Why" above on not skipping ahead.
