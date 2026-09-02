import os
import io
import time
import uuid
import logging
import pypdf
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
import google.generativeai as genai

logger = logging.getLogger("rag")

# Initialize the Qdrant client in memory (100% free, local)
client = QdrantClient(":memory:")
COLLECTION_NAME = "catalog_products"
VECTOR_DIMENSION = 768  # text-embedding-004 was retired; gemini-embedding-001 defaults to 3072
                        # dims but supports output_dimensionality to request this size instead
EMBEDDING_MODEL = "models/gemini-embedding-001"

# Tracks metadata about the last successful ingest for the /api/rag/stats endpoint
_last_ingest_meta = {}

# Tracks whether the most recent embedding call actually used the real API or fell
# back to the mock constant vector, and why - a set GEMINI_API_KEY doesn't guarantee
# the calls succeed (bad key, quota, wrong model name, etc. all fall back silently).
_last_embed_status = {"mode": "unknown", "detail": None}

def initialize_collection():
    client.recreate_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=VECTOR_DIMENSION, distance=Distance.COSINE),
    )

def embed_text(text: str, is_query: bool = False) -> list:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        msg = "GEMINI_API_KEY is not set in this process's environment"
        logger.warning(f"[embed_text] {msg}, using mock vector fallback (retrieval scores will all be ~1.000 and meaningless)")
        _last_embed_status.update({"mode": "mock", "detail": msg})
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
        _last_embed_status.update({"mode": "real", "detail": None})
        return result["embedding"]
    except Exception as e:
        # Fallback in case of rate limits or transient issues
        logger.warning(f"[embed_text] embedding failed, using mock vector fallback: {e}")
        _last_embed_status.update({"mode": "mock", "detail": str(e)})
        return [0.1] * VECTOR_DIMENSION

def embed_texts(texts: list, is_query: bool = False) -> list:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        msg = "GEMINI_API_KEY is not set in this process's environment"
        logger.warning(f"[embed_texts] {msg}, using mock vectors for {len(texts)} texts (retrieval scores will all be ~1.000 and meaningless)")
        _last_embed_status.update({"mode": "mock", "detail": msg})
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
        _last_embed_status.update({"mode": "real", "detail": None})
        return result["embedding"]
    except Exception as e:
        logger.warning(f"[embed_texts] batch embedding failed for {len(texts)} texts, using mock vectors: {e}")
        _last_embed_status.update({"mode": "mock", "detail": str(e)})
        return [[0.1] * VECTOR_DIMENSION] * len(texts)

def ingest_pdf(pdf_bytes: bytes, job_id: str = "unknown") -> dict:
    start = time.monotonic()
    logger.info(f"[job={job_id}] [ingest] starting ingest of {len(pdf_bytes)} bytes")

    # 1. Clear and create the Qdrant collection
    initialize_collection()

    # Resolve Go backend storage path for extracted images
    current_dir = os.path.dirname(os.path.abspath(__file__))
    backend_storage = os.path.abspath(os.path.join(current_dir, "..", "..", "..", "backend-go", "storage", "extracted_images"))
    if not os.path.exists(os.path.join(backend_storage, "..")):
        backend_storage = os.path.abspath(os.path.join(current_dir, "..", "..", "backend-go", "storage", "extracted_images"))
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

        # Extract images from this page
        image_paths = []
        try:
            for img_idx, img_file in enumerate(page.images):
                img_ext = os.path.splitext(img_file.name)[1] if img_file.name else ".png"
                if not img_ext or img_ext == ".":
                    img_ext = ".png"
                img_name = f"page_{i+1}_img_{img_idx}{img_ext}"
                dest_path = os.path.join(backend_storage, img_name)

                with open(dest_path, "wb") as f:
                    f.write(img_file.data)

                image_paths.append(f"/storage/extracted_images/{img_name}")
        except Exception as e:
            image_extract_failures += 1
            logger.warning(f"[job={job_id}] [ingest] failed to extract images on page {i+1}: {e}")

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
    _last_ingest_meta.update({
        "job_id": job_id,
        "indexed_pages": indexed_count,
        "skipped_empty_pages": skipped_empty_pages,
        "image_extract_failures": image_extract_failures,
        "total_pages": len(reader.pages),
        "duration_ms": duration_ms,
    })
    logger.info(
        f"[job={job_id}] [ingest] done: indexed={indexed_count} skipped_empty={skipped_empty_pages} "
        f"image_failures={image_extract_failures} total_pages={len(reader.pages)} duration_ms={duration_ms}"
    )

    return {
        "success": True,
        "indexed_pages": indexed_count,
        "collection_name": COLLECTION_NAME
    }

def get_stats() -> dict:
    # embeddings_mode reflects the outcome of the LAST actual embed_content call, not just
    # whether GEMINI_API_KEY is set - a set key doesn't guarantee the calls are succeeding.
    embeddings_mode = _last_embed_status["mode"]
    embeddings_detail = _last_embed_status["detail"]

    collections = client.get_collections().collections
    collection_exists = any(c.name == COLLECTION_NAME for c in collections)
    if not collection_exists:
        return {
            "collection_exists": False,
            "point_count": 0,
            "last_ingest": _last_ingest_meta or None,
            "embeddings_mode": embeddings_mode,
            "embeddings_detail": embeddings_detail,
        }

    info = client.get_collection(COLLECTION_NAME)
    return {
        "collection_exists": True,
        "point_count": info.points_count,
        "last_ingest": _last_ingest_meta or None,
        "embeddings_mode": embeddings_mode,
        "embeddings_detail": embeddings_detail,
    }

def search_catalog(query: str, limit: int = 3, job_id: str = "unknown") -> list:
    start = time.monotonic()

    # Check if the collection exists
    collections = client.get_collections().collections
    collection_exists = any(c.name == COLLECTION_NAME for c in collections)
    if not collection_exists:
        logger.warning(f"[job={job_id}] [search] query='{query}' collection does not exist yet, returning 0 matches")
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
        logger.warning(f"[job={job_id}] [search] query='{query}' returned 0 matches (duration_ms={duration_ms})")
    else:
        scores = [f"{hit.score:.3f}" for hit in search_results]
        pages = [hit.payload.get("page_number") for hit in search_results]
        logger.info(
            f"[job={job_id}] [search] query='{query}' matches={len(search_results)} "
            f"scores={scores} pages={pages} duration_ms={duration_ms}"
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
