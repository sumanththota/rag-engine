"""TraceStore tests — Phase 1 ticket 2 (docs/evals/phase1-spec.md).

Per the spec's Testing Decisions, these hit the real dev Postgres at
localhost:5433 (.env.example's DATABASE_URL) rather than an unreachable
socket: a capture write only means something if it actually lands and
reads back correctly.
"""

import json

import asyncpg

from app.rag import RewriteOutcomeStatus
from app.store import SearchResult
from app.traces import GenerateStep, RetrieveStep, RewriteStep, TraceStore

_DSN = "postgresql://handbook:handbook@localhost:5433/handbook"


async def _pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn=_DSN, min_size=0, max_size=2)


async def test_ensure_schema_then_write_persists_all_three_step_types():
    pool = await _pool()
    store = TraceStore(pool)
    await store.ensure_schema()

    steps = [
        RewriteStep(
            status=RewriteOutcomeStatus.RAN,
            original_question="when is add drop?",
            rewritten_query="add/drop deadline procedure",
        ),
        RetrieveStep(
            results=[SearchResult(text="Add/drop ends week 2.", page=4, score=0.91)]
        ),
        GenerateStep(prompt="assembled prompt text", output="Add/drop ends in week 2."),
    ]

    try:
        await store.write("trace-abc123", "when is add drop?", steps)

        row = await pool.fetchrow(
            "SELECT question, status, note, tags, steps FROM traces WHERE trace_id = $1",
            "trace-abc123",
        )
        assert row is not None
        assert row["question"] == "when is add drop?"
        assert row["status"] is None
        assert row["note"] is None
        assert list(row["tags"]) == []

        persisted = json.loads(row["steps"])
        assert [s["type"] for s in persisted] == ["rewrite", "retrieve", "generate"]
        assert persisted[0]["status"] == "ran"
        assert persisted[0]["rewritten_query"] == "add/drop deadline procedure"
        assert persisted[1]["results"][0]["text"] == "Add/drop ends week 2."
        assert persisted[1]["results"][0]["page"] == 4
        assert persisted[2]["prompt"] == "assembled prompt text"
        assert persisted[2]["output"] == "Add/drop ends in week 2."
    finally:
        await pool.execute("DELETE FROM traces WHERE trace_id = $1", "trace-abc123")
        await pool.close()


async def test_write_on_conflict_updates_steps_but_never_clobbers_annotation():
    pool = await _pool()
    store = TraceStore(pool)
    await store.ensure_schema()

    trace_id = "trace-conflict-1"
    try:
        await store.write(
            trace_id,
            "first question",
            [GenerateStep(prompt="p1", output="o1")],
        )
        # Simulate a human annotation landing between two capture writes for
        # the same trace_id (shouldn't happen in practice, but the UPDATE
        # clause must still never touch status/note/tags).
        await pool.execute(
            "UPDATE traces SET status = 'PASS', note = 'looks right' WHERE trace_id = $1",
            trace_id,
        )

        await store.write(
            trace_id,
            "second question",
            [GenerateStep(prompt="p2", output="o2")],
        )

        row = await pool.fetchrow(
            "SELECT question, status, note, steps FROM traces WHERE trace_id = $1",
            trace_id,
        )
        assert row["question"] == "second question"
        assert row["status"] == "PASS"
        assert row["note"] == "looks right"
        persisted = json.loads(row["steps"])
        assert persisted[0]["prompt"] == "p2"
    finally:
        await pool.execute("DELETE FROM traces WHERE trace_id = $1", trace_id)
        await pool.close()
