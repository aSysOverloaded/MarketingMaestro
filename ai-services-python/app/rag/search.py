import os
import io
import json
import re
import time
import uuid
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

import pypdf
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
import google.generativeai as genai

from app import diagnostics
from app.config import settings
from app.observability import log_stage

logger = logging.getLogger("rag")

COLLECTION_NAME = "catalog_products"

# Local on-disk Qdrant (storage/qdrant) so the indexed catalog survives restarts. Created
# lazily: `python -m app.main` runs uvicorn with reload, which imports this module in both the
# reloader and the worker process, and local Qdrant allows only one process per folder.
_client: Optional[QdrantClient] = None


def get_client() -> QdrantClient:
    global _client
    if _client is None:
        path = settings.storage_dir / "qdrant"
        path.mkdir(parents=True, exist_ok=True)
        _client = QdrantClient(path=str(path))
    return _client


def _catalog_meta_path():
    return settings.storage_dir / "catalog.json"


VECTOR_DIMENSION = 768  # text-embedding-004 was retired; gemini-embedding-001 defaults to 3072
                        # dims but supports output_dimensionality to request this size instead
EMBEDDING_MODEL = "models/gemini-embedding-001"

# One request per EMBED_BATCH pages instead of one request for the whole catalog. A big
# catalog (hundreds of pages) made that single request fail, and the whole index silently
# fell back to mock vectors.
EMBED_BATCH = 50
# Embedding models cap input length; a page far longer than this adds nothing to retrieval.
MAX_EMBED_CHARS = 8000
# Extracted page images are only ever used to pick one hero image per page, so keeping every
# image of a large catalog just fills the disk.
MAX_IMAGES_PER_PAGE = 2
MIN_IMAGE_BYTES = 4096  # skip icons, logos, separators
# Free-tier embedding quota is per minute and counts one request per text, so a large catalog
# WILL hit it mid-ingest. Waiting and retrying is the difference between a fully indexed
# catalog and one whose last pages hold meaningless mock vectors.
EMBED_RETRIES = 4
EMBED_RETRY_SECONDS = 25
# A *query* embedding happens while the user waits, so it retries briefly and then gives up,
# rather than freezing a run for a minute. Ingest is the opposite: it is a one-off background
# cost, and a mock-vector page stays wrong until the catalog is re-uploaded.
QUERY_EMBED_RETRIES = 1
MAX_QUERY_WAIT_SECONDS = 10

def _collection_exists() -> bool:
    return any(c.name == COLLECTION_NAME for c in get_client().get_collections().collections)


def get_catalog() -> Optional[dict]:
    """The currently indexed catalog ({filename, sha256, indexed_pages, indexed_at,
    embeddings}), or None if nothing usable is indexed. Only a single catalog is kept at a
    time (the app is single-user by design)."""
    meta_path = _catalog_meta_path()
    if not meta_path.is_file() or not _collection_exists():
        return None
    if get_client().get_collection(COLLECTION_NAME).points_count == 0:
        return None
    return json.loads(meta_path.read_text(encoding="utf-8"))


def clear_catalog() -> None:
    if _collection_exists():
        get_client().delete_collection(COLLECTION_NAME)
    _catalog_meta_path().unlink(missing_ok=True)


def initialize_collection():
    get_client().recreate_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=VECTOR_DIMENSION, distance=Distance.COSINE),
    )

def _is_rate_limit(error: Exception) -> bool:
    text = str(error).lower()
    return "429" in text or "quota" in text or "rate limit" in text


def _retry_delay(error: Exception) -> int:
    """Use the provider's own retry_delay when it gives one, else a fixed wait."""
    match = re.search(r"retry[_ ]delay\s*{?\s*seconds:?\s*(\d+)", str(error), re.IGNORECASE)
    if not match:
        match = re.search(r"retry in (\d+)", str(error), re.IGNORECASE)
    return min(int(match.group(1)) + 2, 60) if match else EMBED_RETRY_SECONDS


def embed_text(text: str, is_query: bool = False) -> list:
    api_key = settings.gemini_api_key
    if not settings.has_gemini_key:
        msg = "GEMINI_API_KEY is not set (checked via app.config.settings, not the raw process environment)"
        logger.warning(f"[embed_text] {msg}, using mock vector fallback (retrieval scores will all be ~1.000 and meaningless)")
        diagnostics.set_status("embeddings", "mock", msg)
        return [0.1] * VECTOR_DIMENSION

    genai.configure(api_key=api_key)
    task_type = "retrieval_query" if is_query else "retrieval_document"
    for attempt in range(QUERY_EMBED_RETRIES + 1):
        try:
            result = genai.embed_content(
                model=EMBEDDING_MODEL,
                content=text,
                task_type=task_type,
                output_dimensionality=VECTOR_DIMENSION,
            )
            diagnostics.set_status("embeddings", "real", None)
            return result["embedding"]
        except Exception as e:
            if _is_rate_limit(e) and attempt < QUERY_EMBED_RETRIES:
                delay = min(_retry_delay(e), MAX_QUERY_WAIT_SECONDS)
                logger.warning(f"[embed_text] rate limited; waiting {delay}s and retrying once")
                time.sleep(delay)
                continue
            # Fallback in case of rate limits or transient issues
            logger.warning(f"[embed_text] embedding failed, using mock vector fallback: {e}")
            diagnostics.set_status("embeddings", "mock", str(e))
            return [0.1] * VECTOR_DIMENSION

def embed_texts(texts: list, is_query: bool = False) -> list:
    api_key = settings.gemini_api_key
    if not settings.has_gemini_key:
        msg = "GEMINI_API_KEY is not set (checked via app.config.settings, not the raw process environment)"
        logger.warning(f"[embed_texts] {msg}, using mock vectors for {len(texts)} texts (retrieval scores will all be ~1.000 and meaningless)")
        diagnostics.set_status("embeddings", "mock", msg)
        return [[0.1] * VECTOR_DIMENSION] * len(texts)

    genai.configure(api_key=api_key)
    task_type = "retrieval_query" if is_query else "retrieval_document"
    trimmed = [t[:MAX_EMBED_CHARS] for t in texts]
    vectors = []
    for start in range(0, len(trimmed), EMBED_BATCH):
        batch = trimmed[start:start + EMBED_BATCH]
        batch_no = start // EMBED_BATCH + 1
        for attempt in range(EMBED_RETRIES + 1):
            try:
                result = genai.embed_content(
                    model=EMBEDDING_MODEL,
                    content=batch,
                    task_type=task_type,
                    output_dimensionality=VECTOR_DIMENSION,
                )
                vectors.extend(result["embedding"])
                break
            except Exception as e:
                if _is_rate_limit(e) and attempt < EMBED_RETRIES:
                    delay = _retry_delay(e)
                    logger.warning(f"[embed_texts] batch {batch_no} hit the embedding rate limit; waiting {delay}s and retrying (attempt {attempt + 1}/{EMBED_RETRIES})")
                    time.sleep(delay)
                    continue
                # Only this batch degrades; the rest of the catalog still gets real vectors.
                logger.warning(f"[embed_texts] batch {batch_no} failed for {len(batch)} texts, using mock vectors: {e}")
                diagnostics.set_status("embeddings", "mock", str(e))
                vectors.extend([[0.1] * VECTOR_DIMENSION] * len(batch))
                break
        if diagnostics.get_status("embeddings")["mode"] != "mock":
            diagnostics.set_status("embeddings", "real", None)
    return vectors

def ingest_pdf(pdf_bytes: bytes, job_id: str = "unknown", filename: str = "catalog.pdf") -> dict:
    content_hash = hashlib.sha256(pdf_bytes).hexdigest()

    # Re-uploading byte-identical content skips re-embedding (saves Gemini quota) - unless the
    # stored vectors were mock ones from a run without a working key, which are worth replacing.
    current = get_catalog()
    if current and current["sha256"] == content_hash and current.get("embeddings") == "real":
        log_stage(logger, job_id, "ingest", f"content hash matches currently indexed catalog ({content_hash[:12]}...), skipping re-embed")
        return {"success": True, "indexed_pages": current["indexed_pages"], "collection_name": COLLECTION_NAME, "reused": True}

    start = time.monotonic()
    log_stage(logger, job_id, "ingest", f"starting ingest of {len(pdf_bytes)} bytes")

    # 1. Clear and create the Qdrant collection
    initialize_collection()

    # Served at /storage/extracted_images/<name> by app.main
    backend_storage = str(settings.storage_dir / "extracted_images")
    os.makedirs(backend_storage, exist_ok=True)

    # 2. Parse PDF content
    pdf_file = io.BytesIO(pdf_bytes)
    reader = pypdf.PdfReader(pdf_file)
    
    indexed_count = 0
    skipped_empty_pages = 0
    image_extract_failures = 0
    points = []

    # Store page texts and details for batch processing
    pages_to_embed = []
    page_details = []

    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        text = text.strip()
        if not text:
            skipped_empty_pages += 1
            continue

        # Extract images from this page. Sorted largest-pixel-area-first (not extraction
        # order) so that images[0] - which the recommend step takes unconditionally as the
        # brochure's hero image - is the most likely candidate to be the actual product
        # photo rather than a small decorative/lifestyle banner image that happens to be
        # placed first in the PDF's internal image order.
        image_entries = []
        try:
            for img_idx, img_file in enumerate(page.images):
                if len(image_entries) >= MAX_IMAGES_PER_PAGE:
                    break
                if len(img_file.data) < MIN_IMAGE_BYTES:
                    continue  # icons/logos are never the hero image
                img_ext = os.path.splitext(img_file.name)[1] if img_file.name else ".png"
                if not img_ext or img_ext == ".":
                    img_ext = ".png"
                img_name = f"page_{i+1}_img_{img_idx}{img_ext}"
                dest_path = os.path.join(backend_storage, img_name)

                with open(dest_path, "wb") as f:
                    f.write(img_file.data)

                area = 0
                try:
                    if img_file.image is not None:
                        width, height = img_file.image.size
                        area = width * height
                except Exception:
                    pass  # keep area=0 - falls to the end of the sort, not an extraction failure

                image_entries.append((area, f"/storage/extracted_images/{img_name}"))
        except Exception as e:
            image_extract_failures += 1
            log_stage(logger, job_id, "ingest", f"failed to extract images on page {i+1}: {e}", level="warning")

        image_entries.sort(key=lambda entry: entry[0], reverse=True)
        image_paths = [path for _, path in image_entries]

        pages_to_embed.append(text)
        page_details.append({
            "page_number": i + 1,
            "content": text,
            "images": image_paths
        })

    # 3. Create vector embeddings in exactly ONE batch request
    if pages_to_embed:
        vectors = embed_texts(pages_to_embed, is_query=False)

        # 4. Create Qdrant indexing points
        for idx, details in enumerate(page_details):
            point_id = str(uuid.uuid4())
            points.append(PointStruct(
                id=point_id,
                vector=vectors[idx],
                payload=details
            ))
            indexed_count += 1

    # 5. Insert points into Qdrant index
    if points:
        get_client().upsert(
            collection_name=COLLECTION_NAME,
            wait=True,
            points=points
        )

    duration_ms = int((time.monotonic() - start) * 1000)
    diagnostics.set_ingest_meta({
        "job_id": job_id,
        "indexed_pages": indexed_count,
        "skipped_empty_pages": skipped_empty_pages,
        "image_extract_failures": image_extract_failures,
        "total_pages": len(reader.pages),
        "duration_ms": duration_ms,
    })
    log_stage(
        logger, job_id, "ingest",
        f"done: indexed={indexed_count} skipped_empty={skipped_empty_pages} "
        f"image_failures={image_extract_failures} total_pages={len(reader.pages)} duration_ms={duration_ms}"
    )

    _catalog_meta_path().write_text(json.dumps({
        "filename": filename,
        "sha256": content_hash,
        "indexed_pages": indexed_count,
        "indexed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "embeddings": diagnostics.get_status("embeddings")["mode"],
    }), encoding="utf-8")

    return {
        "success": True,
        "indexed_pages": indexed_count,
        "collection_name": COLLECTION_NAME,
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

    info = get_client().get_collection(COLLECTION_NAME)
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
        collection_name=COLLECTION_NAME,
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
            "page_number": hit.payload.get("page_number"),
            "content": hit.payload.get("content"),
            "images": hit.payload.get("images", []),
            "score": hit.score
        })
    return matches
