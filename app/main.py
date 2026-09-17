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
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTasks

from app.config import Settings, load_config
from app.embed import OllamaClient
from app.llm import OpenAICompatibleClient
from app.logging_utils import configure_logging, new_trace_id, trace_id_var
from app.rag import RagError, RagService
from app.store import PostgresStore
from app.traces import (
    AnnotationStatus,
    GenerateStep,
    RetrieveStep,
    RewriteStep,
    TraceCounts,
    TraceDetail,
    TraceError,
    TraceNeighbors,
    TraceStep,
    TraceStore,
    TraceSummary,
)

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


# ---- traces review UI (ticket 4) --------------------------------------

_TRACES_PAGE_STYLE = """
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f4f6f9; color: #1a1a2e; line-height: 1.5; }
    header { background: #1a1a2e; color: #fff; padding: 1rem 1.5rem; }
    header h1 { font-size: 1.1rem; }
    main.board { display: flex; height: calc(100vh - 53px); overflow: hidden; }

    .badge { display: inline-block; font-size: 0.72rem; font-weight: 700; border-radius: 99px; padding: 0.1rem 0.6rem; }
    .badge.ran { background: #e3f8ec; color: #147a4a; }
    .badge.not_configured { background: #eef0fb; color: #666; }
    .badge.failed_fallback { background: #fdeaea; color: #b00020; }

    /* left pane: thread list */
    .sidebar { width: 300px; flex-shrink: 0; background: #fff; border-right: 1px solid #e2e5ec; display: flex; flex-direction: column; }
    .sidebar-header { padding: 0.9rem 1rem; border-bottom: 1px solid #eee; }
    .sidebar-title { font-size: 0.85rem; font-weight: 700; }
    .count-badge { background: #eef0fb; color: #4361ee; border-radius: 99px; padding: 0.05rem 0.5rem; font-size: 0.7rem; margin-left: 0.4rem; }
    .filter-toggle { display: inline-block; margin-top: 0.5rem; color: #4361ee; text-decoration: none; border: 1px solid #4361ee; border-radius: 6px; padding: 0.2rem 0.55rem; font-size: 0.72rem; }
    .filter-toggle.active { background: #4361ee; color: #fff; }
    .thread-list { overflow-y: auto; flex: 1; }
    .thread-item { display: block; padding: 0.7rem 1rem; border-bottom: 1px solid #f0f1f5; text-decoration: none; color: inherit; }
    .thread-item:hover { background: #f7f8fc; }
    .thread-item.active { background: #eef0fb; border-left: 3px solid #4361ee; }
    .thread-item-top { display: flex; justify-content: space-between; align-items: center; font-family: monospace; font-size: 0.78rem; font-weight: 700; }
    .status-icon.pass { color: #147a4a; }
    .status-icon.fail { color: #b00020; }
    .status-icon.unannotated { color: #ccc; }
    .thread-question { font-size: 0.78rem; color: #444; margin-top: 0.25rem; }
    .thread-meta { font-size: 0.7rem; color: #999; margin-top: 0.2rem; }

    /* middle pane: trace steps */
    .middle-pane { flex: 1; overflow-y: auto; padding: 1.5rem; }
    .trace-question { background: #fff; border-radius: 8px; padding: 1rem 1.25rem; margin-bottom: 1rem; box-shadow: 0 1px 4px rgba(0,0,0,.08); font-size: 0.9rem; }
    details.step { background: #fff; border-radius: 8px; padding: 0.75rem 1.25rem; margin-bottom: 1rem; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
    details.step summary { font-size: 0.9rem; font-weight: 700; cursor: pointer; }
    details.step > *:not(summary) { margin-top: 0.6rem; }
    .step pre { white-space: pre-wrap; word-break: break-word; font-size: 0.8rem; background: #f7f8fc; border-radius: 6px; padding: 0.6rem; }
    .chunk { border-left: 3px solid #4361ee; background: #f7f8fc; border-radius: 6px; padding: 0.5rem 0.7rem; margin-top: 0.4rem; font-size: 0.8rem; }
    .chunk-meta { color: #888; font-size: 0.72rem; margin-bottom: 0.2rem; }
    .error-text { color: #b00020; font-size: 0.8rem; margin-top: 0.4rem; }
    .empty { color: #999; padding: 2rem; text-align: center; font-size: 0.85rem; }

    /* right pane: annotation panel */
    .right-pane { width: 280px; flex-shrink: 0; background: #fff; border-left: 1px solid #e2e5ec; padding: 1.25rem; overflow-y: auto; }
    .panel-title { font-size: 0.85rem; font-weight: 700; margin-bottom: 0.75rem; }
    .rate-group { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
    .rate-group input[type=radio] { position: absolute; opacity: 0; pointer-events: none; }
    .rate-btn { flex: 1; text-align: center; padding: 0.5rem; border-radius: 6px; font-size: 0.8rem; font-weight: 700; cursor: pointer; background: #eef0fb; color: #666; }
    .rate-btn.pass { color: #147a4a; }
    .rate-btn.fail { color: #b00020; }
    input#status-pass:checked + label.rate-btn.pass { background: #147a4a; color: #fff; }
    input#status-fail:checked + label.rate-btn.fail { background: #b00020; color: #fff; }
    .right-pane label { display: block; font-size: 0.8rem; font-weight: 600; margin: 0.6rem 0 0.3rem; }
    .right-pane textarea, .right-pane input[type=text] { width: 100%; padding: 0.5rem; border: 1px solid #d0d5dd; border-radius: 6px; font-size: 0.85rem; font-family: inherit; }
    .update-btn { margin-top: 0.9rem; width: 100%; background: #4361ee; color: #fff; border: none; border-radius: 6px; padding: 0.6rem; font-size: 0.85rem; cursor: pointer; }
    .panel-nav { display: flex; justify-content: space-between; margin-top: 1.25rem; padding-top: 1rem; border-top: 1px solid #eee; }
    .panel-nav a, .panel-nav span.disabled { font-size: 0.82rem; text-decoration: none; color: #4361ee; }
    .panel-nav span.disabled { color: #ccc; }
  </style>
"""


def _rewrite_status_badge(status: str) -> str:
    return f'<span class="badge {html.escape(status)}">{html.escape(status)}</span>'


def _status_icon(status: str | None) -> str:
    if status == AnnotationStatus.PASS.value:
        return '<span class="status-icon pass">&#10003;</span>'
    if status == AnnotationStatus.FAIL.value:
        return '<span class="status-icon fail">&#10007;</span>'
    return '<span class="status-icon unannotated">&ndash;</span>'


def _step_html(step: TraceStep) -> str:
    if isinstance(step, RewriteStep):
        rows = []
        if step.original_question:
            rows.append(f"<pre>original: {html.escape(step.original_question)}</pre>")
        if step.rewritten_query:
            rows.append(f"<pre>rewritten: {html.escape(step.rewritten_query)}</pre>")
        summary = f"Rewrite {_rewrite_status_badge(step.status.value)}"
        return f'<details class="step" open><summary>{summary}</summary>{"".join(rows)}</details>'

    if isinstance(step, RetrieveStep):
        rows = []
        for r in step.results:
            rows.append(
                f'<div class="chunk"><div class="chunk-meta">page {r.page} · score {r.score:.4f}</div>'
                f"{html.escape(r.text)}</div>"
            )
        if step.error:
            rows.append(f'<div class="error-text">{html.escape(step.error)}</div>')
        summary = f"Retrieve ({len(step.results)} results)"
        return f'<details class="step" open><summary>{summary}</summary>{"".join(rows)}</details>'

    if isinstance(step, GenerateStep):
        rows = [
            f"<pre>{html.escape(step.prompt)}</pre>",
            f"<pre>{html.escape(step.output)}</pre>",
        ]
        if step.error:
            rows.append(f'<div class="error-text">{html.escape(step.error)}</div>')
        return f'<details class="step" open><summary>Generate</summary>{"".join(rows)}</details>'

    step_type = html.escape(str(getattr(step, "type", "?")))
    return f'<details class="step" open><summary>Unknown step type: {step_type}</summary></details>'


def _page_shell(title: str, header_html: str, body_html: str, *, main_class: str = "") -> str:
    main_attr = f' class="{main_class}"' if main_class else ""
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\"/>"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>"
        f"<title>{title}</title>{_TRACES_PAGE_STYLE}</head><body>"
        f"<header>{header_html}</header>"
        f"<main{main_attr}>{body_html}</main>"
        "</body></html>"
    )


def _sidebar_html(
    traces: list[TraceSummary], counts: TraceCounts, unannotated_only: bool, selected_id: str | None
) -> str:
    qs = "?unannotated=1" if unannotated_only else ""
    if not traces:
        list_html = (
            '<div class="empty">No unannotated traces — everything caught up.</div>'
            if unannotated_only
            else '<div class="empty">No traces yet — chat turns are captured automatically once you ask a question.</div>'
        )
    else:
        items = []
        for t in traces:
            question = t.question if len(t.question) <= 70 else t.question[:67] + "…"
            active = " active" if t.trace_id == selected_id else ""
            items.append(
                f'<a class="thread-item{active}" href="/traces/{urllib.parse.quote(t.trace_id)}{qs}">'
                f'<div class="thread-item-top"><span>{html.escape(t.trace_id)}</span>{_status_icon(t.status)}</div>'
                f'<div class="thread-question">{html.escape(question)}</div>'
                f'<div class="thread-meta">{html.escape(t.created_at.isoformat())}</div>'
                "</a>"
            )
        list_html = "".join(items)

    toggle_href = "/traces" if unannotated_only else "/traces?unannotated=1"
    toggle_label = "Show all" if unannotated_only else f"Show unannotated ({counts.unannotated})"
    return (
        '<div class="sidebar">'
        '<div class="sidebar-header">'
        f'<span class="sidebar-title">All Traces<span class="count-badge">{counts.total}</span></span><br/>'
        f'<a class="filter-toggle{" active" if unannotated_only else ""}" href="{toggle_href}">{toggle_label}</a>'
        "</div>"
        f'<div class="thread-list">{list_html}</div>'
        "</div>"
    )


def _annotation_panel_html(trace: TraceDetail, neighbors: TraceNeighbors, unannotated_only: bool) -> str:
    tags_value = html.escape(", ".join(trace.tags))
    note_value = html.escape(trace.note or "")
    qs = "?unannotated=1" if unannotated_only else ""
    unannotated_field = '<input type="hidden" name="unannotated" value="1"/>' if unannotated_only else ""
    pass_checked = " checked" if trace.status == AnnotationStatus.PASS.value else ""
    fail_checked = " checked" if trace.status == AnnotationStatus.FAIL.value else ""

    prev_html = (
        f'<a href="/traces/{urllib.parse.quote(neighbors.prev_id)}{qs}">&larr; Back</a>'
        if neighbors.prev_id
        else '<span class="disabled">&larr; Back</span>'
    )
    next_html = (
        f'<a href="/traces/{urllib.parse.quote(neighbors.next_id)}{qs}">Next &rarr;</a>'
        if neighbors.next_id
        else '<span class="disabled">Next &rarr;</span>'
    )

    return (
        '<div class="panel-title">Rate Conversation</div>'
        f'<form method="post" action="/traces/{urllib.parse.quote(trace.trace_id)}/annotate">'
        f"{unannotated_field}"
        '<div class="rate-group">'
        f'<input type="radio" id="status-pass" name="status" value="{AnnotationStatus.PASS.value}"{pass_checked}/>'
        f'<label for="status-pass" class="rate-btn pass">{AnnotationStatus.PASS.value}</label>'
        f'<input type="radio" id="status-fail" name="status" value="{AnnotationStatus.FAIL.value}"{fail_checked}/>'
        f'<label for="status-fail" class="rate-btn fail">{AnnotationStatus.FAIL.value}</label>'
        "</div>"
        '<label for="note">Notes</label>'
        f'<textarea id="note" name="note" rows="5" placeholder="Add your notes here...">{note_value}</textarea>'
        '<label for="tags">Tags (comma-separated)</label>'
        f'<input id="tags" type="text" name="tags" value="{tags_value}"/>'
        '<button type="submit" class="update-btn">Update Annotation</button>'
        "</form>"
        f'<div class="panel-nav">{prev_html}{next_html}</div>'
    )


def _traces_page_html(
    traces: list[TraceSummary],
    counts: TraceCounts,
    unannotated_only: bool,
    selected: TraceDetail | None,
    neighbors: TraceNeighbors | None,
) -> str:
    """Renders the single 3-pane board (thread list / trace steps /
    annotation panel) — both GET /traces and GET /traces/{trace_id} share
    this, differing only in whether a trace is selected, so the sidebar and
    its filter/count state never fall out of sync between the two routes."""
    sidebar_html = _sidebar_html(traces, counts, unannotated_only, selected.trace_id if selected else None)

    if selected is None:
        middle_html = '<div class="empty">Select a trace to review.</div>'
        right_html = ""
    else:
        question_html = f'<div class="trace-question">{html.escape(selected.question)}</div>'
        steps_html = "".join(_step_html(step) for step in selected.steps)
        middle_html = f"{question_html}{steps_html}"
        panel_html = _annotation_panel_html(selected, neighbors or TraceNeighbors(), unannotated_only)
        right_html = f'<div class="right-pane">{panel_html}</div>'

    body_html = f'{sidebar_html}<div class="middle-pane">{middle_html}</div>{right_html}'
    title = f"Trace {html.escape(selected.trace_id)}" if selected else "Traces"
    return _page_shell(title, "<h1>Traces</h1>", body_html, main_class="board")


def create_app(
    *,
    rag_service: RagService,
    provider_clients: dict[str, OpenAICompatibleClient],
    api_keys: dict[str, str],
    embed_client: OllamaClient,
    store: PostgresStore,
    trace_store: TraceStore,
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

            # Mutated inside event_stream() below, then read by write_trace()
            # once the SSE stream has finished. should_write_trace flips True
            # either on full success or on a failure path that still has
            # something worth recording (ticket 3: failures are captured,
            # not dropped — docs/evals/phase1-spec.md) — it no longer means
            # "the turn fully completed".
            trace_steps: list[TraceStep] = []
            should_write_trace = False

            async def event_stream() -> AsyncIterator[str]:
                nonlocal should_write_trace
                try:
                    build_result = await rag_service.build_prompt(question)
                except RagError as err:
                    app_err = classify_error(err)
                    logger.info(
                        "retrieval failed code=%s retryable=%s err=%s",
                        app_err.code, app_err.retryable, err, extra={"stage": "retrieve"},
                    )
                    if err.rewrite_outcome is not None:
                        trace_steps.append(RewriteStep.from_outcome(err.rewrite_outcome))
                        trace_steps.append(RetrieveStep(results=[], error=str(err)))
                        should_write_trace = True
                    yield sse("streamerror", encode_stream_error_payload(app_err))
                    return
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

                trace_steps.append(RewriteStep.from_outcome(build_result.rewrite_outcome))
                trace_steps.append(RetrieveStep(results=results))

                src_rows = []
                for r in results:
                    text = r.text
                    if len(text) > _MAX_SOURCE_RUNES:
                        text = text[:_MAX_SOURCE_RUNES] + "…"
                    src_rows.append({"page": r.page, "score": r.score, "text": text})
                src_json = json.dumps(src_rows).encode("utf-8")
                yield sse("sources", base64.b64encode(src_json).decode("ascii"))

                generated_tokens: list[str] = []
                try:
                    async for token in client.stream_answer(api_key, cfg.model, prompt):
                        generated_tokens.append(token)
                        yield sse("token", token)
                except Exception as err:
                    app_err = classify_error(err)
                    logger.info(
                        "stream failed code=%s retryable=%s err=%s",
                        app_err.code, app_err.retryable, err, extra={"stage": "generate"},
                    )
                    trace_steps.append(
                        GenerateStep(
                            prompt=prompt, output="".join(generated_tokens), error=str(err)
                        )
                    )
                    should_write_trace = True
                    yield sse("streamerror", encode_stream_error_payload(app_err))
                    return

                trace_steps.append(
                    GenerateStep(prompt=prompt, output="".join(generated_tokens))
                )
                should_write_trace = True

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

            async def write_trace() -> None:
                # Fire-and-forget: runs after the SSE stream has fully sent,
                # so a slow or failed write never adds latency to, or
                # breaks, the chat response (docs/adr/0001, spec story 12/13).
                if not should_write_trace:
                    return
                try:
                    await trace_store.write(trace_id, question, trace_steps)
                except TraceError as err:
                    logger.warning(
                        "trace write failed err=%s", err, extra={"stage": "trace"},
                    )

            async def reset_trace_id() -> None:
                trace_id_var.reset(trace_token)

            reset_deferred = True
            background_tasks = BackgroundTasks()
            # write_trace before reset_trace_id, so its own log lines (on
            # failure) still carry trace_id via the contextvar filter.
            background_tasks.add_task(write_trace)
            background_tasks.add_task(reset_trace_id)
            return StreamingResponse(
                event_stream(),
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
                background=background_tasks,
            )
        finally:
            if not reset_deferred:
                trace_id_var.reset(trace_token)

    # ---- traces review (ticket 4) ---------------------------------------

    @app.get("/traces")
    async def traces_list(unannotated: bool = False) -> HTMLResponse:
        # Renders the same 3-pane board as GET /traces/{trace_id}, with the
        # most recent trace auto-selected (matching the reference UI's
        # always-something-open sidebar) — never redirects there, so this
        # stays a plain 200 for callers that just want the thread list.
        try:
            traces = await trace_store.list_recent(unannotated_only=unannotated)
            counts = await trace_store.counts()
        except TraceError as err:
            logger.warning("traces list failed err=%s", err, extra={"stage": "http"})
            return HTMLResponse(
                _traces_page_html([], TraceCounts(total=0, unannotated=0), unannotated, None, None),
                status_code=500,
            )

        if not traces:
            return HTMLResponse(_traces_page_html(traces, counts, unannotated, None, None))

        first_id = traces[0].trace_id
        token = trace_id_var.set(first_id)
        try:
            trace = await trace_store.get(first_id)
            neighbors = await trace_store.neighbors(first_id, unannotated_only=unannotated)
        except TraceError as err:
            logger.warning("traces list failed err=%s", err, extra={"stage": "http"})
            return HTMLResponse(
                _traces_page_html(traces, counts, unannotated, None, None), status_code=500
            )
        finally:
            trace_id_var.reset(token)

        return HTMLResponse(_traces_page_html(traces, counts, unannotated, trace, neighbors))

    @app.get("/traces/{trace_id}")
    async def trace_detail(trace_id: str, unannotated: bool = False) -> HTMLResponse:
        # trace_id is the Trace being reviewed, so it's the natural scope for
        # trace_id_var here (spec story 17: correlate a Trace's review-UI
        # activity with its own capture logs via the same trace_id).
        token = trace_id_var.set(trace_id)
        try:
            try:
                trace = await trace_store.get(trace_id)
                if trace is not None:
                    neighbors = await trace_store.neighbors(trace_id, unannotated_only=unannotated)
                    traces = await trace_store.list_recent(unannotated_only=unannotated)
                    counts = await trace_store.counts()
            except TraceError as err:
                logger.warning("trace detail failed err=%s", err, extra={"stage": "http"})
                return HTMLResponse("<p>Could not load trace.</p>", status_code=500)
            if trace is None:
                return HTMLResponse("<p>Trace not found.</p>", status_code=404)
            return HTMLResponse(_traces_page_html(traces, counts, unannotated, trace, neighbors))
        finally:
            trace_id_var.reset(token)

    @app.post("/traces/{trace_id}/annotate")
    async def annotate_trace(trace_id: str, request: Request):
        token = trace_id_var.set(trace_id)
        try:
            form = await request.form()
            status_raw = str(form.get("status", "")).strip()
            note = str(form.get("note", "")).strip()
            tags = [t.strip() for t in str(form.get("tags", "")).split(",") if t.strip()]
            unannotated_filter = str(form.get("unannotated", "")).strip() == "1"

            try:
                status = AnnotationStatus(status_raw)
            except ValueError:
                valid = [s.value for s in AnnotationStatus]
                return PlainTextResponse(f"status must be one of {valid}", status_code=400)

            try:
                updated = await trace_store.annotate(trace_id, status, note, tags)
            except TraceError as err:
                logger.warning("annotate failed err=%s", err, extra={"stage": "http"})
                return HTMLResponse("<p>Could not save annotation.</p>", status_code=500)

            if not updated:
                return HTMLResponse("<p>Trace not found.</p>", status_code=404)

            redirect_qs = "?unannotated=1" if unannotated_filter else ""
            return RedirectResponse(
                url=f"/traces/{urllib.parse.quote(trace_id)}{redirect_qs}", status_code=303
            )
        finally:
            trace_id_var.reset(token)

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
    trace_store = TraceStore(pool)
    try:
        await trace_store.ensure_schema()
    except TraceError as err:
        # Eval capture is a non-critical, complementary layer (ADR-0001) —
        # a traces-table setup failure must not take down the chat product
        # itself, matching write_trace()'s own TraceError handling below.
        logger.warning("[boot] traces schema setup failed: %s", err, extra={"stage": "boot"})
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
        trace_store=trace_store,
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
