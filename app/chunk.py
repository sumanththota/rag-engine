from pydantic import BaseModel


class ChunkInput(BaseModel):
    page: int
    text: str


class Chunk(BaseModel):
    id: int
    page: int
    text: str


def build(inputs: list[ChunkInput], words_per_chunk: int = 350) -> list[Chunk]:
    """Pure, sync. Mirrors chunk.Build byte-for-byte: words_per_chunk <= 0 falls
    back to 350; splits on strings.Fields-equivalent whitespace tokenization;
    empty-text inputs are skipped; ids are assigned sequentially across all inputs."""
    if words_per_chunk <= 0:
        words_per_chunk = 350

    out: list[Chunk] = []
    id_ = 0

    for inp in inputs:
        words = inp.text.split()
        if not words:
            continue

        for start in range(0, len(words), words_per_chunk):
            end = min(start + words_per_chunk, len(words))
            text = " ".join(words[start:end])
            out.append(Chunk(id=id_, page=inp.page, text=text))
            id_ += 1

    return out
