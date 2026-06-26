from pathlib import Path

import pytest

from app.services.ingest import CHUNK_MAX_CHARS, _clean, chunk_markdown

CORPUS = Path(__file__).resolve().parent.parent / "data" / "corpus"


@pytest.mark.skipif(not CORPUS.exists(), reason="corpus not downloaded (make data)")
def test_chunker_respects_max_and_preserves_content():
    for file in CORPUS.glob("*.md"):
        cleaned = _clean(file.read_text())
        chunks = chunk_markdown(file.read_text())
        joined = "".join(chunks)
        assert chunks, file.name
        assert all(0 < len(c) <= CHUNK_MAX_CHARS for c in chunks)
        for line in cleaned.splitlines():
            if len(line.strip()) > 60:  # every distinctive line survives chunking
                assert line.strip()[:50] in joined, (file.name, line)


def test_chunker_splits_oversized_paragraph():
    chunks = chunk_markdown("x" * (CHUNK_MAX_CHARS * 2 + 10))
    assert len(chunks) == 3
    assert "".join(chunks).count("x") == CHUNK_MAX_CHARS * 2 + 10


def test_chunker_keeps_heading_with_section():
    text = "# Title\n\nintro\n\n## Policy A\n\nrule one\n\n## Policy B\n\nrule two"
    chunks = chunk_markdown(text)
    heading_chunks = [c for c in chunks if "## Policy A" in c]
    assert heading_chunks and "rule one" in heading_chunks[0]
