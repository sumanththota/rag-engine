import json
from collections.abc import AsyncIterator

import httpx
from pydantic import BaseModel

_SYSTEM_PROMPT = """You are a helpful assistant for the University Student Handbook.
Answer questions using only the provided handbook context in markdown format.
If the answer is not in the context:
1. First check whether the context names a specific office, contact, or URL relevant to the question (e.g. Graduate School, Director of Graduate Studies, department advisor) and point the student there directly.
2. Otherwise respond with: "I couldn't find that in the handbook. For questions like this, your academic advisor or the Director of Graduate Studies is the best place to start."
Never fabricate policies, dates, or procedures."""


class ChatMessage(BaseModel):
    role: str
    content: str


class LLMError(Exception):
    """Raised for any chat-completion failure: non-2xx provider response,
    empty choices array, or response-decode failure."""


class OpenAICompatibleClient:
    def __init__(self, base_url: str, headers: dict[str, str] | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = headers or {}
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
        )

    async def _send_chat_request(
        self, api_key: str, payload: dict
    ) -> httpx.Response:
        headers = {"Content-Type": "application/json"}
        key = api_key.strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        headers.update(self._headers)

        request = self._client.build_request(
            "POST", f"{self._base_url}/chat/completions", json=payload, headers=headers
        )
        response = await self._client.send(request, stream=True)
        if response.status_code < 200 or response.status_code >= 300:
            body = (await response.aread())[:4096]
            await response.aclose()
            text = body.decode("utf-8", errors="replace").strip()
            if text:
                raise LLMError(
                    f"provider returned status {response.status_code}: {text}"
                )
            raise LLMError(f"provider returned status {response.status_code}")
        return response

    async def complete(
        self, api_key: str, model: str, temperature: float, messages: list[ChatMessage]
    ) -> str:
        payload = {
            "model": model,
            "messages": [m.model_dump() for m in messages],
            "temperature": temperature,
            "stream": False,
        }

        response = await self._send_chat_request(api_key, payload)
        try:
            body = await response.aread()
        finally:
            await response.aclose()

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise LLMError(f"decode response: {e}") from e

        choices = data.get("choices") or []
        if not choices:
            raise LLMError("empty choices in completion response")
        content = choices[0].get("message", {}).get("content", "") or ""
        return content.strip()

    async def stream_answer(
        self, api_key: str, model: str, prompt: str
    ) -> AsyncIterator[str]:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "stream": True,
        }

        response = await self._send_chat_request(api_key, payload)
        try:
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue

                data = line[len("data: "):]
                if data == "[DONE]":
                    return

                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                choices = chunk.get("choices") or []
                if not choices:
                    continue

                token = choices[0].get("delta", {}).get("content", "")
                if not token:
                    continue

                yield token
        finally:
            await response.aclose()
