"""PDF text extraction (from internal/pdfextract/pdf.go)."""

from __future__ import annotations

import logging
import os

from pydantic import BaseModel

from app.llamaparse import extract_pages

logger = logging.getLogger("rag.pdfextract")


class PdfExtractError(Exception):
    """Raised when neither the LlamaParse path nor the local pypdf fallback
    yields any non-empty pages, or the local fallback fails to read the PDF."""


class PageText(BaseModel):
    page: int
    text: str


async def extract_by_page(path: str) -> list[PageText]:
    """Async. Equivalent of Go's ExtractByPage.

    If LLAMA_CLOUD_API_KEY is set, delegates to llamaparse.extract_pages()
    (tier from LLAMAPARSE_TIER, default "cost_effective"), applying the same
    page-number fallback (missing/zero page number -> sequential numbering)
    and empty-text-page skipping as the Go version.

    Otherwise falls back to local, in-process pypdf extraction. This is a
    deliberate simplification over the Go version, which shells out to a
    `python3` subprocess running a pypdf/PyPDF2 script; the Python port runs
    pypdf directly in-process instead.

    Raises PdfExtractError if the chosen path yields no non-empty pages.
    """
    logger.info("start path=%s", path)

    key = os.environ.get("LLAMA_CLOUD_API_KEY", "").strip()
    if key:
        tier = os.environ.get("LLAMAPARSE_TIER", "").strip() or "cost_effective"
        try:
            pages = await extract_pages(key, path, tier)
        except Exception as e:
            logger.error("parser=llamaparse failed err=%s", e)
            raise

        out: list[PageText] = []
        for p in pages:
            text = p.text.strip()
            if not text:
                continue
            n = p.number if p.number > 0 else len(out) + 1
            out.append(PageText(page=n, text=text))

        if not out:
            raise PdfExtractError("llamaparse: no non-empty pages")

        logger.info("parser=llamaparse pages=%d tier=%s", len(out), tier)
        return out

    try:
        pages = _extract_with_pypdf(path)
    except Exception as e:
        logger.error("parser=pypdf failed err=%s", e)
        raise PdfExtractError(f"extract text with pypdf: {e}") from e

    logger.info("parser=pypdf pages=%d", len(pages))
    return pages


def _extract_with_pypdf(path: str) -> list[PageText]:
    from pypdf import PdfReader

    reader = PdfReader(path)

    out: list[PageText] = []
    for i, p in enumerate(reader.pages, start=1):
        text = p.extract_text() or ""
        text = " ".join(text.split())
        if text:
            out.append(PageText(page=i, text=text))

    if not out:
        raise PdfExtractError("no extractable text from pypdf parser")

    return out
