"""HTTP/SSE contract tests for app/main.py — a Python translation of
cmd/server/main_test.go's 9 tests, locking in the CURRENT contract the HTMX
frontend depends on (see docs/ui-execution-tracker.md).

Like the Go original, these exercise only the deterministic paths
(validation, templating, dependency-unavailable error contract) without
requiring live Ollama/Postgres/LLM-provider processes. "Unreachable" here
means a real network target that refuses the connection (127.0.0.1:1 for
Ollama; a lazily-connecting asyncpg pool at 127.0.0.1:1 for Postgres) —
same approach as Go's unreachableService(), not a mock — so the real
error-classification code path is exercised.

One deliberate deviation from the Go test text: TestHandleIngest_DependencyUnavailable
asserted the string "ensure qdrant collection" because Go's service.go wraps
the store error with that literal prefix. app/rag.py (already ported,
ticket 10) does not add that wrapping — Postgres failures surface via
app/store.py's own "ensure_schema failed for ..." message instead. The
Python test below asserts on that actual message rather than a Go-specific
string that no longer applies.
"""

import base64
import json
import logging
import re
import uuid

import asyncpg
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.embed import OllamaClient
from app.llm import OpenAICompatibleClient
from app.logging_utils import install_trace_id_filter, trace_id_var
from app.main import create_app
from app.rag import RagService
from app.store import PostgresStore, SearchResult
from app.traces import TraceStore


async def _unreachable_rag_service() -> RagService:
    embed_client = OllamaClient("http://127.0.0.1:1")
    pool = await asyncpg.create_pool(
        dsn="postgresql://u:p@127.0.0.1:1/db",
        min_size=0,
        max_size=1,
        timeout=1,
        command_timeout=1,
    )
    store = PostgresStore(pool)
    return RagService(
        store=store,
        embed_client=embed_client,
        collection="handbook_chunks",
        top_k=10,
        pdf_path="unused.pdf",
    )


def _app_for(
    rag_service: RagService, provider_clients=None, api_keys=None, docs_dir=None, trace_store=None
) -> FastAPI:
    kwargs = {}
    if docs_dir is not None:
        kwargs["docs_dir"] = docs_dir
    return create_app(
        rag_service=rag_service,
        provider_clients=provider_clients or {},
        api_keys=api_keys or {},
        embed_client=OllamaClient("http://127.0.0.1:1"),
        store=PostgresStore(pool=None),
        # Unused pool is safe: write_trace() only calls trace_store.write()
        # along the full happy path, never exercised by the tests below
        # that pass no explicit trace_store.
        trace_store=trace_store or TraceStore(pool=None),
        **kwargs,
    )


def _async_client(app: FastAPI) -> AsyncClient:
    # httpx.AsyncClient (not starlette's sync TestClient) so every request
    # runs on the *same* event loop as the test — the unreachable
    # RagService above owns an asyncpg pool bound to that loop, and
    # asyncpg connections/pools cannot cross event loops (TestClient runs
    # the ASGI app on a separate thread/loop via anyio's BlockingPortal).
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def _extract_sse_data(body: str, event: str) -> str:
    marker = f"event: {event}\ndata: "
    idx = body.find(marker)
    assert idx != -1, f"event {event!r} not found in SSE body: {body}"
    rest = body[idx + len(marker):]
    end = rest.find("\n")
    return rest if end == -1 else rest[:end]


def _extract_trace_id_from_start_body(body: str) -> str:
    # window.startAnswerStream("<question>","<model_id>","<started_at_ms>","<trace_id>")
    match = re.search(r'window\.startAnswerStream\("[^"]*","[^"]*","[^"]*","([^"]*)"\)', body)
    assert match, f"trace_id not found in chat_start body: {body}"
    return match.group(1)


async def test_handle_health_live():
    svc = await _unreachable_rag_service()
    async with _async_client(_app_for(svc)) as client:
        resp = await client.get("/health/live")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_handle_chat_start_valid_request():
    svc = await _unreachable_rag_service()
    async with _async_client(_app_for(svc)) as client:
        resp = await client.post(
            "/chat/start",
            data={"question": "What is <b>bold</b>?", "model_id": "groq_llama31_8b"},
        )

    assert resp.status_code == 200
    body = resp.text
    # The question must be HTML-escaped in the rendered user bubble.
    assert "What is &lt;b&gt;bold&lt;/b&gt;?" in body
    # The assistant placeholder + streaming trigger must be present with the
    # expected element id — the frontend JS (window.startAnswerStream)
    # depends on this exact shape.
    assert 'id="assistant-last"' in body
    assert "window.startAnswerStream(" in body


async def test_handle_chat_start_missing_question():
    svc = await _unreachable_rag_service()
    async with _async_client(_app_for(svc)) as client:
        resp = await client.post("/chat/start", data={"model_id": "groq_llama31_8b"})

    assert resp.status_code == 400


async def test_handle_chat_start_invalid_model_id():
    svc = await _unreachable_rag_service()
    async with _async_client(_app_for(svc)) as client:
        resp = await client.post("/chat/start", data={"question": "hi", "model_id": "not_a_real_model"})

    assert resp.status_code == 400


async def test_handle_chat_start_wrong_method():
    svc = await _unreachable_rag_service()
    async with _async_client(_app_for(svc)) as client:
        resp = await client.get("/chat/start")

    assert resp.status_code == 405


async def test_handle_ingest_dependency_unavailable():
    svc = await _unreachable_rag_service()
    async with _async_client(_app_for(svc)) as client:
        resp = await client.post("/ingest")

    # Current behavior: the handler does NOT set a non-200 status on failure
    # — it renders an HTMX-swappable error fragment with implicit 200. This
    # is a real characteristic of the contract, not an oversight to "fix"
    # silently during the port.
    assert resp.status_code == 200
    body = resp.text
    assert '<p class="error">Ingestion failed:' in body
    assert "ensure_schema failed" in body


async def test_handle_chat_stream_missing_question():
    svc = await _unreachable_rag_service()
    async with _async_client(_app_for(svc)) as client:
        resp = await client.get("/chat/stream", params={"model_id": "groq_llama31_8b"})

    assert resp.status_code == 400


async def test_handle_chat_stream_invalid_model_id():
    svc = await _unreachable_rag_service()
    async with _async_client(_app_for(svc)) as client:
        resp = await client.get("/chat/stream", params={"question": "hi", "model_id": "nope"})

    assert resp.status_code == 400


async def test_handle_chat_stream_dependency_unavailable_emits_streamerror():
    svc = await _unreachable_rag_service()
    # A configured provider client + API key must be present for the handler
    # to reach build_prompt at all (it 500s earlier otherwise) — but
    # retrieval fails before the provider client is ever called (the embed
    # step fails first), so the client itself just needs to exist, not
    # succeed.
    provider_clients = {"groq": OpenAICompatibleClient("http://127.0.0.1:1")}
    api_keys = {"GROQ_API_KEY": "test-key"}

    async with _async_client(_app_for(svc, provider_clients, api_keys)) as client:
        resp = await client.get("/chat/stream", params={"question": "hi", "model_id": "groq_llama31_8b"})

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/event-stream"

    body = resp.text
    assert "event: streamerror" in body

    payload = _extract_sse_data(body, "streamerror")
    raw = base64.b64decode(payload)
    decoded = json.loads(raw)
    assert decoded["code"] == "dependency_unavailable"
    assert decoded["message"] == (
        "Search is temporarily unavailable because the retrieval service is offline. "
        "Please try again shortly."
    )


async def test_handle_ingest_logs_carry_ingest_stage(caplog):
    # Proves app/main.py's ingest handler wires `extra={"stage": "ingest"}`
    # through to the actual LogRecord — the JSON shape itself is covered by
    # tests/test_logging_utils.py's isolated JsonFormatter unit tests.
    svc = await _unreachable_rag_service()
    with caplog.at_level(logging.INFO):
        async with _async_client(_app_for(svc)) as client:
            resp = await client.post("/ingest")

    assert resp.status_code == 200
    ingest_records = [r for r in caplog.records if getattr(r, "stage", None) == "ingest"]
    assert ingest_records, "expected at least one log record with stage=ingest"


async def test_handle_chat_start_logs_carry_http_stage(caplog):
    svc = await _unreachable_rag_service()
    with caplog.at_level(logging.INFO):
        async with _async_client(_app_for(svc)) as client:
            resp = await client.post(
                "/chat/start",
                data={"question": "hi", "model_id": "groq_llama31_8b"},
            )

    assert resp.status_code == 200
    http_records = [r for r in caplog.records if getattr(r, "stage", None) == "http"]
    assert http_records, "expected at least one log record with stage=http"


# ---- Ticket 3: trace_id correlation across /chat/start -> /chat/stream ----
#
# install_trace_id_filter() must run before these assertions mean anything:
# without it, `record.trace_id` is never populated (stays unset/None via
# JsonFormatter's getattr fallback) regardless of whether the ContextVar
# itself is set correctly, which would let a broken wiring pass silently.
# We call it directly (not the full configure_logging()) because
# configure_logging() also does `root_logger.handlers.clear()`, which would
# rip out caplog's own handler mid-test.


async def test_trace_id_correlates_chat_start_and_chat_stream(caplog):
    install_trace_id_filter()
    svc = await _unreachable_rag_service()
    # Needs a configured provider client + API key to get far enough into
    # chat_stream to reach the "stream start" log line and the streaming
    # body (retrieval then fails against the unreachable store/embedder,
    # emitting a stage=retrieve log too) — same setup as
    # test_handle_chat_stream_dependency_unavailable_emits_streamerror.
    provider_clients = {"groq": OpenAICompatibleClient("http://127.0.0.1:1")}
    api_keys = {"GROQ_API_KEY": "test-key"}

    with caplog.at_level(logging.INFO):
        async with _async_client(_app_for(svc, provider_clients, api_keys)) as client:
            start_resp = await client.post(
                "/chat/start",
                data={"question": "hi", "model_id": "groq_llama31_8b"},
            )
            assert start_resp.status_code == 200
            trace_id = _extract_trace_id_from_start_body(start_resp.text)

            stream_resp = await client.get(
                "/chat/stream",
                params={"question": "hi", "model_id": "groq_llama31_8b", "trace_id": trace_id},
            )
            assert stream_resp.status_code == 200

    turn_records = [
        r for r in caplog.records
        if getattr(r, "stage", None) in ("http", "retrieve", "generate")
    ]
    assert turn_records, "expected log records from the chat turn"

    trace_ids_seen = {getattr(r, "trace_id", None) for r in turn_records}
    assert trace_ids_seen == {trace_id}, (
        f"expected every chat-turn log record to carry trace_id={trace_id!r}, "
        f"saw {trace_ids_seen!r}"
    )

    # Make sure we actually exercised both HTTP calls' own log lines, not
    # just one of them.
    assert any(
        getattr(r, "stage", None) == "http" and "chat start" in r.getMessage()
        for r in turn_records
    ), "expected a log line from chat_start"
    assert any(
        getattr(r, "stage", None) == "http" and "stream start" in r.getMessage()
        for r in turn_records
    ), "expected a log line from chat_stream"
    assert any(
        getattr(r, "stage", None) == "retrieve" for r in turn_records
    ), "expected the retrieval-failure log line emitted deep inside event_stream()"


async def test_trace_id_does_not_leak_across_unrelated_requests():
    install_trace_id_filter()
    svc = await _unreachable_rag_service()
    async with _async_client(_app_for(svc)) as client:
        resp1 = await client.post(
            "/chat/start", data={"question": "first", "model_id": "groq_llama31_8b"}
        )
        resp2 = await client.post(
            "/chat/start", data={"question": "second", "model_id": "groq_llama31_8b"}
        )

    assert resp1.status_code == 200 and resp2.status_code == 200
    trace_id_1 = _extract_trace_id_from_start_body(resp1.text)
    trace_id_2 = _extract_trace_id_from_start_body(resp2.text)

    assert trace_id_1 != trace_id_2

    # The real proof the ContextVar was reset (not just overwritten): back
    # at module level, outside of any request handling, nothing should
    # still be set. If chat_start() forgot to reset, this would show
    # trace_id_2 here instead of None.
    assert trace_id_var.get() is None


# ---- Ticket 4: trace_id correlation across /docs/upload -> /ingest ----


async def test_trace_id_correlates_upload_and_ingest(tmp_path, caplog):
    install_trace_id_filter()
    svc = await _unreachable_rag_service()

    with caplog.at_level(logging.INFO):
        async with _async_client(_app_for(svc, docs_dir=str(tmp_path))) as client:
            upload_resp = await client.post(
                "/docs/upload",
                files={"file": ("note.txt", b"hello world", "text/plain")},
            )
            assert upload_resp.status_code == 200
            upload_json = upload_resp.json()
            assert upload_json["name"] == "note.txt"
            trace_id = upload_json["trace_id"]
            assert trace_id

            ingest_resp = await client.post("/ingest", params={"trace_id": trace_id})
            assert ingest_resp.status_code == 200

    turn_records = [
        r for r in caplog.records
        if getattr(r, "stage", None) in ("http", "ingest")
    ]
    assert turn_records, "expected log records from the upload+ingest action"

    trace_ids_seen = {getattr(r, "trace_id", None) for r in turn_records}
    assert trace_ids_seen == {trace_id}, (
        f"expected every upload/ingest log record to carry trace_id={trace_id!r}, "
        f"saw {trace_ids_seen!r}"
    )

    assert any(
        getattr(r, "stage", None) == "http" and "upload ok" in r.getMessage()
        for r in turn_records
    ), "expected a log line from upload_doc"
    assert any(
        getattr(r, "stage", None) == "ingest" and "ingest start" in r.getMessage()
        for r in turn_records
    ), "expected a log line from ingest"

    # Uploaded file must land only in the isolated tmp docs_dir, never the
    # repo's real docs/ folder.
    assert (tmp_path / "note.txt").is_file()


async def test_handle_ingest_missing_trace_id_generates_fresh_one(caplog):
    install_trace_id_filter()
    svc = await _unreachable_rag_service()

    with caplog.at_level(logging.INFO):
        async with _async_client(_app_for(svc)) as client:
            resp = await client.post("/ingest")

    assert resp.status_code == 200

    warning_records = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and getattr(r, "stage", None) == "ingest"
    ]
    assert warning_records, "expected a warning logged for the missing trace_id"

    ingest_records = [r for r in caplog.records if getattr(r, "stage", None) == "ingest"]
    trace_ids_seen = {getattr(r, "trace_id", None) for r in ingest_records}
    assert len(trace_ids_seen) == 1
    generated_trace_id = next(iter(trace_ids_seen))
    assert generated_trace_id, "expected a freshly generated trace_id, not empty/None"

    assert trace_id_var.get() is None


# ---- Ticket 2: capture the happy-path Trace to Postgres ----
#
# Unlike the tests above, this drives a real chat turn all the way through
# rewrite/retrieve/generate. There's no live Ollama/LLM provider available
# in CI, so embed_client/store/provider_client are faked at the same
# duck-typed seam tests/test_rag.py already uses (RagService only calls
# .embed()/.search()/.stream_answer() — it doesn't care whether the object
# behind those calls is a real OllamaClient/PostgresStore/
# OpenAICompatibleClient). The Postgres write itself is real (dev Postgres
# at localhost:5433), per the spec's testing decisions — a capture write
# only means something if it actually lands.

_TRACES_DSN = "postgresql://handbook:handbook@localhost:5433/handbook"


class _FakeEmbedClient:
    async def embed(self, model: str, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _FakeRetrievalStore:
    async def search(self, collection: str, vector: list[float], limit: int) -> list[SearchResult]:
        return [SearchResult(text="Attendance policy requires 80% presence.", page=12, score=0.87)]


class _FakeProviderClient:
    async def stream_answer(self, api_key: str, model: str, prompt: str):
        for token in ["Attendance ", "policy: ", "80% presence required."]:
            yield token


async def test_chat_stream_happy_path_writes_trace_with_all_three_steps():
    pool = await asyncpg.create_pool(dsn=_TRACES_DSN, min_size=0, max_size=2)
    trace_store = TraceStore(pool)
    await trace_store.ensure_schema()

    svc = RagService(
        store=_FakeRetrievalStore(),
        embed_client=_FakeEmbedClient(),
        collection="handbook_chunks",
        top_k=10,
        pdf_path="unused.pdf",
    )
    provider_clients = {"groq": _FakeProviderClient()}
    api_keys = {"GROQ_API_KEY": "test-key"}

    app = _app_for(svc, provider_clients, api_keys, trace_store=trace_store)

    trace_id = None
    try:
        async with _async_client(app) as client:
            start_resp = await client.post(
                "/chat/start",
                data={"question": "What is the attendance policy?", "model_id": "groq_llama31_8b"},
            )
            assert start_resp.status_code == 200
            trace_id = _extract_trace_id_from_start_body(start_resp.text)

            stream_resp = await client.get(
                "/chat/stream",
                params={
                    "question": "What is the attendance policy?",
                    "model_id": "groq_llama31_8b",
                    "trace_id": trace_id,
                },
            )

        assert stream_resp.status_code == 200
        assert "event: done" in stream_resp.text

        row = await pool.fetchrow(
            "SELECT question, status, note, tags, steps FROM traces WHERE trace_id = $1",
            trace_id,
        )
        assert row is not None, "expected the chat turn to have written a Trace row"
        assert row["question"] == "What is the attendance policy?"
        assert row["status"] is None
        assert row["note"] is None
        assert list(row["tags"]) == []

        steps = json.loads(row["steps"])
        assert [s["type"] for s in steps] == ["rewrite", "retrieve", "generate"]

        rewrite_step, retrieve_step, generate_step = steps
        assert rewrite_step["status"] == "not_configured"

        assert len(retrieve_step["results"]) == 1
        assert retrieve_step["results"][0]["text"] == "Attendance policy requires 80% presence."
        assert retrieve_step["results"][0]["page"] == 12

        assert "Attendance policy requires 80% presence." in generate_step["prompt"]
        assert generate_step["output"] == "Attendance policy: 80% presence required."
    finally:
        if trace_id:
            await pool.execute("DELETE FROM traces WHERE trace_id = $1", trace_id)
        await pool.close()


class _FakeFailingProviderClient:
    async def stream_answer(self, api_key: str, model: str, prompt: str):
        yield "partial answer "
        raise RuntimeError("connection dropped mid-stream")


class _FakeEmptyRetrievalStore:
    async def search(self, collection: str, vector: list[float], limit: int) -> list[SearchResult]:
        return []


# ---- Ticket 3: capture failure paths ----
#
# Same fakes/seam as ticket 2's happy-path test above, plus a store that
# always returns empty results (empty retrieval) and a rewriter LLM client
# that never produces valid JSON (rewrite-LLM-failure) — driving each
# failure deterministically through the real HTTP/ASGI seam, per the spec's
# testing decisions.


async def test_chat_stream_mid_generation_failure_writes_trace_with_error_on_generate_step():
    # ticket 3: partial/failed turns must still produce a Trace (spec's
    # "Failures are captured, not dropped") — this supersedes ticket 2's
    # original "no partial Trace" behavior for this same failure.
    pool = await asyncpg.create_pool(dsn=_TRACES_DSN, min_size=0, max_size=2)
    trace_store = TraceStore(pool)
    await trace_store.ensure_schema()

    svc = RagService(
        store=_FakeRetrievalStore(),
        embed_client=_FakeEmbedClient(),
        collection="handbook_chunks",
        top_k=10,
        pdf_path="unused.pdf",
    )
    provider_clients = {"groq": _FakeFailingProviderClient()}
    api_keys = {"GROQ_API_KEY": "test-key"}

    app = _app_for(svc, provider_clients, api_keys, trace_store=trace_store)

    trace_id = None
    try:
        async with _async_client(app) as client:
            start_resp = await client.post(
                "/chat/start",
                data={"question": "What is the attendance policy?", "model_id": "groq_llama31_8b"},
            )
            trace_id = _extract_trace_id_from_start_body(start_resp.text)

            stream_resp = await client.get(
                "/chat/stream",
                params={
                    "question": "What is the attendance policy?",
                    "model_id": "groq_llama31_8b",
                    "trace_id": trace_id,
                },
            )

        assert stream_resp.status_code == 200
        assert "event: streamerror" in stream_resp.text

        row = await pool.fetchrow("SELECT steps FROM traces WHERE trace_id = $1", trace_id)
        assert row is not None, "a mid-generation failure must still persist a Trace"

        steps = json.loads(row["steps"])
        assert [s["type"] for s in steps] == ["rewrite", "retrieve", "generate"]
        rewrite_step, retrieve_step, generate_step = steps
        assert rewrite_step["status"] == "not_configured"
        assert retrieve_step["error"] is None
        assert generate_step["output"] == "partial answer "
        assert "connection dropped mid-stream" in generate_step["error"]
    finally:
        if trace_id:
            await pool.execute("DELETE FROM traces WHERE trace_id = $1", trace_id)
        await pool.close()


async def test_chat_stream_empty_retrieval_writes_trace_with_error_on_retrieve_step():
    pool = await asyncpg.create_pool(dsn=_TRACES_DSN, min_size=0, max_size=2)
    trace_store = TraceStore(pool)
    await trace_store.ensure_schema()

    svc = RagService(
        store=_FakeEmptyRetrievalStore(),
        embed_client=_FakeEmbedClient(),
        collection="handbook_chunks",
        top_k=10,
        pdf_path="unused.pdf",
    )
    provider_clients = {"groq": _FakeProviderClient()}
    api_keys = {"GROQ_API_KEY": "test-key"}

    app = _app_for(svc, provider_clients, api_keys, trace_store=trace_store)

    trace_id = None
    try:
        async with _async_client(app) as client:
            start_resp = await client.post(
                "/chat/start",
                data={"question": "What is the attendance policy?", "model_id": "groq_llama31_8b"},
            )
            trace_id = _extract_trace_id_from_start_body(start_resp.text)

            stream_resp = await client.get(
                "/chat/stream",
                params={
                    "question": "What is the attendance policy?",
                    "model_id": "groq_llama31_8b",
                    "trace_id": trace_id,
                },
            )

        assert stream_resp.status_code == 200
        assert "event: streamerror" in stream_resp.text

        row = await pool.fetchrow("SELECT steps FROM traces WHERE trace_id = $1", trace_id)
        assert row is not None, "empty retrieval must still persist a Trace"

        steps = json.loads(row["steps"])
        assert [s["type"] for s in steps] == ["rewrite", "retrieve"]
        rewrite_step, retrieve_step = steps
        assert rewrite_step["status"] == "not_configured"
        assert retrieve_step["results"] == []
        assert "no context found" in retrieve_step["error"]
    finally:
        if trace_id:
            await pool.execute("DELETE FROM traces WHERE trace_id = $1", trace_id)
        await pool.close()


class _FakeBadJsonRewriterClient:
    async def complete(self, api_key: str, model: str, temperature: float, messages) -> str:
        return "not json"


async def test_chat_stream_rewrite_llm_failure_falls_back_and_completes_turn():
    pool = await asyncpg.create_pool(dsn=_TRACES_DSN, min_size=0, max_size=2)
    trace_store = TraceStore(pool)
    await trace_store.ensure_schema()

    svc = RagService(
        store=_FakeRetrievalStore(),
        embed_client=_FakeEmbedClient(),
        collection="handbook_chunks",
        top_k=10,
        pdf_path="unused.pdf",
    )
    svc.set_query_rewriter(_FakeBadJsonRewriterClient(), "rewrite-key", "rewrite-model")
    provider_clients = {"groq": _FakeProviderClient()}
    api_keys = {"GROQ_API_KEY": "test-key"}

    app = _app_for(svc, provider_clients, api_keys, trace_store=trace_store)

    trace_id = None
    try:
        async with _async_client(app) as client:
            start_resp = await client.post(
                "/chat/start",
                data={"question": "What is the attendance policy?", "model_id": "groq_llama31_8b"},
            )
            trace_id = _extract_trace_id_from_start_body(start_resp.text)

            stream_resp = await client.get(
                "/chat/stream",
                params={
                    "question": "What is the attendance policy?",
                    "model_id": "groq_llama31_8b",
                    "trace_id": trace_id,
                },
            )

        assert stream_resp.status_code == 200
        assert "event: done" in stream_resp.text

        row = await pool.fetchrow("SELECT steps FROM traces WHERE trace_id = $1", trace_id)
        assert row is not None

        steps = json.loads(row["steps"])
        assert [s["type"] for s in steps] == ["rewrite", "retrieve", "generate"]
        rewrite_step, retrieve_step, generate_step = steps
        assert rewrite_step["status"] == "failed_fallback"
        assert rewrite_step["original_question"] == "What is the attendance policy?"
        assert len(retrieve_step["results"]) == 1
        assert generate_step["output"] == "Attendance policy: 80% presence required."
    finally:
        if trace_id:
            await pool.execute("DELETE FROM traces WHERE trace_id = $1", trace_id)
        await pool.close()


# ---- Ticket 4: review UI (thread list, detail, annotate) ----
#
# Same _TRACES_DSN seam as tickets 2/3, plus the spec's own prescribed shape
# for these tests: drive a full chat turn for fixture data, then verify
# through GET /traces and GET /traces/{trace_id} rather than querying
# Postgres directly (docs/evals/phase1-spec.md Testing Decisions).


async def _drive_chat_turn(client: AsyncClient, question: str, model_id: str) -> str:
    start_resp = await client.post("/chat/start", data={"question": question, "model_id": model_id})
    assert start_resp.status_code == 200
    trace_id = _extract_trace_id_from_start_body(start_resp.text)

    stream_resp = await client.get(
        "/chat/stream",
        params={"question": question, "model_id": model_id, "trace_id": trace_id},
    )
    assert stream_resp.status_code == 200
    return trace_id


async def test_traces_list_shows_recent_trace():
    pool = await asyncpg.create_pool(dsn=_TRACES_DSN, min_size=0, max_size=2)
    trace_store = TraceStore(pool)
    await trace_store.ensure_schema()

    svc = RagService(
        store=_FakeRetrievalStore(),
        embed_client=_FakeEmbedClient(),
        collection="handbook_chunks",
        top_k=10,
        pdf_path="unused.pdf",
    )
    provider_clients = {"groq": _FakeProviderClient()}
    api_keys = {"GROQ_API_KEY": "test-key"}
    app = _app_for(svc, provider_clients, api_keys, trace_store=trace_store)

    trace_id = None
    try:
        async with _async_client(app) as client:
            trace_id = await _drive_chat_turn(client, "What is the attendance policy?", "groq_llama31_8b")

            list_resp = await client.get("/traces")

        assert list_resp.status_code == 200
        assert trace_id in list_resp.text
        assert "What is the attendance policy?" in list_resp.text
    finally:
        if trace_id:
            await pool.execute("DELETE FROM traces WHERE trace_id = $1", trace_id)
        await pool.close()


async def test_traces_list_empty_state():
    # The shared dev Postgres carries real eval traces (see docs/evals/), so this
    # can't assert on an empty `public.traces` table. Give TraceStore its own
    # throwaway schema instead — genuine isolation, not a table truncate that
    # would risk eval data.
    schema = f"test_empty_{uuid.uuid4().hex[:8]}"
    admin_conn = await asyncpg.connect(dsn=_TRACES_DSN)
    try:
        await admin_conn.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await admin_conn.close()

    # `server_settings` (a connection startup parameter), not an `init`
    # callback with a runtime SET — asyncpg's pool issues RESET ALL when a
    # connection is released back to the pool, which would silently undo a
    # session-level SET search_path on the next acquire.
    pool = await asyncpg.create_pool(
        dsn=_TRACES_DSN, min_size=0, max_size=2, server_settings={"search_path": schema}
    )
    trace_store = TraceStore(pool)
    await trace_store.ensure_schema()
    svc = await _unreachable_rag_service()
    app = _app_for(svc, trace_store=trace_store)

    try:
        async with _async_client(app) as client:
            list_resp = await client.get("/traces")

        assert list_resp.status_code == 200
        assert "no traces" in list_resp.text.lower()
    finally:
        await pool.close()
        admin_conn = await asyncpg.connect(dsn=_TRACES_DSN)
        try:
            await admin_conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            await admin_conn.close()


async def test_trace_detail_renders_steps_and_not_configured_rewrite_marker():
    pool = await asyncpg.create_pool(dsn=_TRACES_DSN, min_size=0, max_size=2)
    trace_store = TraceStore(pool)
    await trace_store.ensure_schema()

    svc = RagService(
        store=_FakeRetrievalStore(),
        embed_client=_FakeEmbedClient(),
        collection="handbook_chunks",
        top_k=10,
        pdf_path="unused.pdf",
    )
    provider_clients = {"groq": _FakeProviderClient()}
    api_keys = {"GROQ_API_KEY": "test-key"}
    app = _app_for(svc, provider_clients, api_keys, trace_store=trace_store)

    trace_id = None
    try:
        async with _async_client(app) as client:
            trace_id = await _drive_chat_turn(client, "What is the attendance policy?", "groq_llama31_8b")

            detail_resp = await client.get(f"/traces/{trace_id}")

        assert detail_resp.status_code == 200
        body = detail_resp.text
        assert "What is the attendance policy?" in body
        assert "not configured" in body.lower() or "not_configured" in body.lower()
        assert "Attendance policy requires 80% presence." in body
        assert "12" in body  # page number
        assert "Attendance policy: 80% presence required." in body  # generate output
    finally:
        if trace_id:
            await pool.execute("DELETE FROM traces WHERE trace_id = $1", trace_id)
        await pool.close()


async def test_trace_detail_renders_failed_fallback_rewrite_marker():
    pool = await asyncpg.create_pool(dsn=_TRACES_DSN, min_size=0, max_size=2)
    trace_store = TraceStore(pool)
    await trace_store.ensure_schema()

    svc = RagService(
        store=_FakeRetrievalStore(),
        embed_client=_FakeEmbedClient(),
        collection="handbook_chunks",
        top_k=10,
        pdf_path="unused.pdf",
    )
    svc.set_query_rewriter(_FakeBadJsonRewriterClient(), "rewrite-key", "rewrite-model")
    provider_clients = {"groq": _FakeProviderClient()}
    api_keys = {"GROQ_API_KEY": "test-key"}
    app = _app_for(svc, provider_clients, api_keys, trace_store=trace_store)

    trace_id = None
    try:
        async with _async_client(app) as client:
            trace_id = await _drive_chat_turn(client, "What is the attendance policy?", "groq_llama31_8b")

            detail_resp = await client.get(f"/traces/{trace_id}")

        assert detail_resp.status_code == 200
        assert "failed" in detail_resp.text.lower()
    finally:
        if trace_id:
            await pool.execute("DELETE FROM traces WHERE trace_id = $1", trace_id)
        await pool.close()


async def test_trace_detail_unknown_trace_id_returns_404():
    pool = await asyncpg.create_pool(dsn=_TRACES_DSN, min_size=0, max_size=2)
    trace_store = TraceStore(pool)
    await trace_store.ensure_schema()
    svc = await _unreachable_rag_service()
    app = _app_for(svc, trace_store=trace_store)

    try:
        async with _async_client(app) as client:
            resp = await client.get("/traces/does-not-exist")
        assert resp.status_code == 404
    finally:
        await pool.close()


async def test_trace_detail_escapes_question_html():
    pool = await asyncpg.create_pool(dsn=_TRACES_DSN, min_size=0, max_size=2)
    trace_store = TraceStore(pool)
    await trace_store.ensure_schema()

    trace_id = "escape-test-trace"
    try:
        await trace_store.write(trace_id, "<script>alert(1)</script>", [])

        svc = await _unreachable_rag_service()
        app = _app_for(svc, trace_store=trace_store)
        async with _async_client(app) as client:
            resp = await client.get(f"/traces/{trace_id}")

        assert resp.status_code == 200
        assert "<script>alert(1)</script>" not in resp.text
        assert "&lt;script&gt;" in resp.text
    finally:
        await pool.execute("DELETE FROM traces WHERE trace_id = $1", trace_id)
        await pool.close()


async def test_annotate_round_trip_persists_and_reflects_in_detail():
    pool = await asyncpg.create_pool(dsn=_TRACES_DSN, min_size=0, max_size=2)
    trace_store = TraceStore(pool)
    await trace_store.ensure_schema()

    svc = RagService(
        store=_FakeRetrievalStore(),
        embed_client=_FakeEmbedClient(),
        collection="handbook_chunks",
        top_k=10,
        pdf_path="unused.pdf",
    )
    provider_clients = {"groq": _FakeProviderClient()}
    api_keys = {"GROQ_API_KEY": "test-key"}
    app = _app_for(svc, provider_clients, api_keys, trace_store=trace_store)

    trace_id = None
    try:
        async with _async_client(app) as client:
            trace_id = await _drive_chat_turn(client, "What is the attendance policy?", "groq_llama31_8b")

            annotate_resp = await client.post(
                f"/traces/{trace_id}/annotate",
                data={"status": "PASS", "note": "Looks correct.", "tags": "attendance, good"},
            )
            assert annotate_resp.status_code == 303
            assert annotate_resp.headers["location"] == f"/traces/{trace_id}"

            detail_resp = await client.get(f"/traces/{trace_id}")

        row = await pool.fetchrow(
            "SELECT status, note, tags FROM traces WHERE trace_id = $1", trace_id
        )
        assert row["status"] == "PASS"
        assert row["note"] == "Looks correct."
        assert list(row["tags"]) == ["attendance", "good"]

        body = detail_resp.text
        assert "PASS" in body
        assert "Looks correct." in body
        assert "attendance" in body
        assert "good" in body
    finally:
        if trace_id:
            await pool.execute("DELETE FROM traces WHERE trace_id = $1", trace_id)
        await pool.close()


async def test_annotate_unknown_trace_id_returns_404():
    pool = await asyncpg.create_pool(dsn=_TRACES_DSN, min_size=0, max_size=2)
    trace_store = TraceStore(pool)
    await trace_store.ensure_schema()
    svc = await _unreachable_rag_service()
    app = _app_for(svc, trace_store=trace_store)

    try:
        async with _async_client(app) as client:
            resp = await client.post(
                "/traces/does-not-exist/annotate",
                data={"status": "PASS", "note": "", "tags": ""},
            )
        assert resp.status_code == 404
    finally:
        await pool.close()


async def test_annotate_invalid_status_returns_400():
    pool = await asyncpg.create_pool(dsn=_TRACES_DSN, min_size=0, max_size=2)
    trace_store = TraceStore(pool)
    await trace_store.ensure_schema()

    svc = RagService(
        store=_FakeRetrievalStore(),
        embed_client=_FakeEmbedClient(),
        collection="handbook_chunks",
        top_k=10,
        pdf_path="unused.pdf",
    )
    provider_clients = {"groq": _FakeProviderClient()}
    api_keys = {"GROQ_API_KEY": "test-key"}
    app = _app_for(svc, provider_clients, api_keys, trace_store=trace_store)

    trace_id = None
    try:
        async with _async_client(app) as client:
            trace_id = await _drive_chat_turn(client, "What is the attendance policy?", "groq_llama31_8b")

            resp = await client.post(
                f"/traces/{trace_id}/annotate",
                data={"status": "MAYBE", "note": "", "tags": ""},
            )
        assert resp.status_code == 400
    finally:
        if trace_id:
            await pool.execute("DELETE FROM traces WHERE trace_id = $1", trace_id)
        await pool.close()
