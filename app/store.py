import asyncpg
from pydantic import BaseModel

_DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError)


class StoreError(Exception):
    """Raised for any Postgres/pgvector failure: connection failure, schema
    setup failure, upsert failure, or search failure."""


class SearchResult(BaseModel):
    text: str
    page: int
    score: float


class PostgresStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def ensure_schema(self, collection: str) -> None:
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {collection} (
                        id bigint PRIMARY KEY,
                        text text NOT NULL,
                        page int NOT NULL,
                        embedding vector(384) NOT NULL
                    )
                    """
                )
                await conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS {collection}_embedding_hnsw_idx
                    ON {collection} USING hnsw (embedding vector_cosine_ops)
                    """
                )
        except _DB_ERRORS as e:
            raise StoreError(f"ensure_schema failed for {collection!r}: {e}") from e

    async def upsert(
        self, collection: str, id: int, vector: list[float], text: str, page: int
    ) -> None:
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    f"""
                    INSERT INTO {collection} (id, text, page, embedding)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (id) DO UPDATE
                    SET text = EXCLUDED.text,
                        page = EXCLUDED.page,
                        embedding = EXCLUDED.embedding
                    """,
                    id,
                    text,
                    page,
                    str(vector),
                )
        except _DB_ERRORS as e:
            raise StoreError(f"upsert failed for {collection!r} id={id}: {e}") from e

    async def search(
        self, collection: str, vector: list[float], limit: int
    ) -> list[SearchResult]:
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT text, page, 1 - (embedding <=> $1) AS score
                    FROM {collection}
                    ORDER BY embedding <=> $1
                    LIMIT $2
                    """,
                    str(vector),
                    limit,
                )
        except _DB_ERRORS as e:
            raise StoreError(f"search failed for {collection!r}: {e}") from e

        return [
            SearchResult(text=row["text"], page=row["page"], score=row["score"])
            for row in rows
        ]

    async def healthy(self) -> None:
        try:
            async with self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
        except _DB_ERRORS as e:
            raise StoreError(f"health check failed: {e}") from e
