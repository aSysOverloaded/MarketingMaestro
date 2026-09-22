import os
import io
import time
import uuid
import hashlib
import logging
import pypdf
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
import google.generativeai as genai

from app import diagnostics
from app.config import settings
from app.observability import log_stage

logger = logging.getLogger("rag")

# Initialize the Qdrant client in memory (100% free, local)
client = QdrantClient(":memory:")
COLLECTION_NAME = "catalog_products"

# Content hash of the PDF currently held in the in-memory collection, and the ingest
# result that produced it. A re-upload of byte-identical content (e.g. clicking
# Analyze again after a downstream step like CriticStep rejects the copy) skips
# re-embedding entirely instead of burning Gemini embedding quota for no reason -
# the catalog itself didn't change, only something later in the pipeline failed.
# Reset to None whenever the process restarts, since the in-memory index is too.
_last_ingested_hash = None
_last_ingest_result = None
VECTOR_DIMENSION = 768  # text-embedding-004 was retired; gemini-embedding-001 defaults to 3072
                        # dims but supports output_dimensionality to request this size instead
EMBEDDING_MODEL = "models/gemini-embedding-001"

def initialize_collection():
    client.recreate_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=VECTOR_DIMENSION, distance=Distance.COSINE),
    )

def embed_text(text: str, is_query: bool = False) -> list:
    api_key = settings.gemini_api_key
    if not settings.has_gemini_key:
        msg = "GEMINI_API_KEY is not set (checked via app.config.settings, not the raw process environment)"
        logger.warning(f"[embed_text] {msg}, using mock vector fallback (retrieval scores will all be ~1.000 and meaningless)")
        diagnostics.set_status("embeddings", "mock", msg)
        return [0.1] * VECTOR_DIMENSION

    genai.configure(api_key=api_key)
    task_type = "retrieval_query" if is_query else "retrieval_document"
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
    try:
        result = genai.embed_content(
            model=EMBEDDING_MODEL,
            content=texts,
            task_type=task_type,
            output_dimensionality=VECTOR_DIMENSION,
        )
        diagnostics.set_status("embeddings", "real", None)
        return result["embedding"]
    except Exception as e:
        logger.warning(f"[embed_texts] batch embedding failed for {len(texts)} texts, using mock vectors: {e}")
        diagnostics.set_status("embeddings", "mock", str(e))
        return [[0.1] * VECTOR_DIMENSION] * len(texts)

def ingest_pdf(pdf_bytes: bytes, job_id: str = "unknown") -> dict:
    global _last_ingested_hash, _last_ingest_result

    content_hash = hashlib.sha256(pdf_bytes).hexdigest()
    collections = client.get_collections().collections
    collection_exists = any(c.name == COLLECTION_NAME for c in collections)

    if collection_exists and content_hash == _last_ingested_hash and _last_ingest_result is not None:
        log_stage(logger, job_id, "ingest", f"content hash matches currently indexed catalog ({content_hash[:12]}...), skipping re-embed")
        return _last_ingest_result

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
        client.upsert(
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

    result = {
        "success": True,
        "indexed_pages": indexed_count,
        "collection_name": COLLECTION_NAME
    }
    _last_ingested_hash = content_hash
    _last_ingest_result = result
    return result

def get_stats() -> dict:
    # embeddings_mode reflects the outcome of the LAST actual embed_content call, not just
    # whether GEMINI_API_KEY is set - a set key doesn't guarantee the calls are succeeding.
    embed_status = diagnostics.get_status("embeddings")
    embeddings_mode = embed_status["mode"]
    embeddings_detail = embed_status["detail"]

    collections = client.get_collections().collections
    collection_exists = any(c.name == COLLECTION_NAME for c in collections)
    if not collection_exists:
        return {
            "collection_exists": False,
            "point_count": 0,
            "last_ingest": diagnostics.get_ingest_meta(),
            "embeddings_mode": embeddings_mode,
            "embeddings_detail": embeddings_detail,
        }

    info = client.get_collection(COLLECTION_NAME)
    return {
        "collection_exists": True,
        "point_count": info.points_count,
        "last_ingest": diagnostics.get_ingest_meta(),
        "embeddings_mode": embeddings_mode,
        "embeddings_detail": embeddings_detail,
    }

def search_catalog(query: str, limit: int = 3, job_id: str = "unknown") -> list:
    start = time.monotonic()

    # Check if the collection exists
    collections = client.get_collections().collections
    collection_exists = any(c.name == COLLECTION_NAME for c in collections)
    if not collection_exists:
        log_stage(logger, job_id, "search", f"query='{query}' collection does not exist yet, returning 0 matches", level="warning")
        return []

    # 1. Generate query vector embedding
    query_vector = embed_text(query, is_query=True)

    # 2. Perform Cosine Similarity Search
    search_results = client.search(
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
