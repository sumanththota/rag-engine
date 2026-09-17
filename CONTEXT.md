# RAG Engine

A FastAPI + Postgres/pgvector RAG assistant over a university handbook, plus an evolving eval pipeline for reviewing and improving it.

See [CONVENTIONS.md](CONVENTIONS.md) for implementation conventions (call
stack, naming, error handling, code style) — kept separate since this file
is a glossary only.

## Language

**Trace**:
The recorded record of one chat turn captured for eval review: the question, its sequence of Steps, and its Annotation. Keyed by the same id minted per chat turn for log correlation.
_Avoid_: Thread, Run, Session, Request.

**Step**:
One stage of a Trace's pipeline (rewrite, retrieve, or generate), carrying its full structured payload (e.g. retrieve's chunks+scores, generate's prompt+output). Distinct from the log line `stage` field, which is a coarser ops-debugging tag that doesn't currently separate rewrite from retrieve.
_Avoid_: Stage.

**Annotation**:
A human-authored PASS/FAIL judgment with a free-text note and free-form tags, attached to a Trace during review.
_Avoid_: Label, Score, Rating.

**Thread**:
An end user's multi-turn chat conversation: a title plus an ordered list of messages. Distinct from a Trace — a Thread is the user-facing conversation; a Trace is one turn's dev-facing eval record. Anonymous users keep Threads client-side only (`localStorage`); logging in persists them server-side, keyed by user.
_Avoid_: Session, History, Chat.
