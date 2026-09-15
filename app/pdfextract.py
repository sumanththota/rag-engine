"""PDF text extraction (from internal/pdfextract/pdf.go)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from pydantic import BaseModel

from app.llamaparse import extract_pages

logger = logging.getLogger("rag.pdfextract")

# Extraction results are cached to disk, keyed by (path, size, mtime, tier,
# source). LlamaParse is a billed API call — without this, every ingest run
# (POST /ingest, cli/ingest.py, and cli/eval.py, which re-ingests on *every*
# invocation regardless of --chunk-words/--top-k) re-uploads and re-parses
# the PDF from scratch. Committed to git deliberately (not gitignored): it's
# a reusable fixture for future migration/eval work, not a throwaway cache.
_CACHE_DIR = Path("docs/pdfextract_cache")


class PdfExtractError(Exception):
    """Raised when neither the LlamaParse path nor the local pypdf fallback
    yields any non-empty pages, or the local fallback fails to read the PDF."""


class PageText(BaseModel):
    page: int
    text: str


def _cache_key(path: str, tier: str, source: str) -> str:
    stat = Path(path).stat()
    raw = f"{Path(path).resolve()}|{stat.st_size}|{int(stat.st_mtime)}|{tier}|{source}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _cache_file(key: str) -> Path:
    return _CACHE_DIR / f"{key}.json"


def _load_cache(key: str) -> list[PageText] | None:
    fp = _cache_file(key)
    if not fp.is_file():
        return None
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        return [PageText.model_validate(p) for p in data["pages"]]
    except Exception as e:
        logger.warning(
            "cache read failed key=%s err=%s — re-extracting", key, e,
            extra={"stage": "ingest"},
        )
        return None


def _save_cache(key: str, pages: list[PageText], source: str, tier: str, path: str) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": source,
        "tier": tier,
        "pdf_path": str(Path(path).resolve()),
        "pages": [p.model_dump() for p in pages],
    }
    _cache_file(key).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def extract_by_page(path: str) -> list[PageText]:
    """Async. Equivalent of Go's ExtractByPage.

    Checks the on-disk cache first (see _CACHE_DIR above) — a hit returns
    immediately with no LlamaParse/pypdf call at all, no billing.

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
    logger.info("start path=%s", path, extra={"stage": "ingest"})

    key = os.environ.get("LLAMA_CLOUD_API_KEY", "").strip()
    source = "llamaparse" if key else "pypdf"
    tier = (os.environ.get("LLAMAPARSE_TIER", "").strip() or "cost_effective") if key else "n/a"

    cache_key = _cache_key(path, tier, source)
    cached = _load_cache(cache_key)
    if cached is not None:
        logger.info(
            "cache hit source=%s pages=%d key=%s — no extraction call made",
            source, len(cached), cache_key,
            extra={"stage": "ingest"},
        )
        return cached
    logger.info(
        "cache miss source=%s key=%s — extracting for real", source, cache_key,
        extra={"stage": "ingest"},
    )

    if key:
        try:
            pages = await extract_pages(key, path, tier)
        except Exception as e:
            logger.error("parser=llamaparse failed err=%s", e, extra={"stage": "ingest"})
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

        logger.info("parser=llamaparse pages=%d tier=%s", len(out), tier, extra={"stage": "ingest"})
        _save_cache(cache_key, out, source, tier, path)
        return out

    try:
        pages = _extract_with_pypdf(path)
    except Exception as e:
        logger.error("parser=pypdf failed err=%s", e, extra={"stage": "ingest"})
        raise PdfExtractError(f"extract text with pypdf: {e}") from e

    logger.info("parser=pypdf pages=%d", len(pages), extra={"stage": "ingest"})
    _save_cache(cache_key, pages, source, tier, path)
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
