"""Characterization tests translated from internal/embed/ollama_test.go.

These lock in OllamaClient's behavior (progressive-shrink retry loop,
context-length detection) to match the Go source. See docs/MIGRATION.md
§3/§6 in handbook-rag-golang.
"""

import httpx
import pytest

from app.embed import EmbedError, OllamaClient, _RetryableEmbedError, _shrink_text


def words_n(n: int) -> str:
    return " ".join(["w"] * n)


def make_client(handler) -> OllamaClient:
    client = OllamaClient("http://mock-ollama")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=60.0
    )
    return client


# --- _shrink_text ---


def test_shrink_text_halves_above_floor():
    got = _shrink_text(words_n(100))
    want = words_n(50)
    assert got == want


def test_shrink_text_clamps_to_floor_of_40():
    # 41 words halved would be 20, but the 40-word floor clamps it to 40.
    got = _shrink_text(words_n(41))
    want = words_n(40)
    assert got == want


def test_shrink_text_empty_when_at_or_below_40_words():
    for n in (0, 1, 40):
        got = _shrink_text(words_n(n))
        assert got == "", f"_shrink_text({n} words) = {got!r}, want empty string"


# --- _embed_once retryability ---


async def test_embed_once_status_500_with_context_length_body_is_retryable():
    cases = [
        "error: context length exceeded",
        "ERROR: CONTEXT LENGTH EXCEEDED",
        "Context Length exceeded the model's limit",
    ]
    for body in cases:
        def handler(request, body=body):
            return httpx.Response(500, text=body)

        client = make_client(handler)
        with pytest.raises(_RetryableEmbedError):
            await client._embed_once("m", "hello")


async def test_embed_once_other_non_2xx_is_not_retryable():
    cases = [
        (500, "internal server error"),
        (400, "context length exceeded"),
        (404, "not found"),
        (503, "context length exceeded"),
    ]
    for status, body in cases:
        def handler(request, status=status, body=body):
            return httpx.Response(status, text=body)

        client = make_client(handler)
        with pytest.raises(EmbedError) as exc_info:
            await client._embed_once("m", "hello")
        assert not isinstance(exc_info.value, _RetryableEmbedError), (
            f"status={status} body={body!r}: expected non-retryable error"
        )


async def test_embed_once_success_returns_vector_no_retry():
    def handler(request):
        return httpx.Response(200, json={"embedding": [0.1, 0.2, 0.3]})

    client = make_client(handler)
    vec = await client._embed_once("m", "hello")
    assert len(vec) == 3


# --- Embed: empty/whitespace input rejected before any HTTP call ---


async def test_embed_rejects_empty_or_whitespace_input_without_http_call():
    for text in ("", "   ", "\t\n  "):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"embedding": [0.1]})

        client = make_client(handler)
        with pytest.raises(EmbedError):
            await client.embed("m", text)
        assert calls == 0, f"input {text!r}: expected no HTTP calls, got {calls}"


# --- Embed: non-retryable error returns immediately ---


async def test_embed_non_retryable_error_returns_immediately():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(400, text="bad request")

    client = make_client(handler)
    with pytest.raises(EmbedError) as exc_info:
        await client.embed("m", "some short prompt")
    assert calls == 1
    assert "status 400" in str(exc_info.value)


# --- Embed: exhausts 4 attempts on persistent context-length errors ---


async def test_embed_stops_after_4_attempts_on_persistent_context_length_error():
    # 1000 words survives 4 successive _shrink_text halvings without ever
    # dropping to the empty-string floor condition, so all 4 attempts are
    # genuinely exhausted rather than short-circuited by an empty shrink result.
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="error: context length exceeded")

    client = make_client(handler)
    with pytest.raises(EmbedError) as exc_info:
        await client.embed("m", words_n(1000))
    assert calls == 4
    assert "failed after retries" in str(exc_info.value)


# sanity check that the mock transport is wired correctly and status codes
# round-trip as expected (guards against a broken test double masking a
# false pass above).


async def test_mock_transport_status_code_sanity():
    def handler(request):
        return httpx.Response(418, text="418")

    client = make_client(handler)
    with pytest.raises(EmbedError) as exc_info:
        await client._embed_once("m", "hello")
    assert not isinstance(exc_info.value, _RetryableEmbedError)
