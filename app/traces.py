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


class TraceStatusFilter(str, Enum):
    """Which Traces the Trace list shows, by Annotation status. The value is
    the `?status=` query parameter; unknown values parse as ALL."""

    ALL = "all"
    UNANNOTATED = "unannotated"
    PASS = "pass"
    FAIL = "fail"

    @classmethod
    def parse(cls, raw: str | None) -> "TraceStatusFilter":
        try:
            return cls((raw or "").strip().lower())
        except ValueError:
            return cls.ALL


class TraceFilter(BaseModel):
    """Which Traces the Trace list shows: an Annotation status, an exact
    Annotation tag and a case-insensitive question search, all of which
    must match. None means that part doesn't filter."""

    status: TraceStatusFilter = TraceStatusFilter.ALL
    tag: str | None = None
    q: str | None = None

    @classmethod
    def parse(cls, status: str | None, tag: str | None, q: str | None) -> "TraceFilter":
        """From raw query parameters: blank tag/q don't filter."""
        return cls(
            status=TraceStatusFilter.parse(status),
            tag=(tag or "").strip() or None,
            q=(q or "").strip() or None,
        )


# SQL predicate for a TraceFilter, bound as three params (status value, tag,
# q) starting at $first; shared by list_recent(), count() and neighbors() so
# Prev/Next stays inside the filtered list. strpos() over lower() rather than
# ILIKE, so % and _ in the search text match literally.
def _filter_sql(first: int) -> str:
    status, tag, q = (f"${first + i}" for i in range(3))
    return f"""
    CASE {status}::text
        WHEN 'unannotated' THEN status IS NULL
        WHEN 'pass' THEN status = 'PASS'
        WHEN 'fail' THEN status = 'FAIL'
        ELSE true
    END
    AND ({tag}::text IS NULL OR {tag}::text = ANY(tags))
    AND ({q}::text IS NULL OR strpos(lower(question), lower({q}::text)) > 0)
"""


def _filter_params(trace_filter: TraceFilter) -> tuple[str, str | None, str | None]:
    return trace_filter.status.value, trace_filter.tag, trace_filter.q


class TraceSummary(BaseModel):
    """One row of the GET /traces thread list — no steps payload, since the
    list view only needs enough to pick a Trace to open."""

    trace_id: str
    created_at: datetime
    question: str
    status: str | None = None
    tags: list[str] = []


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


class TraceCounts(BaseModel):
    """Table-wide Trace counts per Annotation status, for the Trace list's
    status filter bar."""

    total: int
    unannotated: int
    passed: int
    failed: int

    def for_filter(self, status_filter: TraceStatusFilter) -> int:
        return {
            TraceStatusFilter.ALL: self.total,
            TraceStatusFilter.UNANNOTATED: self.unannotated,
            TraceStatusFilter.PASS: self.passed,
            TraceStatusFilter.FAIL: self.failed,
        }[status_filter]


class TagCount(BaseModel):
    """One distinct Annotation tag and how many Traces carry it, for the
    Annotation panel's tag suggestions."""

    tag: str
    count: int


class TraceNeighbors(BaseModel):
    """The trace_ids adjacent to a Trace in thread-list order (created_at
    DESC), for the detail page's Prev/Next navigation. None at either end
    of the list, or when the current trace no longer matches the active
    TraceFilter (e.g. it was just annotated out of the unannotated view)."""

    prev_id: str | None = None
    next_id: str | None = None


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

    async def list_recent(
        self, limit: int = 50, offset: int = 0, trace_filter: TraceFilter | None = None
    ) -> list[TraceSummary]:
        trace_filter = trace_filter or TraceFilter()
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT trace_id, created_at, question, status, tags
                    FROM traces
                    WHERE {_filter_sql(3)}
                    ORDER BY created_at DESC, trace_id DESC
                    LIMIT $1 OFFSET $2
                    """,
                    limit,
                    offset,
                    *_filter_params(trace_filter),
                )
        except _DB_ERRORS as e:
            raise TraceError(f"list_recent failed: {e}") from e
        try:
            return [TraceSummary(**{**dict(row), "tags": list(row["tags"])}) for row in rows]
        except (ValidationError, TypeError) as e:
            raise TraceError(f"list_recent failed to parse rows: {e}") from e

    async def count(self, trace_filter: TraceFilter | None = None) -> int:
        """How many Traces match `trace_filter` — the Trace list's
        pagination total (unlike counts(), which is table-wide)."""
        trace_filter = trace_filter or TraceFilter()
        try:
            async with self._pool.acquire() as conn:
                return await conn.fetchval(
                    f"SELECT COUNT(*) FROM traces WHERE {_filter_sql(1)}",
                    *_filter_params(trace_filter),
                )
        except _DB_ERRORS as e:
            raise TraceError(f"count failed: {e}") from e

    async def counts(self) -> TraceCounts:
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(*) AS total,
                        COUNT(*) FILTER (WHERE status IS NULL) AS unannotated,
                        COUNT(*) FILTER (WHERE status = 'PASS') AS passed,
                        COUNT(*) FILTER (WHERE status = 'FAIL') AS failed
                    FROM traces
                    """
                )
        except _DB_ERRORS as e:
            raise TraceError(f"counts failed: {e}") from e
        return TraceCounts(**dict(row))

    async def tag_counts(self) -> list[TagCount]:
        """Distinct Annotation tags across the whole table, most-used first
        (ties alphabetical) — table-wide, not just the visible Trace list."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT tag, COUNT(DISTINCT trace_id) AS count
                    FROM traces, unnest(tags) AS tag
                    GROUP BY tag
                    ORDER BY count DESC, tag
                    """
                )
        except _DB_ERRORS as e:
            raise TraceError(f"tag_counts failed: {e}") from e
        return [TagCount(**dict(row)) for row in rows]

    async def neighbors(self, trace_id: str, trace_filter: TraceFilter | None = None) -> TraceNeighbors:
        """Prev/next trace_id in the same order as list_recent's WHERE/ORDER
        BY, so a filtered thread list's Next/Prev stays inside that filter."""
        trace_filter = trace_filter or TraceFilter()
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"""
                    SELECT prev_id, next_id FROM (
                        SELECT
                            trace_id,
                            LAG(trace_id) OVER (ORDER BY created_at DESC, trace_id DESC) AS prev_id,
                            LEAD(trace_id) OVER (ORDER BY created_at DESC, trace_id DESC) AS next_id
                        FROM traces
                        WHERE {_filter_sql(2)}
                    ) neighbors
                    WHERE trace_id = $1
                    """,
                    trace_id,
                    *_filter_params(trace_filter),
                )
        except _DB_ERRORS as e:
            raise TraceError(f"neighbors failed for trace_id={trace_id!r}: {e}") from e
        if row is None:
            return TraceNeighbors()
        return TraceNeighbors(prev_id=row["prev_id"], next_id=row["next_id"])

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
