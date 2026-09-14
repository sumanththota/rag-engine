# Characterization tests: these lock in build()'s CURRENT behavior (including
# known-mediocre choices like zero overlap) as ground truth, ported from
# internal/chunk/chunk_test.go. They do not assert build() is "correct" — the
# known chunking-quality issues are a separate, later change.

from app.chunk import Chunk, ChunkInput, build


def test_build_empty_input():
    got = build([], 5)
    assert got == []


def test_build_blank_page_text_is_skipped():
    got = build([ChunkInput(page=1, text="   ")], 5)
    assert got == []


def test_build_single_chunk_under_limit():
    got = build([ChunkInput(page=1, text="one two three")], 5)
    want = [Chunk(id=0, page=1, text="one two three")]
    assert got == want


def test_build_exact_multiple_boundary():
    # 10 words, 5 per chunk -> exactly 2 chunks, no trailing empty chunk.
    got = build([ChunkInput(page=1, text="a b c d e f g h i j")], 5)
    want = [
        Chunk(id=0, page=1, text="a b c d e"),
        Chunk(id=1, page=1, text="f g h i j"),
    ]
    assert got == want


def test_build_non_multiple_trailing_chunk_is_short():
    # 7 words, 5 per chunk -> [0:5], [5:7] (last chunk shorter, not padded/merged).
    got = build([ChunkInput(page=1, text="a b c d e f g")], 5)
    want = [
        Chunk(id=0, page=1, text="a b c d e"),
        Chunk(id=1, page=1, text="f g"),
    ]
    assert got == want


def test_build_zero_overlap_between_chunks():
    # Locks in the known characteristic: no word appears in two chunks. A
    # quality fix must change this test deliberately, not accidentally.
    got = build([ChunkInput(page=1, text="a b c d e f g h i j")], 4)
    seen: dict[str, int] = {}
    for c in got:
        for w in c.text.split(" "):
            seen[w] = seen.get(w, 0) + 1
    for w, n in seen.items():
        assert n == 1, f"word {w!r} appeared in {n} chunks, want exactly 1 (zero overlap)"


def test_build_ids_increment_across_pages():
    got = build(
        [
            ChunkInput(page=1, text="a b c d e f"),
            ChunkInput(page=2, text="g h i"),
        ],
        5,
    )
    want = [
        Chunk(id=0, page=1, text="a b c d e"),
        Chunk(id=1, page=1, text="f"),
        Chunk(id=2, page=2, text="g h i"),
    ]
    assert got == want


def test_build_skipped_blank_page_does_not_burn_an_id():
    # A blank page in the middle must not leave a gap in chunk IDs, since the
    # Python port must reproduce identical IDs for identical input.
    got = build(
        [
            ChunkInput(page=1, text="a b c"),
            ChunkInput(page=2, text=""),
            ChunkInput(page=3, text="d e f"),
        ],
        5,
    )
    want = [
        Chunk(id=0, page=1, text="a b c"),
        Chunk(id=1, page=3, text="d e f"),
    ]
    assert got == want


def test_build_whitespace_is_collapsed_by_split():
    # str.split() with no args treats any run of whitespace (spaces, tabs,
    # newlines) as one separator and re-joins with single spaces — this
    # normalizes text, it is not a lossless roundtrip. Matches Go's
    # strings.Fields semantics exactly.
    got = build([ChunkInput(page=1, text="a\n\tb   c\n\n d")], 10)
    want = [Chunk(id=0, page=1, text="a b c d")]
    assert got == want


def test_build_non_positive_words_per_chunk_defaults_to_350():
    text = "x " * 400

    got_zero = build([ChunkInput(page=1, text=text)], 0)
    got_neg = build([ChunkInput(page=1, text=text)], -5)
    got_explicit_350 = build([ChunkInput(page=1, text=text)], 350)

    assert len(got_zero) == 2
    assert len(got_neg) == 2
    assert got_zero == got_explicit_350
    assert got_neg == got_explicit_350
