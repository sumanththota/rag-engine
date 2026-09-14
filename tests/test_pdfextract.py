"""Unit tests for app/pdfextract.py — the LLAMA_CLOUD_API_KEY routing branch,
the local pypdf fallback, and the on-disk extraction cache, translated from
internal/pdfextract/pdf.go's behavior. LlamaParse's own HTTP upload/poll flow
is covered by app/llamaparse.py directly, not re-tested here.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.llamaparse import Page
from app.pdfextract import PdfExtractError, PageText, extract_by_page


@pytest.fixture(autouse=True)
def isolated_cache_dir(tmp_path, monkeypatch):
    # Every test gets its own empty cache dir — no test depends on, or
    # pollutes, the real docs/pdfextract_cache fixture.
    monkeypatch.setattr("app.pdfextract._CACHE_DIR", tmp_path / "cache")


@pytest.fixture
def fake_pdf(tmp_path):
    # _cache_key() stats the file, so it must actually exist on disk.
    p = tmp_path / "fake.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    return str(p)


async def test_extract_by_page_uses_llamaparse_when_api_key_set(monkeypatch, fake_pdf):
    monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "test-key")
    monkeypatch.setenv("LLAMAPARSE_TIER", "agentic")

    fake_extract = AsyncMock(
        return_value=[Page(number=1, text=" a "), Page(number=2, text="b")]
    )
    with patch("app.pdfextract.extract_pages", fake_extract):
        pages = await extract_by_page(fake_pdf)

    fake_extract.assert_awaited_once_with("test-key", fake_pdf, "agentic")
    assert pages == [PageText(page=1, text="a"), PageText(page=2, text="b")]


async def test_extract_by_page_llamaparse_defaults_tier_and_backfills_page_number(
    monkeypatch, fake_pdf
):
    monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "test-key")
    monkeypatch.delenv("LLAMAPARSE_TIER", raising=False)

    # number=0 (or missing) must fall back to sequential position, matching
    # Go's `if n <= 0 { n = len(out) + 1 }`.
    fake_extract = AsyncMock(
        return_value=[Page(number=0, text="first"), Page(number=0, text="second")]
    )
    with patch("app.pdfextract.extract_pages", fake_extract):
        pages = await extract_by_page(fake_pdf)

    fake_extract.assert_awaited_once_with("test-key", fake_pdf, "cost_effective")
    assert pages == [PageText(page=1, text="first"), PageText(page=2, text="second")]


async def test_extract_by_page_llamaparse_skips_empty_pages_and_raises_if_all_empty(
    monkeypatch, fake_pdf
):
    monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "test-key")

    fake_extract = AsyncMock(return_value=[Page(number=1, text="   ")])
    with patch("app.pdfextract.extract_pages", fake_extract):
        with pytest.raises(PdfExtractError, match="no non-empty pages"):
            await extract_by_page(fake_pdf)


async def test_extract_by_page_falls_back_to_pypdf_when_no_api_key(monkeypatch, fake_pdf):
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)

    fake_page = MagicMock()
    fake_page.extract_text.return_value = "hello   world"
    fake_reader = MagicMock()
    fake_reader.pages = [fake_page]

    with patch("app.pdfextract.extract_pages") as fake_extract, patch(
        "pypdf.PdfReader", return_value=fake_reader
    ):
        pages = await extract_by_page(fake_pdf)

    fake_extract.assert_not_called()
    assert pages == [PageText(page=1, text="hello world")]


async def test_extract_by_page_pypdf_raises_when_no_extractable_text(monkeypatch, fake_pdf):
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)

    fake_page = MagicMock()
    fake_page.extract_text.return_value = ""
    fake_reader = MagicMock()
    fake_reader.pages = [fake_page]

    with patch("pypdf.PdfReader", return_value=fake_reader):
        with pytest.raises(PdfExtractError, match="no extractable text"):
            await extract_by_page(fake_pdf)


# --- on-disk cache: the whole point is "never pay LlamaParse twice" ---


async def test_second_call_hits_cache_and_makes_no_llamaparse_call(monkeypatch, fake_pdf):
    monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "test-key")

    fake_extract = AsyncMock(return_value=[Page(number=1, text="cached content")])
    with patch("app.pdfextract.extract_pages", fake_extract):
        first = await extract_by_page(fake_pdf)
        second = await extract_by_page(fake_pdf)

    # The real assertion: exactly one extraction call across two ingests.
    fake_extract.assert_awaited_once()
    assert first == second == [PageText(page=1, text="cached content")]


async def test_cache_is_keyed_by_tier_so_different_tiers_do_not_collide(
    monkeypatch, fake_pdf
):
    monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "test-key")

    monkeypatch.setenv("LLAMAPARSE_TIER", "cost_effective")
    fake_extract = AsyncMock(return_value=[Page(number=1, text="cheap tier")])
    with patch("app.pdfextract.extract_pages", fake_extract):
        cheap = await extract_by_page(fake_pdf)

    monkeypatch.setenv("LLAMAPARSE_TIER", "agentic")
    fake_extract2 = AsyncMock(return_value=[Page(number=1, text="agentic tier")])
    with patch("app.pdfextract.extract_pages", fake_extract2):
        agentic = await extract_by_page(fake_pdf)

    fake_extract.assert_awaited_once()
    fake_extract2.assert_awaited_once()
    assert cheap == [PageText(page=1, text="cheap tier")]
    assert agentic == [PageText(page=1, text="agentic tier")]


async def test_pypdf_path_is_also_cached(monkeypatch, fake_pdf):
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)

    fake_page = MagicMock()
    fake_page.extract_text.return_value = "some text"
    fake_reader = MagicMock()
    fake_reader.pages = [fake_page]

    with patch("pypdf.PdfReader", return_value=fake_reader) as fake_reader_cls:
        await extract_by_page(fake_pdf)
        await extract_by_page(fake_pdf)

    fake_reader_cls.assert_called_once()
