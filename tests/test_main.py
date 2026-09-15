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

import asyncpg
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.embed import OllamaClient
from app.llm import OpenAICompatibleClient
from app.main import create_app
from app.rag import RagService
from app.store import PostgresStore


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


def _app_for(rag_service: RagService, provider_clients=None, api_keys=None) -> FastAPI:
    return create_app(
        rag_service=rag_service,
        provider_clients=provider_clients or {},
        api_keys=api_keys or {},
        embed_client=OllamaClient("http://127.0.0.1:1"),
        store=PostgresStore(pool=None),
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
