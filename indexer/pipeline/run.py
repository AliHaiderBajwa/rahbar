"""The indexer Job's entry point. It runs once and exits (section 4.2).

    python -m pipeline.run --dummy    # week 1: three fake chunks, no model needed
    python -m pipeline.run            # the real pipeline: extract, clean, chunk, embed, store

Exit code 0 means the index was written; anything else fails the Job, and
Kubernetes retries it up to the Job's backoffLimit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dataclasses import asdict

from .config import IndexerSettings, get_settings
from .models import Chunk, Document, Page


def log(msg: str, **fields: object) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "logger": "rahbar.indexer", "msg": msg, **fields}), flush=True)


def _front_matter(path: Path) -> dict[str, str]:
    """Read `key: value` lines between leading '---' markers (used by eval/fixtures)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta


def discover(corpus_dir: str) -> list[tuple[Path, str, bool]]:
    """List (path, doc_version, superseded) for every document to index.

    corpus/fetch.py writes _index.json next to the files it downloads; use it when
    present. Otherwise read Markdown front matter (the CI fixtures work this way).
    """
    root = Path(corpus_dir)
    index = root / "_index.json"
    if index.exists():
        entries = json.loads(index.read_text(encoding="utf-8"))
        return [(root / e["file"], str(e.get("doc_version", "unknown")),
                 bool(e.get("superseded", False))) for e in entries]
    found = []
    for path in sorted(root.glob("*")):
        if path.suffix.lower() not in {".pdf", ".md", ".html", ".txt"}:
            continue
        if path.name.lower().startswith("readme"):
            continue
        meta = _front_matter(path) if path.suffix.lower() == ".md" else {}
        found.append((path, meta.get("doc_version", "unknown"),
                      meta.get("superseded", "false").lower() == "true"))
    return found


def extract_all(settings: IndexerSettings) -> list[Document]:
    """Stages 1 and 2 for every document in the corpus."""
    from .clean import clean
    from .extract import extract

    documents = discover(settings.corpus_dir)
    if not documents:
        raise SystemExit(f"no documents found in {settings.corpus_dir}")
    return [clean(extract(str(path), version, old)) for path, version, old in documents]


def chunk_all(docs: list[Document], settings: IndexerSettings) -> list[Chunk]:
    from .chunk import chunk

    all_chunks: list[Chunk] = []
    for doc in docs:
        pieces = chunk(doc, settings.chunk_size, settings.chunk_overlap)
        log("chunked", source=doc.source, chunks=len(pieces))
        all_chunks.extend(pieces)
    return all_chunks


def run_pipeline(settings: IndexerSettings, docs: list[Document] | None = None) -> int:
    from .embed import embed
    from .store import store

    all_chunks = chunk_all(docs if docs is not None else extract_all(settings), settings)
    vectors = embed([c.text for c in all_chunks], settings)
    return store(all_chunks, vectors, settings)


def dump_documents(docs: list[Document], path: str) -> None:
    """Save extracted+cleaned documents, so experiments can re-chunk without re-OCR."""
    Path(path).write_text(json.dumps([asdict(d) for d in docs]), encoding="utf-8")


def load_documents(path: str) -> list[Document]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Document(source=d["source"], doc_version=d["doc_version"],
                     superseded=d["superseded"], pages=[Page(**p) for p in d["pages"]])
            for d in raw]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the RAHBAR index, then exit.")
    parser.add_argument("--dummy", action="store_true", help="write three fake chunks")
    parser.add_argument("--dump-docs", metavar="PATH",
                        help="extract and clean only, save the documents as JSON, then exit")
    parser.add_argument("--from-docs", metavar="PATH",
                        help="skip extraction: chunk, embed and store documents from --dump-docs")
    args = parser.parse_args()
    settings = get_settings()
    log("indexer starting", dummy=args.dummy, embed_model=settings.embed_model,
        collection=settings.collection, chroma=f"{settings.chroma_host}:{settings.chroma_port}",
        pod=os.environ.get("HOSTNAME", "-"))
    started = time.perf_counter()
    try:
        if args.dummy:
            from .dummy import write_dummy_index
            from .store import get_client

            count = write_dummy_index(settings, get_client(settings))
        elif args.dump_docs:
            docs = extract_all(settings)
            dump_documents(docs, args.dump_docs)
            count = sum(len(d.pages) for d in docs)
        else:
            loaded = load_documents(args.from_docs) if args.from_docs else None
            count = run_pipeline(settings, loaded)
    except NotImplementedError as exc:
        log("indexer stage not implemented yet", error=str(exc))
        return 2
    log("indexer finished", chunks=count, seconds=round(time.perf_counter() - started, 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
