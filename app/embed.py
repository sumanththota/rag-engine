import httpx


class EmbedError(Exception):
    """Raised for any embedding failure: empty input, HTTP failure, empty
    embedding, or exhausted shrink retries."""


class _RetryableEmbedError(EmbedError):
    """Internal signal from _embed_once that a failure is retryable (500 +
    "context length" in body). Only embed() catches this; it never escapes
    to callers — mirrors Go's embedOnce (vec, retry, err) return via a type
    instead of a bool, since PYTHON_INTERFACES.md rules out tuple returns."""


class OllamaClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=60.0)

    async def embed(self, model: str, text: str) -> list[float]:
        trimmed = text.strip()
        if not trimmed:
            raise EmbedError("empty text provided for embedding")

        current = trimmed
        last_err: EmbedError | None = None
        for _ in range(4):
            try:
                return await self._embed_once(model, current)
            except _RetryableEmbedError as err:
                last_err = err
                current = _shrink_text(current)
                if not current:
                    raise last_err from None

        raise EmbedError(
            "embedding failed after retries due to context-length constraints"
        ) from last_err

    async def healthy(self) -> None:
        try:
            resp = await self._client.get(f"{self._base_url}/api/tags")
        except httpx.HTTPError as err:
            raise EmbedError(f"ollama health check request failed: {err}") from err

        if not (200 <= resp.status_code < 300):
            body = resp.content[:2048].decode("utf-8", errors="replace").strip()
            raise EmbedError(
                f"ollama health check failed status={resp.status_code} body={body}"
            )

    async def _embed_once(self, model: str, text: str) -> list[float]:
        try:
            resp = await self._client.post(
                f"{self._base_url}/api/embeddings",
                json={"model": model, "prompt": text},
            )
        except httpx.HTTPError as err:
            raise EmbedError(f"send embed request: {err}") from err

        if not (200 <= resp.status_code < 300):
            body_text = resp.text
            if resp.status_code == 500 and "context length" in body_text.lower():
                raise _RetryableEmbedError(
                    f"ollama embed input too long: {body_text}"
                )
            raise EmbedError(
                f"ollama embed failed with status {resp.status_code}: {body_text.strip()}"
            )

        try:
            payload = resp.json()
        except ValueError as err:
            raise EmbedError(f"decode embed response: {err}") from err

        embedding = payload.get("embedding") or []
        if not embedding:
            raise EmbedError("empty embedding returned by ollama")

        return embedding


def _shrink_text(text: str) -> str:
    words = text.split()
    if len(words) <= 40:
        return ""
    new_len = max(len(words) // 2, 40)
    return " ".join(words[:new_len])
