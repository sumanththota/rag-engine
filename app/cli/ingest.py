"""Admin ingestion entrypoint. Ported from cmd/ingest/main.go.

Usage: python -m app.cli.ingest
Config is env-var driven only (via app.config.Settings / .env), matching the
Go CLI's zero-flag ergonomics — see internal/config/config.go.
"""

import asyncio
import logging
import sys

import asyncpg

from app.config import ConfigError, load_config
from app.embed import OllamaClient
from app.logging_utils import configure_logging
from app.rag import RagService
from app.store import PostgresStore

logger = logging.getLogger("rag.ingest.cli")


async def _run() -> int:
    configure_logging(service="rag-ingest-cli")

    try:
        cfg = load_config()
    except ConfigError as err:
        logger.error("load config: %s", err, extra={"stage": "boot"})
        return 1

    logger.info(
        "[ingest] start database_url=%s collection=%s handbook=%s ollama_host=%s",
        cfg.database_url,
        cfg.collection_name,
        cfg.handbook_path,
        cfg.ollama_host,
        extra={"stage": "ingest"},
    )

    pool = await asyncpg.create_pool(cfg.database_url)
    try:
        svc = RagService(
            store=PostgresStore(pool),
            embed_client=OllamaClient(cfg.ollama_host),
            collection=cfg.collection_name,
            top_k=cfg.top_k,
            pdf_path=cfg.handbook_path,
        )

        try:
            count = await svc.ingest()
        except Exception as err:
            logger.error("[ingest] failed: %s", err, extra={"stage": "ingest"})
            return 1
    finally:
        await pool.close()

    logger.info("[ingest] complete indexed_chunks=%d", count, extra={"stage": "ingest"})
    return 0


def main() -> None:
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
