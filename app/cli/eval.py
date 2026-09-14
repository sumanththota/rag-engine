"""Eval harness entrypoint. Ported from cmd/eval/main.go.

Usage: python -m app.cli.eval [--chunk-words N] [--top-k N] [--model MODEL_ID]
                               [--questions PATH] [--output PATH]
Config is env-var driven (via app.config.Settings / .env) for everything the
Go CLI also sourced from config.Load(); the five Go flags become argparse
options with matching defaults.
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from app.config import ConfigError, Settings, load_config
from app.embed import OllamaClient
from app.llm import LLMError, OpenAICompatibleClient
from app.rag import RagService
from app.store import PostgresStore, SearchResult

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger("rag.eval.cli")


@dataclass(frozen=True)
class _ModelDef:
    provider: str
    model: str
    env_key: str


_MODELS: dict[str, _ModelDef] = {
    "openrouter_nemotron": _ModelDef(
        "openrouter", "nvidia/nemotron-3-nano-30b-a3b:free", "OPENROUTER_API_KEY"
    ),
    "openrouter_llama4_scout": _ModelDef(
        "openrouter", "meta-llama/llama-4-scout-17b-16e-instruct", "OPENROUTER_API_KEY"
    ),
    "groq_llama31_8b": _ModelDef("groq", "llama-3.1-8b-instant", "GROQ_API_KEY"),
    "ollama_gemma4_26b": _ModelDef("ollama", "gemma4:26b", "OLLAMA_API_KEY"),
}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RAG eval harness")
    parser.add_argument("--chunk-words", type=int, default=180, help="words per chunk")
    parser.add_argument("--top-k", type=int, default=10, help="number of chunks to retrieve")
    parser.add_argument(
        "--model",
        default="groq_llama31_8b",
        help="LLM model ID for answer generation (empty to skip)",
    )
    parser.add_argument(
        "--questions", default="docs/eval_questions.txt", help="path to questions file"
    )
    parser.add_argument(
        "--output",
        default="docs/experiments.jsonl",
        help="path to append-only JSONL results file",
    )
    return parser.parse_args(argv)


def _load_questions(path: str) -> list[str]:
    lines = Path(path).read_text().splitlines()
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(stripped)
    return out


def _setup_llm(
    model_id: str, cfg: Settings
) -> tuple[OpenAICompatibleClient | None, str, str]:
    if not model_id:
        return None, "", ""

    md = _MODELS.get(model_id)
    if md is None:
        logger.warning("unknown model_id=%s — skipping LLM", model_id)
        return None, "", ""

    api_keys = {
        "OPENROUTER_API_KEY": cfg.openrouter_api_key,
        "GROQ_API_KEY": cfg.groq_api_key,
        "OLLAMA_API_KEY": cfg.ollama_api_key,
    }
    api_key = api_keys[md.env_key].strip()
    if not api_key and md.provider != "ollama":
        logger.warning("missing %s — skipping LLM", md.env_key)
        return None, "", ""

    if md.provider == "openrouter":
        client = OpenAICompatibleClient(
            "https://openrouter.ai/api/v1",
            {"HTTP-Referer": "http://localhost", "X-Title": "handbook-rag-eval"},
        )
    elif md.provider == "groq":
        client = OpenAICompatibleClient("https://api.groq.com/openai/v1")
    elif md.provider == "ollama":
        client = OpenAICompatibleClient(cfg.ollama_host.rstrip("/") + "/v1")
    else:
        logger.warning("unknown provider=%s", md.provider)
        return None, "", ""

    return client, md.model, api_key


async def _get_answer(
    client: OpenAICompatibleClient,
    api_key: str,
    model: str,
    question: str,
    results: list[SearchResult],
) -> str:
    context = "".join(f"[Page {r.page}]: {r.text}\n\n" for r in results)
    prompt = (
        f"Question:\n{question}\n\nContext:\n{context}"
        "Respond only from context and include page citations."
    )
    parts = []
    async for token in client.stream_answer(api_key, model, prompt):
        parts.append(token)
    return "".join(parts)


async def _run(argv: list[str]) -> int:
    args = _parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as err:
        logger.error("load config: %s", err)
        return 1

    collection = f"handbook_eval_w{args.chunk_words}_k{args.top_k}"
    logger.info(
        "chunk_words=%d top_k=%d collection=%s", args.chunk_words, args.top_k, collection
    )

    pool = await asyncpg.create_pool(cfg.database_url)
    try:
        svc = RagService(
            store=PostgresStore(pool),
            embed_client=OllamaClient(cfg.ollama_host),
            collection=collection,
            top_k=args.top_k,
            pdf_path=cfg.handbook_path,
            chunk_words=args.chunk_words,
        )

        logger.info("ingesting PDF into collection=%s ...", collection)
        try:
            count = await svc.ingest()
        except Exception as err:
            logger.error("ingest: %s", err)
            return 1
        logger.info("ingested chunks=%d", count)

        try:
            questions = _load_questions(args.questions)
        except OSError as err:
            logger.error("load questions from %s: %s", args.questions, err)
            return 1
        logger.info("loaded questions=%d", len(questions))

        llm_client, llm_model, llm_api_key = _setup_llm(args.model, cfg)

        with open(args.output, "a", encoding="utf-8") as out:
            for i, question in enumerate(questions, start=1):
                logger.info("[%d/%d] %s", i, len(questions), question)

                try:
                    results = await svc.retrieve(question)
                except Exception as err:
                    logger.error("retrieve failed: %s — skipping", err)
                    continue

                chunks = [
                    {"text": r.text, "page": r.page, "score": r.score} for r in results
                ]
                avg_score = sum(r.score for r in results) / len(results) if results else 0.0

                record = {
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "chunk_words": args.chunk_words,
                    "top_k": args.top_k,
                    "question": question,
                    "retrieved_chunks": chunks,
                    "avg_score": avg_score,
                }

                if llm_client is not None:
                    try:
                        answer = await _get_answer(
                            llm_client, llm_api_key, llm_model, question, results
                        )
                    except LLMError as err:
                        logger.error("LLM failed: %s", err)
                    else:
                        if answer:
                            record["answer"] = answer

                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                out.flush()
                logger.info("[%d/%d] done avg_score=%.4f", i, len(questions), avg_score)
    finally:
        await pool.close()

    logger.info("complete — results in %s", args.output)
    return 0


def main() -> None:
    sys.exit(asyncio.run(_run(sys.argv[1:])))


if __name__ == "__main__":
    main()
