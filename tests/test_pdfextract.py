"""Unit tests for app/pdfextract.py — the LLAMA_CLOUD_API_KEY routing branch
and the local pypdf fallback, translated from internal/pdfextract/pdf.go's
behavior. LlamaParse's own HTTP upload/poll flow is covered by app/llamaparse.py
directly, not re-tested here.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.llamaparse import Page
from app.pdfextract import PdfExtractError, PageText, extract_by_page


async def test_extract_by_page_uses_llamaparse_when_api_key_set(monkeypatch):
    monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "test-key")
    monkeypatch.setenv("LLAMAPARSE_TIER", "agentic")

    fake_extract = AsyncMock(
        return_value=[Page(number=1, text=" a "), Page(number=2, text="b")]
    )
    with patch("app.pdfextract.extract_pages", fake_extract):
        pages = await extract_by_page("/tmp/fake.pdf")

    fake_extract.assert_awaited_once_with("test-key", "/tmp/fake.pdf", "agentic")
    assert pages == [PageText(page=1, text="a"), PageText(page=2, text="b")]


async def test_extract_by_page_llamaparse_defaults_tier_and_backfills_page_number(
    monkeypatch,
):
    monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "test-key")
    monkeypatch.delenv("LLAMAPARSE_TIER", raising=False)

    # number=0 (or missing) must fall back to sequential position, matching
    # Go's `if n <= 0 { n = len(out) + 1 }`.
    fake_extract = AsyncMock(
        return_value=[Page(number=0, text="first"), Page(number=0, text="second")]
    )
    with patch("app.pdfextract.extract_pages", fake_extract):
        pages = await extract_by_page("/tmp/fake.pdf")

    fake_extract.assert_awaited_once_with("test-key", "/tmp/fake.pdf", "cost_effective")
    assert pages == [PageText(page=1, text="first"), PageText(page=2, text="second")]


async def test_extract_by_page_llamaparse_skips_empty_pages_and_raises_if_all_empty(
    monkeypatch,
):
    monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "test-key")

    fake_extract = AsyncMock(return_value=[Page(number=1, text="   ")])
    with patch("app.pdfextract.extract_pages", fake_extract):
        with pytest.raises(PdfExtractError, match="no non-empty pages"):
            await extract_by_page("/tmp/fake.pdf")


async def test_extract_by_page_falls_back_to_pypdf_when_no_api_key(monkeypatch):
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)

    fake_page = MagicMock()
    fake_page.extract_text.return_value = "hello   world"
    fake_reader = MagicMock()
    fake_reader.pages = [fake_page]

    with patch("app.pdfextract.extract_pages") as fake_extract, patch(
        "pypdf.PdfReader", return_value=fake_reader
    ):
        pages = await extract_by_page("/tmp/fake.pdf")

    fake_extract.assert_not_called()
    assert pages == [PageText(page=1, text="hello world")]


async def test_extract_by_page_pypdf_raises_when_no_extractable_text(monkeypatch):
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)

    fake_page = MagicMock()
    fake_page.extract_text.return_value = ""
    fake_reader = MagicMock()
    fake_reader.pages = [fake_page]

    with patch("pypdf.PdfReader", return_value=fake_reader):
        with pytest.raises(PdfExtractError, match="no extractable text"):
            await extract_by_page("/tmp/fake.pdf")
