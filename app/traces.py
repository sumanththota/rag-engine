"""Trace capture storage. Persists one Trace row per chat turn to Postgres
so a human can review it later — see docs/evals/phase1-spec.md,
docs/adr/0001-postgres-capture-not-loki.md, and
docs/adr/0002-hybrid-trace-schema.md for the shape/rationale.

Deliberately separate from app/rag.py: RagService stays trace-agnostic
(ADR-0001) — this module only knows how to store Steps that
app/main.py's event_stream() assembles from RagService's return values.
"""

import json
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

import asyncpg
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

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
    error: str | None = None


class GenerateStep(BaseModel):
    type: Literal["generate"] = "generate"
    prompt: str
    output: str
    error: str | None = None


TraceStep = RewriteStep | RetrieveStep | GenerateStep

# Discriminated on the `type` field so reads reconstruct the right Step
# subclass from the steps jsonb column — writes go through model_dump()
# (see TraceStore.write below) so this only matters for the read side.
_StepUnion = Annotated[RewriteStep | RetrieveStep | GenerateStep, Field(discriminator="type")]
_StepsAdapter: TypeAdapter[list[_StepUnion]] = TypeAdapter(list[_StepUnion])


class AnnotationStatus(str, Enum):
    """A human-authored PASS/FAIL judgment (CONTEXT.md's Annotation) —
    lives here, not in app/main.py, so every TraceStore.annotate() caller
    (not just the HTTP route) is bound by the same invariant, matching how
    RewriteOutcomeStatus governs RewriteStep."""

    PASS = "PASS"
    FAIL = "FAIL"


class TraceSummary(BaseModel):
    """One row of the GET /traces thread list — no steps payload, since the
    list view only needs enough to pick a Trace to open."""

    trace_id: str
    created_at: datetime
    question: str
    status: str | None = None


class TraceDetail(BaseModel):
    """Full Trace row for GET /traces/{trace_id}, including its ordered
    Steps and current Annotation (status/note/tags)."""

    trace_id: str
    created_at: datetime
    question: str
    status: str | None = None
    note: str | None = None
    tags: list[str] = []
    steps: list[TraceStep]


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

    async def list_recent(self, limit: int = 50) -> list[TraceSummary]:
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT trace_id, created_at, question, status
                    FROM traces
                    ORDER BY created_at DESC
                    LIMIT $1
                    """,
                    limit,
                )
        except _DB_ERRORS as e:
            raise TraceError(f"list_recent failed: {e}") from e
        try:
            return [TraceSummary(**dict(row)) for row in rows]
        except (ValidationError, TypeError) as e:
            raise TraceError(f"list_recent failed to parse rows: {e}") from e

    async def get(self, trace_id: str) -> TraceDetail | None:
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT trace_id, created_at, question, status, note, tags, steps
                    FROM traces
                    WHERE trace_id = $1
                    """,
                    trace_id,
                )
        except _DB_ERRORS as e:
            raise TraceError(f"get failed for trace_id={trace_id!r}: {e}") from e
        if row is None:
            return None
        try:
            steps = _StepsAdapter.validate_python(json.loads(row["steps"]))
            return TraceDetail(
                trace_id=row["trace_id"],
                created_at=row["created_at"],
                question=row["question"],
                status=row["status"],
                note=row["note"],
                tags=list(row["tags"]),
                steps=steps,
            )
        except (json.JSONDecodeError, ValidationError, TypeError) as e:
            raise TraceError(f"get failed to parse row for trace_id={trace_id!r}: {e}") from e

    async def annotate(
        self, trace_id: str, status: AnnotationStatus, note: str, tags: list[str]
    ) -> bool:
        """Plain UPDATE (spec story 18) — no insert-if-missing, since a Trace
        must already exist (written by a chat turn) before it can be
        annotated. Returns False if trace_id doesn't exist."""
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "UPDATE traces SET status = $2, note = $3, tags = $4 WHERE trace_id = $1",
                    trace_id,
                    status.value,
                    note,
                    tags,
                )
        except _DB_ERRORS as e:
            raise TraceError(f"annotate failed for trace_id={trace_id!r}: {e}") from e
        return result == "UPDATE 1"
