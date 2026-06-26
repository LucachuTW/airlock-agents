"""Corpus ingestion: real markdown docs -> chunks -> local embeddings -> pgvector.

Reads `sources.json` next to the corpus (written by scripts/fetch_data.py) so every
chunk cites the live source URL. Documents no longer present are removed.
"""

import json
import re
from pathlib import Path

from sqlalchemy import delete, select

from app import llm
from app.db import SessionLocal
from app.models import Chunk, Document

CHUNK_MAX_CHARS = 1200

_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_SHORTCODE = re.compile(r"{{<.*?>}}")  # Hugo shortcodes add noise to chunks


def _clean(text: str) -> str:
    return _SHORTCODE.sub("", _FRONTMATTER.sub("", text))


def _pack(paragraphs: list[str]) -> list[str]:
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if current and len(current) + len(para) + 2 > CHUNK_MAX_CHARS:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
        while len(current) > CHUNK_MAX_CHARS:  # single oversized paragraph
            chunks.append(current[:CHUNK_MAX_CHARS])
            current = current[CHUNK_MAX_CHARS:].strip()
    if current:
        chunks.append(current)
    return chunks


def chunk_markdown(text: str) -> list[str]:
    """Heading-aware: split into h2 sections first, then greedily pack paragraphs.

    Keeps a policy section together with its heading, which makes retrieved chunks
    self-explanatory and citations precise.
    """
    chunks: list[str] = []
    for section in re.split(r"\n(?=## )", _clean(text)):
        paragraphs = [p.strip() for p in section.split("\n\n") if p.strip()]
        chunks.extend(_pack(paragraphs))
    return chunks


def _sources(path: Path) -> dict[str, dict]:
    manifest = path / "sources.json"
    if manifest.exists():
        return json.loads(manifest.read_text())
    return {f.name: {"title": f.stem, "url": f"corpus/{f.name}"} for f in path.glob("*.md")}


async def ingest_directory(path: Path) -> int:
    """(Re-)ingest every corpus file. Returns number of chunks written."""
    embedder = llm.embeddings()
    sources = _sources(path)
    total = 0
    async with SessionLocal() as session:
        for filename, meta in sorted(sources.items()):
            file = path / filename
            existing = (
                await session.execute(select(Document).where(Document.source_uri == meta["url"]))
            ).scalar_one_or_none()
            version = 1
            if existing:
                version = existing.version + 1
                await session.execute(delete(Document).where(Document.id == existing.id))
            doc = Document(title=meta["title"], source_uri=meta["url"], version=version)
            session.add(doc)
            await session.flush()
            chunks = chunk_markdown(file.read_text())
            vectors = await embedder.aembed_documents(chunks)
            for i, (content, vector) in enumerate(zip(chunks, vectors, strict=True)):
                session.add(Chunk(document_id=doc.id, ord=i, content=content, embedding=vector))
            total += len(chunks)
        # drop documents that are no longer part of the corpus
        current = [meta["url"] for meta in sources.values()]
        await session.execute(delete(Document).where(Document.source_uri.not_in(current)))
        await session.commit()
    return total
