"""Thread persistence for logged-in users. Persists multi-turn chat conversations
to Postgres, keyed by user_id. Anonymous users keep Threads client-side only.

Same module shape as app/store.py's PostgresStore and app/traces.py's TraceStore:
one typed exception (ThreadsError), an ensure_schema(), Postgres errors caught
narrowly and re-raised as that typed exception.
"""

import json
from datetime import datetime
from typing import Literal

import asyncpg
from pydantic import BaseModel

_DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError)
_THREAD_ID_MAX = 9223372036854775807  # Postgres bigint max (2^63 - 1)


def _is_valid_thread_id(thread_id: int) -> bool:
    """True if thread_id fits Postgres's bigint range. Every route that accepts
    a client-supplied thread_id must check this before querying the database
    with it — an out-of-range value raises a DB-level error, not a graceful miss.

    Args:
        thread_id: The thread ID to validate.

    Returns:
        True if thread_id is in valid Postgres bigint range (0, _THREAD_ID_MAX],
        False otherwise."""
    return 0 < thread_id <= _THREAD_ID_MAX


class ThreadsError(Exception):
    """Raised for any Postgres failure while creating the threads schema or
    reading/writing a thread or thread_message row."""


class ThreadMessage(BaseModel):
    """One message in a Thread conversation."""

    id: int
    role: Literal["user", "assistant"]
    content: str
    sources: list[dict] | None = None
    created_at: datetime


class Thread(BaseModel):
    """A logged-in user's multi-turn conversation."""

    id: int
    user_id: int
    title: str | None = None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class ThreadDetail(BaseModel):
    """Full Thread row with its ordered messages."""

    id: int
    user_id: int
    title: str | None = None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    messages: list[ThreadMessage]


class ThreadSyncInput(BaseModel):
    """One local Thread to sync to the server."""

    client_thread_id: str
    title: str | None = None
    messages: list[dict] | None = None


class ThreadSyncResult(BaseModel):
    """Result of syncing a single local Thread to the server."""

    client_thread_id: str
    id: int


class ThreadStore:
    """Postgres-backed writer for the `threads` and `thread_messages` tables.
    Persists logged-in users' multi-turn conversations."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def ensure_schema(self) -> None:
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS threads (
                        id bigserial PRIMARY KEY,
                        user_id bigint NOT NULL REFERENCES users(id),
                        title text,
                        created_at timestamptz NOT NULL DEFAULT now(),
                        updated_at timestamptz NOT NULL DEFAULT now(),
                        deleted_at timestamptz
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS threads_user_id_idx
                    ON threads (user_id)
                    WHERE deleted_at IS NULL
                    """
                )
                await conn.execute(
                    """
                    ALTER TABLE threads ADD COLUMN IF NOT EXISTS client_thread_id text
                    """
                )
                await conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS threads_user_client_id_idx
                    ON threads (user_id, client_thread_id) WHERE client_thread_id IS NOT NULL
                    """
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS thread_messages (
                        id bigserial PRIMARY KEY,
                        thread_id bigint NOT NULL REFERENCES threads(id),
                        role text NOT NULL,
                        content text NOT NULL,
                        sources jsonb,
                        created_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS thread_messages_thread_id_idx
                    ON thread_messages (thread_id)
                    """
                )
        except _DB_ERRORS as e:
            raise ThreadsError(f"ensure_schema failed: {e}") from e

    async def create_thread(self, user_id: int, title: str | None = None) -> int:
        """Creates a new thread for a user. Returns the thread_id."""
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO threads (user_id, title)
                    VALUES ($1, $2)
                    RETURNING id
                    """,
                    user_id,
                    title,
                )
        except _DB_ERRORS as e:
            raise ThreadsError(f"create_thread failed for user_id={user_id}: {e}") from e
        return row["id"]

    async def write_message(
        self,
        thread_id: int,
        role: Literal["user", "assistant"],
        content: str,
        sources: list[dict] | None = None,
        user_id: int | None = None,
    ) -> int:
        """Writes a message to a thread. Returns the message_id.
        If user_id is provided, ensures the write only succeeds if the thread
        exists, belongs to the user, and is not soft-deleted (defense-in-depth)."""
        try:
            sources_json = json.dumps(sources) if sources else None
            async with self._pool.acquire() as conn:
                # Build the WHERE clause for validation
                if user_id is not None:
                    # Defense-in-depth: validate ownership and soft-delete status at SQL level
                    where_clause = "WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL"
                    check_result = await conn.fetchrow(
                        f"SELECT id FROM threads {where_clause}",
                        thread_id,
                        user_id,
                    )
                    if check_result is None:
                        raise ThreadsError(
                            f"write_message: thread {thread_id} not found, owned by user {user_id}, or is soft-deleted"
                        )

                row = await conn.fetchrow(
                    """
                    INSERT INTO thread_messages (thread_id, role, content, sources)
                    VALUES ($1, $2, $3, $4)
                    RETURNING id
                    """,
                    thread_id,
                    role,
                    content,
                    sources_json,
                )
                # Update the thread's updated_at timestamp
                await conn.execute(
                    "UPDATE threads SET updated_at = now() WHERE id = $1",
                    thread_id,
                )
        except _DB_ERRORS as e:
            raise ThreadsError(
                f"write_message failed for thread_id={thread_id}: {e}"
            ) from e
        return row["id"]

    async def get_thread(self, thread_id: int, user_id: int) -> ThreadDetail | None:
        """Fetches a thread and its messages, validating ownership by user_id.
        Returns None if the thread doesn't exist or belongs to a different user."""
        try:
            async with self._pool.acquire() as conn:
                # Fetch thread (soft-delete check: don't return deleted threads)
                thread_row = await conn.fetchrow(
                    """
                    SELECT id, user_id, title, created_at, updated_at, deleted_at
                    FROM threads
                    WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                    """,
                    thread_id,
                    user_id,
                )
                if thread_row is None:
                    return None

                # Fetch messages in order (HAZARD 2: id ASC tiebreaker for same-transaction bulk inserts)
                message_rows = await conn.fetch(
                    """
                    SELECT id, role, content, sources, created_at
                    FROM thread_messages
                    WHERE thread_id = $1
                    ORDER BY created_at ASC, id ASC
                    """,
                    thread_id,
                )
        except _DB_ERRORS as e:
            raise ThreadsError(f"get_thread failed for thread_id={thread_id}: {e}") from e

        messages = []
        for row in message_rows:
            sources = None
            if row["sources"]:
                try:
                    sources = json.loads(row["sources"])
                except (json.JSONDecodeError, ValueError):
                    sources = None
            messages.append(
                ThreadMessage(
                    id=row["id"],
                    role=row["role"],
                    content=row["content"],
                    sources=sources,
                    created_at=row["created_at"],
                )
            )

        return ThreadDetail(
            id=thread_row["id"],
            user_id=thread_row["user_id"],
            title=thread_row["title"],
            created_at=thread_row["created_at"],
            updated_at=thread_row["updated_at"],
            deleted_at=thread_row["deleted_at"],
            messages=messages,
        )

    async def list_threads(self, user_id: int, limit: int = 50) -> list[Thread]:
        """Lists a user's threads, excluding deleted ones, ordered by most recent."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, user_id, title, created_at, updated_at, deleted_at
                    FROM threads
                    WHERE user_id = $1 AND deleted_at IS NULL
                    ORDER BY updated_at DESC, id DESC
                    LIMIT $2
                    """,
                    user_id,
                    limit,
                )
        except _DB_ERRORS as e:
            raise ThreadsError(f"list_threads failed for user_id={user_id}: {e}") from e

        return [
            Thread(
                id=row["id"],
                user_id=row["user_id"],
                title=row["title"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                deleted_at=row["deleted_at"],
            )
            for row in rows
        ]

    async def soft_delete_thread(self, thread_id: int, user_id: int) -> bool:
        """Soft-deletes a thread (sets deleted_at). Returns True if the thread existed
        and was deleted, False if it didn't exist or belongs to a different user."""
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE threads
                    SET deleted_at = now()
                    WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                    """,
                    thread_id,
                    user_id,
                )
        except _DB_ERRORS as e:
            raise ThreadsError(
                f"soft_delete_thread failed for thread_id={thread_id}: {e}"
            ) from e
        return result == "UPDATE 1"

    async def sync_threads(self, user_id: int, threads_to_sync: list[ThreadSyncInput]) -> list[ThreadSyncResult]:
        """Syncs a batch of local Threads to the server (upsert by user_id + client_thread_id).

        Returns a list of ThreadSyncResult with server IDs for each synced thread.
        Wraps the entire operation in a transaction for atomicity — if any message
        fails mid-batch, nothing partial is left behind (rule 4, HAZARD 1).
        For true idempotence, deletes old messages before reinserting on upsert.
        """
        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    results = []
                    for thread_input in threads_to_sync:
                        # Upsert thread: ON CONFLICT (user_id, client_thread_id) WHERE client_thread_id IS NOT NULL
                        # HAZARD (Postgres partial-index inference): must repeat the WHERE predicate
                        thread_row = await conn.fetchrow(
                            """
                            INSERT INTO threads (user_id, client_thread_id, title)
                            VALUES ($1, $2, $3)
                            ON CONFLICT (user_id, client_thread_id) WHERE client_thread_id IS NOT NULL
                            DO UPDATE SET title = EXCLUDED.title, updated_at = now()
                            RETURNING id
                            """,
                            user_id,
                            thread_input.client_thread_id,
                            thread_input.title,
                        )
                        server_thread_id = thread_row["id"]

                        # For idempotent sync: delete existing messages and reinsert.
                        # This ensures second call with identical payload doesn't duplicate.
                        await conn.execute(
                            "DELETE FROM thread_messages WHERE thread_id = $1",
                            server_thread_id,
                        )

                        # Insert messages for this thread (if any)
                        if thread_input.messages:
                            for msg in thread_input.messages:
                                role = msg.get("role", "user")
                                content = msg.get("content", "")
                                sources = msg.get("sources")
                                sources_json = json.dumps(sources) if sources else None

                                await conn.execute(
                                    """
                                    INSERT INTO thread_messages (thread_id, role, content, sources)
                                    VALUES ($1, $2, $3, $4)
                                    """,
                                    server_thread_id,
                                    role,
                                    content,
                                    sources_json,
                                )

                        results.append(
                            ThreadSyncResult(
                                client_thread_id=thread_input.client_thread_id,
                                id=server_thread_id,
                            )
                        )

                    return results
        except _DB_ERRORS as e:
            raise ThreadsError(f"sync_threads failed for user_id={user_id}: {e}") from e
