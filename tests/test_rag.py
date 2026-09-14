"""Unit tests for app/rag.py, translated from the business logic in
internal/rag/service.go and internal/rag/rewrite.go.

These are mock-level tests (fake embed/store/llm clients) — they do not hit
real Ollama/Postgres/LLM services. The live end-to-end characterization run
against the real handbook PDF + Ollama + Postgres, compared against
internal/rag/testdata/characterization_golden.json, was run separately
(see the ticket-10 report) and is not part of this suite because it needs
live services.
"""

from unittest.mock import AsyncMock

import pytest

from app.chunk import Chunk
from app.pdfextract import PageText
from app.rag import (
    RagError,
    RagService,
    RewriteAction,
    RewriteError,
    RewriteResult,
    _fallback_rewrite_query,
    _is_clearly_off_topic,
    _rewrite_query_with_llm,
    _should_fallback_to_rewrite,
)
from app.store import SearchResult


def make_service(embed_client=None, store=None, **kwargs):
    embed_client = embed_client or AsyncMock()
    store = store or AsyncMock()
    defaults = dict(
        store=store,
        embed_client=embed_client,
        collection="handbook_chunks",
        top_k=10,
        pdf_path="/tmp/fake.pdf",
        chunk_words=180,
    )
    defaults.update(kwargs)
    return RagService(**defaults), embed_client, store


# --- build_prompt: happy path, prompt assembly ---


async def test_build_prompt_assembles_context_and_matches_go_template():
    embed_client = AsyncMock()
    embed_client.embed.return_value = [0.1, 0.2]
    store = AsyncMock()
    store.search.return_value = [
        SearchResult(text="First chunk text.", page=5, score=0.9),
        SearchResult(text="Second chunk text.", page=12, score=0.8),
    ]
    svc, _, _ = make_service(embed_client=embed_client, store=store)

    prompt, results = await svc.build_prompt("What is the GPA policy?")

    assert results == store.search.return_value
    embed_client.embed.assert_awaited_once_with(
        "qllama/bge-small-en-v1.5", "What is the GPA policy?"
    )
    store.search.assert_awaited_once_with("handbook_chunks", [0.1, 0.2], 10)

    expected_context = "[Page 5]: First chunk text.\n\n[Page 12]: Second chunk text.\n\n"
    assert "Context (retrieved from handbook):\n\t" + expected_context in prompt
    assert "Question: What is the GPA policy?" in prompt
    assert prompt.startswith("Context (retrieved from handbook):\n")
    assert prompt.endswith(
        "Do not cite for greetings, simple clarifications, or conversational replies.]\n\t"
    )


async def test_build_prompt_raises_rag_error_on_no_results():
    embed_client = AsyncMock()
    embed_client.embed.return_value = [0.1]
    store = AsyncMock()
    store.search.return_value = []
    svc, _, _ = make_service(embed_client=embed_client, store=store)

    with pytest.raises(RagError, match="no context found; run ingestion first"):
        await svc.build_prompt("anything")


# --- build_prompt: rewrite integration ---


async def test_build_prompt_uses_rewritten_query_for_embedding():
    embed_client = AsyncMock()
    embed_client.embed.return_value = [0.1]
    store = AsyncMock()
    store.search.return_value = [SearchResult(text="t", page=1, score=0.5)]
    svc, _, _ = make_service(embed_client=embed_client, store=store)

    llm_client = AsyncMock()
    llm_client.complete.return_value = (
        '{"action":"rewrite_for_retrieval","rewritten_query":"gpa policy standing",'
        '"assistant_message":""}'
    )
    svc.set_query_rewriter(llm_client, "key", "model")

    await svc.build_prompt("what's the min gpa i need?")

    embed_client.embed.assert_awaited_once_with(
        "qllama/bge-small-en-v1.5", "gpa policy standing"
    )


async def test_build_prompt_graceful_reply_short_circuits_without_embed_or_search():
    embed_client = AsyncMock()
    store = AsyncMock()
    svc, _, _ = make_service(embed_client=embed_client, store=store)

    llm_client = AsyncMock()
    llm_client.complete.return_value = (
        '{"action":"graceful_reply","rewritten_query":"",'
        '"assistant_message":"Hi! Ask me about handbook topics."}'
    )
    svc.set_query_rewriter(llm_client, "key", "model")

    message, results = await svc.build_prompt("hi")

    assert message == "Hi! Ask me about handbook topics."
    assert results == []
    embed_client.embed.assert_not_awaited()
    store.search.assert_not_awaited()


async def test_build_prompt_falls_back_to_original_question_when_rewrite_fails():
    embed_client = AsyncMock()
    embed_client.embed.return_value = [0.1]
    store = AsyncMock()
    store.search.return_value = [SearchResult(text="t", page=1, score=0.5)]
    svc, _, _ = make_service(embed_client=embed_client, store=store)

    llm_client = AsyncMock()
    llm_client.complete.return_value = "not json"
    svc.set_query_rewriter(llm_client, "key", "model")

    prompt, results = await svc.build_prompt("original question")

    embed_client.embed.assert_awaited_once_with(
        "qllama/bge-small-en-v1.5", "original question"
    )
    assert results == store.search.return_value


# --- set_query_rewriter ---


def test_set_query_rewriter_clears_when_api_key_or_model_blank():
    svc, _, _ = make_service()
    llm_client = AsyncMock()

    svc.set_query_rewriter(llm_client, "  ", "model")
    assert svc._rewriter is None

    svc.set_query_rewriter(llm_client, "key", "  ")
    assert svc._rewriter is None

    svc.set_query_rewriter(None, "key", "model")
    assert svc._rewriter is None

    svc.set_query_rewriter(llm_client, "key", "model")
    assert svc._rewriter is not None


# --- ingest ---


def test_default_chunk_words_matches_go_production_default():
    # Go's cmd/server and cmd/ingest both construct via rag.NewService, which
    # hardcodes chunkWords: 180 (internal/rag/service.go). A caller that omits
    # chunk_words — app/cli/ingest.py, app/main.py's bootstrap() — must get the
    # same 180, not chunk.build's unrelated <= 0 -> 350 fallback. Construct
    # RagService directly (not via make_service, which always overrides
    # chunk_words=180 explicitly and so would mask a regression here).
    svc = RagService(
        store=AsyncMock(),
        embed_client=AsyncMock(),
        collection="handbook_chunks",
        top_k=10,
        pdf_path="/tmp/fake.pdf",
    )
    assert svc._chunk_words == 180


async def test_ingest_extracts_chunks_embeds_and_upserts(monkeypatch):
    embed_client = AsyncMock()
    embed_client.embed.return_value = [0.1, 0.2]
    store = AsyncMock()
    svc, _, _ = make_service(embed_client=embed_client, store=store, chunk_words=180)

    async def fake_extract_by_page(path):
        assert path == "/tmp/fake.pdf"
        return [PageText(page=1, text="word " * 200), PageText(page=2, text="more words")]

    monkeypatch.setattr("app.rag.extract_by_page", fake_extract_by_page)

    count = await svc.ingest()

    # 200 words at 180/chunk -> 2 chunks for page 1, 1 chunk for page 2 -> 3 total.
    assert count == 3
    store.ensure_schema.assert_awaited_once_with("handbook_chunks")
    assert embed_client.embed.await_count == 3
    assert store.upsert.await_count == 3
    first_call = store.upsert.await_args_list[0]
    assert first_call.args[0] == "handbook_chunks"
    assert first_call.args[1] == 0  # first chunk id


# --- _rewrite_query_with_llm ---


async def test_rewrite_retries_once_on_bad_json_then_succeeds():
    llm_client = AsyncMock()
    llm_client.complete.side_effect = [
        "not json",
        '{"action":"rewrite_for_retrieval","rewritten_query":"q","assistant_message":""}',
    ]

    result = await _rewrite_query_with_llm(llm_client, "key", "model", "question")

    assert result.action == RewriteAction.REWRITE_FOR_RETRIEVAL
    assert result.rewritten_query == "q"
    assert llm_client.complete.await_count == 2


async def test_rewrite_raises_after_exhausting_two_attempts():
    llm_client = AsyncMock()
    llm_client.complete.side_effect = ["not json", "still not json"]

    with pytest.raises(RewriteError):
        await _rewrite_query_with_llm(llm_client, "key", "model", "question")

    assert llm_client.complete.await_count == 2


async def test_rewrite_ask_better_question_falls_back_to_retrieval_for_ordinary_query():
    llm_client = AsyncMock()
    llm_client.complete.return_value = (
        '{"action":"ask_better_question","rewritten_query":"",'
        '"assistant_message":"please ask a handbook question"}'
    )

    result = await _rewrite_query_with_llm(
        llm_client, "key", "model", "some ordinary handbook-ish question"
    )

    assert result.action == RewriteAction.REWRITE_FOR_RETRIEVAL
    assert result.rewritten_query == "some ordinary handbook-ish question"


async def test_rewrite_ask_better_question_kept_for_clearly_off_topic_query():
    llm_client = AsyncMock()
    llm_client.complete.return_value = (
        '{"action":"ask_better_question","rewritten_query":"",'
        '"assistant_message":"please ask a handbook question"}'
    )

    result = await _rewrite_query_with_llm(llm_client, "key", "model", "tell me a joke")

    assert result.action == RewriteAction.ASK_BETTER_QUESTION
    assert result.assistant_message == "please ask a handbook question"


# --- helper functions ---


def test_is_clearly_off_topic():
    assert _is_clearly_off_topic("tell me a joke")
    assert _is_clearly_off_topic("what's the weather today")
    assert not _is_clearly_off_topic("what is the gpa policy")


def test_should_fallback_to_rewrite():
    assert _should_fallback_to_rewrite("some question") is True
    assert _should_fallback_to_rewrite("   ") is False
    assert _should_fallback_to_rewrite("tell me a joke") is False


def test_fallback_rewrite_query_short_query_gets_suffix():
    assert _fallback_rewrite_query("scholarship") == "scholarship university handbook policy"


def test_fallback_rewrite_query_strips_punctuation_and_whitespace():
    assert _fallback_rewrite_query("  when is add drop?  ") == "when is add drop"


def test_fallback_rewrite_query_empty_input():
    assert _fallback_rewrite_query("   ") == "university handbook policy requirement"
