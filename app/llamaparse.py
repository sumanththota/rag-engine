"""LlamaCloud parse client (from internal/llamaparse/client.go)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
from pydantic import BaseModel

_DEFAULT_BASE_URL = "https://api.cloud.llamaindex.ai"
_POLL_INTERVAL_SECONDS = 1.5
_POLL_TIMEOUT_SECONDS = 25 * 60
_TRUNCATE_LEN = 500


class LlamaParseError(Exception):
    """Raised for any stage failure: missing API key, upload failure, job-start
    failure, poll failure, job FAILED/ERROR status, or a completed job with no
    markdown/text pages."""


class Page(BaseModel):
    number: int
    text: str


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "..."


def _normalize_whitespace(s: str) -> str:
    return " ".join(s.split())


def _base_url() -> str:
    base = os.environ.get("LLAMA_CLOUD_BASE_URL", "").rstrip("/")
    return base or _DEFAULT_BASE_URL


async def extract_pages(api_key: str, pdf_path: str, tier: str = "cost_effective") -> list[Page]:
    if not api_key or not api_key.strip():
        raise LlamaParseError("llamaparse: empty API key")
    if not tier:
        tier = "cost_effective"

    base = _base_url()

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            file_id = await _upload_file(client, base, api_key, pdf_path)
            job_id = await _start_parse_job(client, base, api_key, file_id, tier)
    except LlamaParseError:
        raise
    except Exception as e:
        raise LlamaParseError(f"llamaparse: {e}") from e

    try:
        pages = await _poll_parse_job(base, api_key, job_id)
    except LlamaParseError:
        raise
    except Exception as e:
        raise LlamaParseError(f"llamaparse poll: {e}") from e

    if not pages:
        raise LlamaParseError("llamaparse: no pages in result")
    return pages


async def _upload_file(client: httpx.AsyncClient, base: str, api_key: str, pdf_path: str) -> str:
    path = Path(pdf_path)
    try:
        data = path.read_bytes()
    except OSError as e:
        raise LlamaParseError(f"llamaparse upload: {e}") from e

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    files = {"upload_file": (path.name, data)}
    data_fields = {"purpose": "parse"}

    try:
        resp = await client.post(f"{base}/api/v1/files/", headers=headers, files=files, data=data_fields)
    except httpx.HTTPError as e:
        raise LlamaParseError(f"llamaparse upload: {e}") from e

    if not (200 <= resp.status_code < 300):
        raise LlamaParseError(
            f"llamaparse upload: status {resp.status_code}: {_truncate(resp.text, _TRUNCATE_LEN)}"
        )

    try:
        out = resp.json()
    except ValueError as e:
        raise LlamaParseError(f"llamaparse upload: decode upload response: {e}") from e

    file_id = out.get("id") if isinstance(out, dict) else None
    if not file_id:
        raise LlamaParseError(
            f"llamaparse upload: missing file id in response: {_truncate(resp.text, 300)}"
        )
    return file_id


async def _start_parse_job(
    client: httpx.AsyncClient, base: str, api_key: str, file_id: str, tier: str
) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {"file_id": file_id, "tier": tier, "version": "latest"}

    try:
        resp = await client.post(f"{base}/api/v2/parse", headers=headers, json=payload)
    except httpx.HTTPError as e:
        raise LlamaParseError(f"llamaparse start job: {e}") from e

    if not (200 <= resp.status_code < 300):
        raise LlamaParseError(
            f"llamaparse start job: status {resp.status_code}: {_truncate(resp.text, _TRUNCATE_LEN)}"
        )

    try:
        out = resp.json()
    except ValueError as e:
        raise LlamaParseError(f"llamaparse start job: decode parse start: {e}") from e

    if isinstance(out, dict):
        job_id = out.get("id")
        if job_id:
            return job_id
        job = out.get("job")
        if isinstance(job, dict):
            nested_id = job.get("id")
            if nested_id:
                return nested_id

    raise LlamaParseError(
        f"llamaparse start job: no job id in response: {_truncate(resp.text, 400)}"
    )


async def _poll_parse_job(base: str, api_key: str, job_id: str) -> list[Page]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    url = f"{base}/api/v2/parse/{job_id}?expand=markdown"

    async with asyncio.timeout(_POLL_TIMEOUT_SECONDS):
        async with httpx.AsyncClient(timeout=60.0) as client:
            while True:
                try:
                    resp = await client.get(url, headers=headers)
                except httpx.HTTPError as e:
                    raise LlamaParseError(f"llamaparse poll: {e}") from e

                if not (200 <= resp.status_code < 300):
                    raise LlamaParseError(
                        f"llamaparse poll: poll status {resp.status_code}: "
                        f"{_truncate(resp.text, 400)}"
                    )

                try:
                    parsed = resp.json()
                except ValueError as e:
                    raise LlamaParseError(f"llamaparse poll: decode poll: {e}") from e

                status = _job_status(parsed).upper()

                if status in ("FAILED", "ERROR"):
                    raise LlamaParseError(
                        f"llamaparse job failed: {_truncate(resp.text, 600)}"
                    )
                if status in ("COMPLETED", "COMPLETE", "SUCCESS"):
                    return _to_pages(parsed)

                await asyncio.sleep(_POLL_INTERVAL_SECONDS)


def _job_status(parsed: dict) -> str:
    job = parsed.get("job") if isinstance(parsed, dict) else None
    if isinstance(job, dict):
        job_status = job.get("status")
        if job_status:
            return job_status
    return (parsed.get("status") or "") if isinstance(parsed, dict) else ""


def _to_pages(parsed: dict) -> list[Page]:
    markdown = parsed.get("markdown") if isinstance(parsed, dict) else None
    if isinstance(markdown, dict):
        md_pages = markdown.get("pages") or []
        out: list[Page] = []
        for pg in md_pages:
            text = (pg.get("markdown") or "").strip()
            if not text:
                continue
            out.append(Page(number=pg.get("page_number", 0), text=_normalize_whitespace(text)))
        if out:
            return out

    text_block = parsed.get("text") if isinstance(parsed, dict) else None
    if isinstance(text_block, dict):
        text_pages = text_block.get("pages") or []
        out = []
        for pg in text_pages:
            text = (pg.get("text") or "").strip()
            if not text:
                continue
            out.append(Page(number=pg.get("page_number", 0), text=_normalize_whitespace(text)))
        if out:
            return out

    raise LlamaParseError("llamaparse: completed job has no markdown/text pages")
