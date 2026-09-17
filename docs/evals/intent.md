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

Implementation decisions (schema, capture point, write timing, routes, UI
pattern) are fully specified in
[phase1-spec.md](./phase1-spec.md#implementation-decisions) and its ADRs
([0001](../adr/0001-postgres-capture-not-loki.md),
[0002](../adr/0002-hybrid-trace-schema.md)) — not restated here, so the two
docs can't drift out of sync. Two methodology choices live here instead,
since they're about *why* phase 1 looks this way, not *what* gets built:

- **Annotation is binary PASS/FAIL + a free-text note**, not a 1-5 scale —
  the note *is* open coding (see "Why" above); PASS/FAIL is just a coarse
  filter for browsing later, not the signal itself.
- **Process weight: solo-dev context.** The user is the "benevolent
  dictator" — no multi-annotator/inter-annotator-agreement process needed.
  Keep phase 1 lightweight; don't build clustering/taxonomy tooling before
  there's any data to cluster.

## Explicitly out of scope for now

- Curated golden question/answer sets
- Automated scoring / LLM-as-judge
- Adopting a full framework (RAGAS, LangSmith, Langfuse)
- Multi-system comparison / experiment tracking

## Next step

Phase 1 (Capture + Review) has shipped: the `traces` table, capture
wiring in `event_stream()`, and the `/traces` review UI (filterable
thread list, trace detail with rewrite/retrieve/generate steps, and
PASS/FAIL annotation with notes and tags) are all live — see
[phase1-spec.md](./phase1-spec.md). Next is phase 2 (error analysis):
read ≥30 real traces yourself and start open coding per the roadmap
above. Phase 3 and beyond stay un-designed until phase 2's taxonomy says
what's actually worth building — see "Why" above on not skipping ahead.
