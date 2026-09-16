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
from starlette.background import BackgroundTask

from app.config import Settings, load_config
from app.embed import OllamaClient
from app.llm import OpenAICompatibleClient
from app.logging_utils import configure_logging, new_trace_id, trace_id_var
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
    "openrouter_nemotron": ModelConfig("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free", "OPENROUTER_API_KEY"),
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
        # One trace_id per upload-then-ingest action, threaded through to
        # /ingest via the JSON response's "trace_id" field (see the
        # equivalent chat_start -> chat_stream wiring above). No
        # StreamingResponse involved here, so a plain try/finally is safe.
        req_id = new_req_id()
        trace_id = new_trace_id()
        token = trace_id_var.set(trace_id)
        try:
            try:
                form = await request.form()
            except Exception as err:
                logger.info("[http][%s] upload parse form: %s", req_id, err, extra={"stage": "http"})
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
                logger.info("[http][%s] upload mkdir: %s", req_id, err, extra={"stage": "http"})
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
                logger.info("[http][%s] upload write: %s", req_id, err, extra={"stage": "http"})
                return JSONResponse({"error": "could not write file"}, status_code=500)

            logger.info("[http][%s] upload ok name=%s bytes=%d", req_id, base, written, extra={"stage": "http"})
            return JSONResponse({"name": base, "trace_id": trace_id})
        finally:
            trace_id_var.reset(token)

    # ---- ingest -------------------------------------------------------

    @app.post("/ingest")
    async def ingest(request: Request) -> HTMLResponse:
        # Continues the trace_id upload_doc minted (passed back as a query
        # param, same pattern as chat_stream's trace_id). Falls back to
        # minting a fresh one so hitting this endpoint directly doesn't
        # break, but that's a real gap worth a warning. Plain try/finally is
        # safe here too — ingest() returns a plain HTMLResponse, no
        # separately-spawned task involved (unlike chat_stream).
        raw_trace_id = request.query_params.get("trace_id", "").strip()
        missing_trace_id = not raw_trace_id
        trace_id = raw_trace_id or new_trace_id()
        token = trace_id_var.set(trace_id)
        try:
            if missing_trace_id:
                logger.warning(
                    "ingest missing trace_id; generated %s", trace_id,
                    extra={"stage": "ingest"},
                )

            req_id = new_req_id()
            logger.info("[http][%s] ingest start", req_id, extra={"stage": "ingest"})

            try:
                count = await rag_service.ingest()
            except Exception as err:
                logger.info("[http][%s] ingest failed: %s", req_id, err, extra={"stage": "ingest"})
                return HTMLResponse(f'<p class="error">Ingestion failed: {html.escape(str(err))}</p>')

            logger.info("[http][%s] ingest success chunks=%d", req_id, count, extra={"stage": "ingest"})
            return HTMLResponse(f'<p class="ok">Ingestion complete. Indexed {count} chunks.</p>')
        finally:
            trace_id_var.reset(token)

    # ---- chat -----------------------------------------------------------

    @app.post("/chat/start")
    async def chat_start(request: Request):
        # One trace_id per chat turn, threaded through to /chat/stream via
        # the rendered <script> call below — see logging_utils.trace_id_var.
        # Set/reset is safe as a plain try/finally here (unlike chat_stream,
        # this handler has no StreamingResponse to outlive its own return).
        trace_id = new_trace_id()
        token = trace_id_var.set(trace_id)
        try:
            started_at_ms = int(time.time() * 1000)

            form = await request.form()
            question = str(form.get("question", "")).strip()
            model_id = str(form.get("model_id", "")).strip()

            if not question:
                return PlainTextResponse("question is required", status_code=400)
            if model_id not in MODEL_CONFIGS:
                return PlainTextResponse("invalid model selection", status_code=400)

            logger.info(
                "chat start question_chars=%d model_id=%s",
                len(question), model_id, extra={"stage": "http"},
            )

            escaped_question = html.escape(question)
            escaped_query = urllib.parse.quote_plus(question)
            escaped_model_id = urllib.parse.quote_plus(model_id)
            escaped_started_at = urllib.parse.quote_plus(str(started_at_ms))
            escaped_trace_id = urllib.parse.quote_plus(trace_id)

            body = (
                f'<div class="msg user">{escaped_question}</div>'
                f'<div class="msg assistant raw" id="assistant-last" data-streaming="1"></div>'
                f'<script>window.startAnswerStream("{escaped_query}","{escaped_model_id}","{escaped_started_at}","{escaped_trace_id}");</script>'
            )
            return HTMLResponse(body)
        finally:
            trace_id_var.reset(token)

    @app.get("/chat/stream")
    async def chat_stream(request: Request):
        # Continues the trace_id chat_start minted (passed back as a query
        # param, same pattern as question/model_id/started_at_ms — see
        # window.startAnswerStream in app/templates/index.html). Falls back
        # to minting a fresh one so hitting this endpoint directly doesn't
        # break, but that's a real gap worth a warning.
        raw_trace_id = request.query_params.get("trace_id", "").strip()
        missing_trace_id = not raw_trace_id
        trace_id = raw_trace_id or new_trace_id()
        trace_token = trace_id_var.set(trace_id)
        reset_deferred = False

        # Unlike chat_start, this handler can return a StreamingResponse
        # whose body (event_stream() below) is driven by Starlette *after*
        # this coroutine returns — under anyio that runs in a separate task
        # with its own copy of the current context, so `trace_id_var.reset`
        # must NOT happen in a plain try/finally here (that fires the
        # instant this function returns, before the copy is even taken, and
        # calling .reset() from that other task raises "Token was created
        # in a different Context" anyway). Early-return validation paths
        # below reset immediately since no streaming ever starts for them;
        # the success path defers the reset to a StreamingResponse
        # background task, which runs in *this* same context, after the
        # stream has fully sent.
        try:
            if missing_trace_id:
                logger.warning(
                    "chat stream missing trace_id; generated %s", trace_id,
                    extra={"stage": "http"},
                )

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
                "stream start question_chars=%d provider=%s model=%s",
                len(question), cfg.provider, cfg.model, extra={"stage": "http"},
            )

            def sse(event: str, data: str) -> str:
                return f"event: {event}\ndata: {data.replace(chr(10), chr(92) + 'n')}\n\n"

            async def event_stream() -> AsyncIterator[str]:
                try:
                    build_result = await rag_service.build_prompt(question)
                except Exception as err:
                    app_err = classify_error(err)
                    logger.info(
                        "retrieval failed code=%s retryable=%s err=%s",
                        app_err.code, app_err.retryable, err, extra={"stage": "retrieve"},
                    )
                    yield sse("streamerror", encode_stream_error_payload(app_err))
                    return

                prompt, results = build_result.prompt, build_result.results
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
                        "stream failed code=%s retryable=%s err=%s",
                        app_err.code, app_err.retryable, err, extra={"stage": "generate"},
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
                    "stream complete duration=%.3fs total_from_query=%.3fs",
                    stream_duration, total_from_query, extra={"stage": "generate"},
                )
                yield sse("done", "complete")

            async def reset_trace_id() -> None:
                trace_id_var.reset(trace_token)

            reset_deferred = True
            return StreamingResponse(
                event_stream(),
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
                background=BackgroundTask(reset_trace_id),
            )
        finally:
            if not reset_deferred:
                trace_id_var.reset(trace_token)

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

    configure_logging(service="rag-server")

    settings = load_config()
    logger.info(
        "[boot] config loaded port=%s ollama_host=%s collection=%s top_k=%d handbook=%s "
        "openrouter_key=%s groq_key=%s llamaparse=%s",
        settings.port, settings.ollama_host, settings.collection_name, settings.top_k,
        settings.handbook_path, bool(settings.openrouter_api_key), bool(settings.groq_api_key),
        bool(os.environ.get("LLAMA_CLOUD_API_KEY")),
        extra={"stage": "boot"},
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
    logger.info(
        "[boot] query rewrite enabled provider=ollama model=gemma4:26b base=%s",
        ollama_openai_base, extra={"stage": "boot"},
    )

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
