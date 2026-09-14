"""FastAPI HTTP + SSE server. Ported from cmd/server/main.go and dashboard.go.

Routes, status codes, and the SSE event contract (`sources`, `token`,
`streamerror`, `done`) are reproduced exactly so the existing HTMX frontend
(app/templates/index.html, unchanged from cmd/server/index.html) keeps
working — see docs/ui-execution-tracker.md for the contract this must not
break.

Dependencies (rag service, provider clients, store, embed client) are passed
into `create_app()` rather than constructed at import time, mirroring Go's
handleXxx(svc)(w, r) closures — this keeps the module importable (and the
route logic directly testable) without a live Postgres/Ollama available.
Production wiring lives in `bootstrap()`, used only when run as a script.
"""

import asyncio
import base64
import html
import json
import logging
import os
import random
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from app.config import Settings, load_config
from app.embed import OllamaClient
from app.llm import OpenAICompatibleClient
from app.rag import RagService
from app.store import PostgresStore

logger = logging.getLogger("server")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_INDEX_HTML = (_TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
_DASHBOARD_HTML = (_TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")

_DEFAULT_EXPERIMENTS_PATH = "docs/experiments.jsonl"
_DEFAULT_DOCS_DIR = "docs"
_UPLOAD_MAX_BYTES = 32 << 20
_UPLOAD_ALLOWED_EXT = {".pdf", ".md", ".txt", ".html", ".htm"}
_MAX_SOURCE_RUNES = 220


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    env_key: str


MODEL_CONFIGS: dict[str, ModelConfig] = {
    "openrouter_nemotron": ModelConfig("openrouter", "nvidia/nemotron-3-nano-30b-a3b:free", "OPENROUTER_API_KEY"),
    "openrouter_llama4_scout": ModelConfig("openrouter", "meta-llama/llama-4-scout-17b-16e-instruct", "OPENROUTER_API_KEY"),
    "groq_llama31_8b": ModelConfig("groq", "llama-3.1-8b-instant", "GROQ_API_KEY"),
    "ollama_gemma4_26b": ModelConfig("ollama", "gemma4:26b", "OLLAMA_API_KEY"),
}


@dataclass(frozen=True)
class AppError:
    code: str
    user_message: str
    retryable: bool


def classify_error(err: Exception) -> AppError:
    """Equivalent of Go's classifyError. Go string-sniffs a generic wrapped
    error; here the ported modules (app/embed.py, app/store.py, app/llm.py)
    already raise typed exceptions per failure domain, so we classify by
    type first and fall back to message substrings only where Go's own
    classification is itself message-based (timeout, rate limiting)."""
    msg = str(err).strip().lower()

    if isinstance(err, (TimeoutError, asyncio.TimeoutError)) or "timeout" in msg or "deadline exceeded" in msg:
        return AppError("timeout", "Request timed out while contacting search services. Please try again.", True)

    from app.embed import EmbedError
    from app.store import StoreError
    from app.llm import LLMError
    from app.rag import RagError

    if isinstance(err, EmbedError):
        return AppError(
            "dependency_unavailable",
            "Search is temporarily unavailable because the retrieval service is offline. Please try again shortly.",
            True,
        )
    if isinstance(err, StoreError):
        return AppError(
            "dependency_unavailable",
            "Search index is temporarily unavailable. Please try again in a moment.",
            True,
        )
    if "status 429" in msg or "rate limit" in msg:
        return AppError("rate_limited", "The model is receiving too many requests right now. Please retry shortly.", True)
    if isinstance(err, RagError) or "no context found" in msg:
        return AppError(
            "no_context",
            "I couldn't find matching handbook content for that question. Try a more specific academic or policy query.",
            True,
        )
    if isinstance(err, LLMError):
        return AppError("internal", "Something went wrong while generating the answer. Please try again.", True)
    return AppError("internal", "Something went wrong while generating the answer. Please try again.", True)


def encode_stream_error_payload(app_err: AppError) -> str:
    payload = json.dumps({"code": app_err.code, "message": app_err.user_message}).encode("utf-8")
    return base64.b64encode(payload).decode("ascii")


def new_req_id() -> str:
    return f"{random.getrandbits(32):08x}"


def create_app(
    *,
    rag_service: RagService,
    provider_clients: dict[str, OpenAICompatibleClient],
    api_keys: dict[str, str],
    embed_client: OllamaClient,
    store: PostgresStore,
    settings: Settings | None = None,
    experiments_path: str = _DEFAULT_EXPERIMENTS_PATH,
    docs_dir: str = _DEFAULT_DOCS_DIR,
) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings

    # ---- health ----------------------------------------------------------

    @app.get("/health/live")
    async def health_live() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/health/ready")
    async def health_ready() -> JSONResponse:
        deps = {"ollama": "ok", "postgres": "ok"}
        overall = "ok"

        try:
            async with asyncio.timeout(2):
                await embed_client.healthy()
        except Exception:
            deps["ollama"] = "unavailable"
            overall = "degraded"

        try:
            async with asyncio.timeout(2):
                await store.healthy()
        except Exception:
            deps["postgres"] = "unavailable"
            overall = "degraded"

        if settings is not None and (settings.openrouter_api_key.strip() or settings.groq_api_key.strip()):
            deps["llm_provider"] = "configured"
        else:
            deps["llm_provider"] = "ollama_local"

        status_code = 200 if overall == "ok" else 503
        return JSONResponse({"status": overall, "dependencies": deps}, status_code=status_code)

    # ---- index / dashboard -------------------------------------------------

    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse(_INDEX_HTML)

    @app.get("/dashboard")
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(_DASHBOARD_HTML)

    @app.get("/dashboard/data")
    async def dashboard_data() -> JSONResponse:
        path = Path(experiments_path)
        if not path.is_file():
            return PlainTextResponse("experiments file not found — run eval first", status_code=404)

        records = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return JSONResponse(records)

    # ---- docs upload ---------------------------------------------------

    @app.post("/docs/upload")
    async def upload_doc(request: Request) -> JSONResponse:
        req_id = new_req_id()
        try:
            form = await request.form()
        except Exception as err:
            logger.info("[http][%s] upload parse form: %s", req_id, err)
            return JSONResponse({"error": "invalid or too large upload"}, status_code=400)

        upload = form.get("file")
        if upload is None or not hasattr(upload, "filename"):
            return JSONResponse({"error": "file is required"}, status_code=400)

        base = os.path.basename(upload.filename or "")
        if base in ("", ".", ".."):
            return JSONResponse({"error": "invalid filename"}, status_code=400)
        ext = os.path.splitext(base)[1].lower()
        if ext not in _UPLOAD_ALLOWED_EXT:
            return JSONResponse({"error": "allowed types: .pdf, .md, .txt, .html"}, status_code=400)

        docs_path = Path(docs_dir)
        try:
            docs_path.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            logger.info("[http][%s] upload mkdir: %s", req_id, err)
            return JSONResponse({"error": "could not prepare docs folder"}, status_code=500)

        abs_dir = docs_path.resolve()
        abs_dest = (docs_path / base).resolve()
        if abs_dir not in abs_dest.parents:
            return JSONResponse({"error": "invalid path"}, status_code=400)

        written = 0
        try:
            with abs_dest.open("wb") as out:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > _UPLOAD_MAX_BYTES:
                        raise ValueError("upload too large")
                    out.write(chunk)
        except ValueError:
            abs_dest.unlink(missing_ok=True)
            return JSONResponse({"error": "invalid or too large upload"}, status_code=400)
        except OSError as err:
            abs_dest.unlink(missing_ok=True)
            logger.info("[http][%s] upload write: %s", req_id, err)
            return JSONResponse({"error": "could not write file"}, status_code=500)

        logger.info("[http][%s] upload ok name=%s bytes=%d", req_id, base, written)
        return JSONResponse({"name": base})

    # ---- ingest -------------------------------------------------------

    @app.post("/ingest")
    async def ingest() -> HTMLResponse:
        req_id = new_req_id()
        logger.info("[http][%s] ingest start", req_id)

        try:
            count = await rag_service.ingest()
        except Exception as err:
            logger.info("[http][%s] ingest failed: %s", req_id, err)
            return HTMLResponse(f'<p class="error">Ingestion failed: {html.escape(str(err))}</p>')

        logger.info("[http][%s] ingest success chunks=%d", req_id, count)
        return HTMLResponse(f'<p class="ok">Ingestion complete. Indexed {count} chunks.</p>')

    # ---- chat -----------------------------------------------------------

    @app.post("/chat/start")
    async def chat_start(request: Request):
        req_id = new_req_id()
        started_at_ms = int(time.time() * 1000)

        form = await request.form()
        question = str(form.get("question", "")).strip()
        model_id = str(form.get("model_id", "")).strip()

        if not question:
            return PlainTextResponse("question is required", status_code=400)
        if model_id not in MODEL_CONFIGS:
            return PlainTextResponse("invalid model selection", status_code=400)

        logger.info("[http][%s] chat start question_chars=%d model_id=%s", req_id, len(question), model_id)

        escaped_question = html.escape(question)
        escaped_query = urllib.parse.quote_plus(question)
        escaped_model_id = urllib.parse.quote_plus(model_id)
        escaped_started_at = urllib.parse.quote_plus(str(started_at_ms))

        body = (
            f'<div class="msg user">{escaped_question}</div>'
            f'<div class="msg assistant raw" id="assistant-last" data-streaming="1"></div>'
            f'<script>window.startAnswerStream("{escaped_query}","{escaped_model_id}","{escaped_started_at}");</script>'
        )
        return HTMLResponse(body)

    @app.get("/chat/stream")
    async def chat_stream(request: Request):
        req_id = new_req_id()
        stream_start = time.monotonic()

        question = request.query_params.get("question", "").strip()
        model_id = request.query_params.get("model_id", "").strip()
        started_at_raw = request.query_params.get("started_at_ms", "").strip()

        if not question:
            return PlainTextResponse("question is required", status_code=400)

        cfg = MODEL_CONFIGS.get(model_id)
        if cfg is None:
            return PlainTextResponse("invalid model_id", status_code=400)

        client = provider_clients.get(cfg.provider) if provider_clients else None
        if client is None:
            return PlainTextResponse("provider client not configured", status_code=500)

        api_key = (api_keys.get(cfg.env_key, "") if api_keys else "").strip()
        if not api_key and cfg.provider != "ollama":
            return PlainTextResponse(f"missing {cfg.env_key} for selected provider", status_code=400)

        logger.info(
            "[http][%s] stream start question_chars=%d provider=%s model=%s",
            req_id, len(question), cfg.provider, cfg.model,
        )

        def sse(event: str, data: str) -> str:
            return f"event: {event}\ndata: {data.replace(chr(10), chr(92) + 'n')}\n\n"

        async def event_stream() -> AsyncIterator[str]:
            try:
                prompt, results = await rag_service.build_prompt(question)
            except Exception as err:
                app_err = classify_error(err)
                logger.info(
                    "[http][%s] retrieval failed code=%s retryable=%s err=%s",
                    req_id, app_err.code, app_err.retryable, err,
                )
                yield sse("streamerror", encode_stream_error_payload(app_err))
                return

            if not results:
                yield sse("sources", base64.b64encode(b"[]").decode("ascii"))
                if prompt:
                    yield sse("token", prompt)
                yield sse("done", "complete")
                return

            src_rows = []
            for r in results:
                text = r.text
                if len(text) > _MAX_SOURCE_RUNES:
                    text = text[:_MAX_SOURCE_RUNES] + "…"
                src_rows.append({"page": r.page, "score": r.score, "text": text})
            src_json = json.dumps(src_rows).encode("utf-8")
            yield sse("sources", base64.b64encode(src_json).decode("ascii"))

            try:
                async for token in client.stream_answer(api_key, cfg.model, prompt):
                    yield sse("token", token)
            except Exception as err:
                app_err = classify_error(err)
                logger.info(
                    "[http][%s] stream failed code=%s retryable=%s err=%s",
                    req_id, app_err.code, app_err.retryable, err,
                )
                yield sse("streamerror", encode_stream_error_payload(app_err))
                return

            stream_duration = time.monotonic() - stream_start
            total_from_query = stream_duration
            if started_at_raw:
                try:
                    started_at_ms = int(started_at_raw)
                    if started_at_ms > 0:
                        total_from_query = time.time() - (started_at_ms / 1000)
                except ValueError:
                    pass
            logger.info(
                "[http][%s] stream complete duration=%.3fs total_from_query=%.3fs",
                req_id, stream_duration, total_from_query,
            )
            yield sse("done", "complete")

        return StreamingResponse(
            event_stream(),
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    return app


async def bootstrap() -> FastAPI:
    """Production wiring — equivalent of Go's main(). Only invoked when run
    as a script/ASGI factory, never at import time, so `app.main` stays
    importable (and route logic testable) without live Postgres/Ollama.

    Async (not sync) on purpose: asyncpg.create_pool()'s returned Pool needs
    a *running* event loop to connect. The old sync version wrapped just the
    pool creation in its own asyncio.run(...) — that loop is closed the
    instant asyncio.run() returns, so the pool's connections were bound to a
    loop that no longer existed by the time uvicorn's *own* asyncio.run()
    (inside uvicorn.run()) later tried to use them, failing with
    ConnectionDoesNotExistError on first real request. Pool creation and
    request handling now share one event loop end to end — see __main__.
    """
    import asyncpg

    settings = load_config()
    logger.info(
        "[boot] config loaded port=%s ollama_host=%s collection=%s top_k=%d handbook=%s "
        "openrouter_key=%s groq_key=%s llamaparse=%s",
        settings.port, settings.ollama_host, settings.collection_name, settings.top_k,
        settings.handbook_path, bool(settings.openrouter_api_key), bool(settings.groq_api_key),
        bool(os.environ.get("LLAMA_CLOUD_API_KEY")),
    )

    embed_client = OllamaClient(settings.ollama_host)
    pool = await asyncpg.create_pool(
        dsn=settings.database_url, command_timeout=30, timeout=10
    )
    store = PostgresStore(pool)
    rag_service = RagService(
        store=store,
        embed_client=embed_client,
        collection=settings.collection_name,
        top_k=settings.top_k,
        pdf_path=settings.handbook_path,
    )

    ollama_openai_base = settings.ollama_host.rstrip("/") + "/v1"
    provider_clients = {
        "openrouter": OpenAICompatibleClient(
            "https://openrouter.ai/api/v1",
            {"HTTP-Referer": "http://localhost", "X-Title": "handbook-rag"},
        ),
        "groq": OpenAICompatibleClient("https://api.groq.com/openai/v1"),
        "ollama": OpenAICompatibleClient(ollama_openai_base),
    }
    api_keys = {
        "OPENROUTER_API_KEY": settings.openrouter_api_key,
        "GROQ_API_KEY": settings.groq_api_key,
        "OLLAMA_API_KEY": settings.ollama_api_key,
    }

    rag_service.set_query_rewriter(provider_clients["ollama"], settings.ollama_api_key, "gemma4:26b")
    logger.info("[boot] query rewrite enabled provider=ollama model=gemma4:26b base=%s", ollama_openai_base)

    return create_app(
        rag_service=rag_service,
        provider_clients=provider_clients,
        api_keys=api_keys,
        embed_client=embed_client,
        store=store,
        settings=settings,
    )


if __name__ == "__main__":
    import uvicorn

    async def _serve() -> None:
        application = await bootstrap()
        config = uvicorn.Config(
            application, host="0.0.0.0", port=int(application.state.settings.port)
        )
        await uvicorn.Server(config).serve()

    asyncio.run(_serve())
