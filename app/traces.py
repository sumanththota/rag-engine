"""Trace capture storage. Persists one Trace row per chat turn to Postgres
so a human can review it later — see docs/evals/phase1-spec.md,
docs/adr/0001-postgres-capture-not-loki.md, and
docs/adr/0002-hybrid-trace-schema.md for the shape/rationale.

Deliberately separate from app/rag.py: RagService stays trace-agnostic
(ADR-0001) — this module only knows how to store Steps that
app/main.py's event_stream() assembles from RagService's return values.
"""

import json
from typing import Literal

import asyncpg
from pydantic import BaseModel

from app.rag import RewriteOutcome
from app.store import SearchResult

_DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError)


class TraceError(Exception):
    """Raised for any Postgres failure while creating the traces schema or
    writing a Trace row."""


class RewriteStep(RewriteOutcome):
    """Same fields as RagService's RewriteOutcome (status/original_question/
    rewritten_query) plus the `type` discriminator — inherited rather than
    hand-copied, so the two stay in sync if RewriteOutcome ever changes."""

    type: Literal["rewrite"] = "rewrite"

    @classmethod
    def from_outcome(cls, outcome: RewriteOutcome) -> "RewriteStep":
        return cls(**outcome.model_dump())


class RetrieveStep(BaseModel):
    type: Literal["retrieve"] = "retrieve"
    results: list[SearchResult] = []


class GenerateStep(BaseModel):
    type: Literal["generate"] = "generate"
    prompt: str
    output: str


TraceStep = RewriteStep | RetrieveStep | GenerateStep


class TraceStore:
    """Postgres-backed writer for the `traces` table (hybrid schema per
    ADR-0002: flat trace_id/created_at/question/Annotation columns, plus
    one `steps jsonb` column). Same role as app/store.py's PostgresStore,
    but for eval capture rather than retrieval — a separate table, on
    purpose (see ADR-0001)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def ensure_schema(self) -> None:
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS traces (
                        trace_id text PRIMARY KEY,
                        created_at timestamptz NOT NULL DEFAULT now(),
                        question text NOT NULL,
                        status text,
                        note text,
                        tags text[] NOT NULL DEFAULT '{}',
                        steps jsonb NOT NULL
                    )
                    """
                )
        except _DB_ERRORS as e:
            raise TraceError(f"ensure_schema failed: {e}") from e

    async def write(self, trace_id: str, question: str, steps: list[TraceStep]) -> None:
        """Inserts one Trace row. ON CONFLICT only touches question/steps —
        a capture write must never clobber a human's prior Annotation.

        Serialization is inside the try/except too: callers (e.g.
        app/main.py's write_trace()) only need to catch TraceError — this
        method's contract is that nothing else escapes it."""
        try:
            payload = json.dumps([step.model_dump(mode="json") for step in steps])
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO traces (trace_id, question, steps)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (trace_id) DO UPDATE
                    SET question = EXCLUDED.question, steps = EXCLUDED.steps
                    """,
                    trace_id,
                    question,
                    payload,
                )
        except (*_DB_ERRORS, TypeError, ValueError) as e:
            raise TraceError(f"write failed for trace_id={trace_id!r}: {e}") from e
