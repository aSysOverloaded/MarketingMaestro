"""Ingesting a catalog, and searching it.

The pieces live next door: embeddings.py (text -> vectors), blocks.py (PDF -> product blocks),
index.py (collections and the catalog record). This module is the sequence that uses them, and
the query side.
"""
import hashlib
import io
import json
import os
import logging
import time
import uuid
from datetime import datetime, timezone

import pypdf
from qdrant_client.models import PointStruct

from app import diagnostics
from app.ai.catalog_brand import detect_catalog_brand
from app.config import settings
from app.observability import log_stage
from app.rag import extraction_cache
from app.rag.blocks import (MIN_CHUNK_CHARS, chunk_pdf_by_layout, chunk_pdf_by_page,
                            filter_product_blocks, find_boilerplate_lines, strip_boilerplate)
from app.rag.embeddings import embed_text, embed_texts
from app.rag.index import (INGEST_VERSION, _catalog_meta_path, _collection_exists, _current_collection,
                           _drop_other_image_folders, collection_for, drop_other_collections,
                           get_catalog, get_client, initialize_collection)

logger = logging.getLogger("rag")


logger = logging.getLogger("rag")


def ingest_pdf(pdf_bytes: bytes, job_id: str = "unknown", filename: str = "catalog.pdf") -> dict:
    content_hash = hashlib.sha256(pdf_bytes).hexdigest()

    # Re-uploading byte-identical content skips re-embedding (saves Gemini quota) - unless the
    # stored vectors were mock ones from a run without a working key, which are worth replacing.
    current = get_catalog()
    if (current and current["sha256"] == content_hash and current.get("embeddings") == "real"
            and current.get("ingest_version") == INGEST_VERSION):
        log_stage(logger, job_id, "ingest", f"content hash matches currently indexed catalog ({content_hash[:12]}...), skipping re-embed")
        return {"success": True, "indexed_pages": current["indexed_pages"],
                "indexed_chunks": current.get("indexed_chunks", current["indexed_pages"]),
                "collection_name": current.get("collection"), "reused": True}

    start = time.monotonic()
    log_stage(logger, job_id, "ingest", f"starting ingest of {len(pdf_bytes)} bytes")

    # 1. A fresh collection for this catalog (and goodbye to any previous one)
    collection = collection_for(content_hash)
    initialize_collection(collection)

    # One image folder per catalog, served at /storage/extracted_images/<catalog>/<name>.
    # Per-catalog so a replaced catalog's images can be dropped without touching the ones a
    # half-finished ingest might still need.
    images_root = settings.storage_dir / "extracted_images"
    backend_storage = str(images_root / content_hash[:16])
    os.makedirs(backend_storage, exist_ok=True)

    # 2. Split the PDF into product-sized chunks (layout-aware, one per product block)
    total_pages = len(pypdf.PdfReader(io.BytesIO(pdf_bytes)).pages)
    try:
        chunks, stats = chunk_pdf_by_layout(pdf_bytes, backend_storage, job_id)
    except Exception as e:
        log_stage(logger, job_id, "ingest", f"layout chunking failed ({e}); falling back to one chunk per page", level="warning")
        chunks, stats = chunk_pdf_by_page(pdf_bytes, backend_storage, job_id)
    if not chunks:
        chunks, stats = chunk_pdf_by_page(pdf_bytes, backend_storage, job_id)

    # Drop repeated navigation/header lines before embedding; the cleaned text is also what
    # the extractor later reads.
    texts = [c["content"] for c in chunks]
    boilerplate = find_boilerplate_lines(texts)
    if boilerplate:
        log_stage(logger, job_id, "ingest", f"stripping {len(boilerplate)} repeated line(s) of page furniture")
        for chunk in chunks:
            chunk["content"] = strip_boilerplate(chunk["content"], boilerplate)
        chunks = [c for c in chunks if len(c["content"]) >= MIN_CHUNK_CHARS]

    chunks = filter_product_blocks(chunks, job_id)

    # 3. Embed every chunk (in batches), then index it
    points = []
    if chunks:
        log_stage(logger, job_id, "ingest", f"embedding {len(chunks)} chunk(s) from {stats['pages_with_text']} page(s)")
        vectors = embed_texts([c["content"] for c in chunks], is_query=False)
        points = [PointStruct(id=str(uuid.uuid4()), vector=vector, payload=chunk)
                  for chunk, vector in zip(chunks, vectors)]

    # 4. Insert points into Qdrant index
    if points:
        get_client().upsert(
            collection_name=collection,
            wait=True,
            points=points
        )

    # One LLM call per catalog: brand styling belongs to the catalog, not to each product name.
    sample = "\n\n".join(c["content"] for c in chunks[:8])
    brand = detect_catalog_brand(sample, job_id) if chunks else None
    if brand:
        log_stage(logger, job_id, "ingest", f"catalog brand detected: {brand['name']}")

    indexed_count = stats["pages_with_text"]
    duration_ms = int((time.monotonic() - start) * 1000)
    diagnostics.set_ingest_meta({
        "job_id": job_id,
        "indexed_pages": indexed_count,
        "indexed_chunks": len(chunks),
        "skipped_empty_pages": total_pages - indexed_count,
        "image_extract_failures": stats["image_failures"],
        "total_pages": total_pages,
        "duration_ms": duration_ms,
    })
    log_stage(
        logger, job_id, "ingest",
        f"done: pages={indexed_count}/{total_pages} chunks={len(chunks)} "
        f"image_failures={stats['image_failures']} duration_ms={duration_ms}"
    )

    if not points:
        # Nothing indexed (no usable text, or the quota ran out before the first batch): leave
        # whatever catalog was in use alone rather than replacing it with an empty index.
        log_stage(logger, job_id, "ingest", "no chunks indexed; keeping the previous catalog", level="warning")
        get_client().delete_collection(collection_name=collection)
        return {"success": False, "indexed_pages": 0, "indexed_chunks": 0,
                "collection_name": None, "reused": False}

    # Chunk ids describe positions in the index just rebuilt, so anything cached against the
    # previous build is meaningless now.
    extraction_cache.clear(content_hash)

    _catalog_meta_path().write_text(json.dumps({
        "filename": filename,
        "sha256": content_hash,
        "collection": collection,
        "ingest_version": INGEST_VERSION,
        "brand": brand,
        "indexed_pages": indexed_count,
        "indexed_chunks": len(chunks),
        "indexed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "embeddings": diagnostics.get_status("embeddings")["mode"],
    }), encoding="utf-8")

    drop_other_collections(keep=collection)
    _drop_other_image_folders(keep=content_hash[:16])

    return {
        "success": True,
        "indexed_pages": indexed_count,
        "indexed_chunks": len(chunks),
        "collection_name": collection,
        "reused": False,
    }


def get_stats() -> dict:
    # embeddings_mode reflects the outcome of the LAST actual embed_content call, not just
    # whether GEMINI_API_KEY is set - a set key doesn't guarantee the calls are succeeding.
    embed_status = diagnostics.get_status("embeddings")
    embeddings_mode = embed_status["mode"]
    embeddings_detail = embed_status["detail"]

    if not _collection_exists():
        return {
            "collection_exists": False,
            "point_count": 0,
            "catalog": None,
            "last_ingest": diagnostics.get_ingest_meta(),
            "embeddings_mode": embeddings_mode,
            "embeddings_detail": embeddings_detail,
        }

    info = get_client().get_collection(_current_collection())
    return {
        "collection_exists": True,
        "point_count": info.points_count,
        "catalog": get_catalog(),
        "last_ingest": diagnostics.get_ingest_meta(),
        "embeddings_mode": embeddings_mode,
        "embeddings_detail": embeddings_detail,
    }


def search_catalog(query: str, limit: int = 3, job_id: str = "unknown") -> list:
    start = time.monotonic()

    if not _collection_exists():
        log_stage(logger, job_id, "search", f"query='{query}' collection does not exist yet, returning 0 matches", level="warning")
        return []

    # 1. Generate query vector embedding
    query_vector = embed_text(query, is_query=True)

    # 2. Perform Cosine Similarity Search
    search_results = get_client().search(
        collection_name=_current_collection(),
        query_vector=query_vector,
        limit=limit
    )

    duration_ms = int((time.monotonic() - start) * 1000)
    if not search_results:
        log_stage(logger, job_id, "search", f"query='{query}' returned 0 matches (duration_ms={duration_ms})", level="warning")
    else:
        scores = [f"{hit.score:.3f}" for hit in search_results]
        pages = [hit.payload.get("page_number") for hit in search_results]
        log_stage(
            logger, job_id, "search",
            f"query='{query}' matches={len(search_results)} scores={scores} pages={pages} duration_ms={duration_ms}"
        )

    # 3. Format and return matched payloads including images
    matches = []
    for hit in search_results:
        matches.append({
            "chunk_id": hit.payload.get("chunk_id", f"p{hit.payload.get('page_number')}b0"),
            "page_number": hit.payload.get("page_number"),
            "block_index": hit.payload.get("block_index", 0),
            "content": hit.payload.get("content"),
            "images": hit.payload.get("images", []),
            "score": hit.score
        })
    return matches
