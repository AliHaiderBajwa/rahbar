"""Stage 5: Store. The only code in the whole system that writes to Chroma.

Build aside, then swap (ADR 0001):
1. write every chunk into a staging collection '<name>__building'
2. stamp it with the embedding pin (embed_model, dimension) and a fresh index_id
3. delete the live collection and rename the staging one into its place

The API keeps serving the old index during steps 1-2 (which take minutes), and the gap
in step 3 is one rename. A Job that dies half-way leaves only a stale '__building'
collection, which the next run deletes; the live index is never half-written.

index_id is also how the API's semantic cache is invalidated on re-index: cache keys are
namespaced by it, so answers from the previous index can never be served (section 3.3).
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .config import IndexerSettings
from .models import Chunk

WRITE_BATCH = 200


def get_client(settings: IndexerSettings) -> Any:
    import chromadb  # the thin client, from chromadb-client in requirements.txt
    from chromadb.config import Settings as ChromaSettings

    return chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port,
                               settings=ChromaSettings(anonymized_telemetry=False))


def collection_metadata(settings: IndexerSettings, dimension: int, chunks: int) -> dict:
    return {
        "hnsw:space": "cosine",
        "embed_model": settings.embed_model,       # the pin the API checks (section 4.4)
        "dimension": dimension,
        "index_id": uuid.uuid4().hex[:12],         # cache namespace (section 3.3)
        "indexed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
        "chunks": chunks,
    }


def store(chunks: list[Chunk], vectors: list[list[float]], settings: IndexerSettings,
          client: Any = None) -> int:
    """Write every chunk with its vector and metadata, and record the embedding model
    and vector dimension in the collection metadata (section 4.4). Return the count."""
    if len(chunks) != len(vectors):
        raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
    if not chunks:
        raise ValueError("refusing to publish an empty index")
    ids = [c.id for c in chunks]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate chunk ids: two documents produce the same slug")
    client = client or get_client(settings)
    live, staging = settings.collection, settings.collection + "__building"

    try:
        client.delete_collection(staging)        # left over from a Job that died
    except Exception:
        pass
    meta = collection_metadata(settings, len(vectors[0]), len(chunks))
    col = client.create_collection(name=staging, metadata=meta)
    for i in range(0, len(chunks), WRITE_BATCH):
        part = chunks[i:i + WRITE_BATCH]
        col.add(ids=[c.id for c in part], documents=[c.text for c in part],
                embeddings=vectors[i:i + WRITE_BATCH], metadatas=[c.metadata() for c in part])
    count = int(col.count())
    if count != len(chunks):
        raise RuntimeError(f"staging collection holds {count} of {len(chunks)} chunks")

    started = time.perf_counter()
    try:
        client.delete_collection(live)
    except Exception:
        pass                                     # first run: nothing to replace
    col.modify(name=live)
    print(json.dumps({"logger": "rahbar.indexer.store", "msg": "index published",
                      "collection": live, "chunks": count, "index_id": meta["index_id"],
                      "embed_model": meta["embed_model"], "dimension": meta["dimension"],
                      "swap_ms": round((time.perf_counter() - started) * 1000, 1)}), flush=True)
    return count
