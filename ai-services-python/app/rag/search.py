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

import pdfplumber
import pypdf
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
import google.generativeai as genai

from app import diagnostics
from app.config import settings
from app.rag import extraction_cache
from app.rag.layout import segment_words
from app.observability import log_stage

logger = logging.getLogger("rag")

# One collection per catalog, named from its content hash. qdrant-client 1.9's on-disk mode
# does not really drop a deleted collection's points - after delete + create they are still
# there - so reusing a single name left the previous catalog's products turning up in searches
# for the new one (884 points indexed for a 644-chunk catalogue). A fresh name per catalog
# sidesteps that entirely; the old collection is deleted too, for tidiness.
COLLECTION_PREFIX = "catalog_"


def collection_for(catalog_sha: str) -> str:
    return f"{COLLECTION_PREFIX}{catalog_sha[:16]}"


def _current_collection() -> Optional[str]:
    """Name of the collection holding the catalog currently in use, if any."""
    meta_path = _catalog_meta_path()
    if not meta_path.is_file():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8")).get("collection")
    except Exception:
        return None

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
# Blocks shorter than this are page furniture, stray codes or captions: not worth an index
# entry (and every entry costs an embedding request).
MIN_CHUNK_CHARS = 80
# Resolution for cropping a product photo out of the rendered page.
IMAGE_RENDER_DPI = 110
# Smaller than this (in PDF points squared) is an icon or colour swatch, not a product shot.
MIN_PRODUCT_IMAGE_AREA = 2500
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

def _collection_exists(name: Optional[str] = None) -> bool:
    name = name or _current_collection()
    return bool(name) and any(c.name == name for c in get_client().get_collections().collections)


def get_catalog() -> Optional[dict]:
    """The currently indexed catalog ({filename, sha256, indexed_pages, indexed_at,
    embeddings}), or None if nothing usable is indexed. Only a single catalog is kept at a
    time (the app is single-user by design)."""
    meta_path = _catalog_meta_path()
    if not meta_path.is_file() or not _collection_exists():
        return None
    if get_client().get_collection(_current_collection()).points_count == 0:
        return None
    return json.loads(meta_path.read_text(encoding="utf-8"))


def clear_catalog() -> None:
    current = get_catalog()
    if _collection_exists():
        get_client().delete_collection(_current_collection())
    _catalog_meta_path().unlink(missing_ok=True)
    if current:
        extraction_cache.clear(current["sha256"])


def initialize_collection(name: str) -> None:
    """Create an empty collection for this catalog, dropping every older catalog's."""
    client = get_client()
    for existing in client.get_collections().collections:
        if existing.name.startswith(COLLECTION_PREFIX):
            client.delete_collection(collection_name=existing.name)
    client.create_collection(
        collection_name=name,
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

# A line repeated on at least this share of pages is navigation/running header, not content.
BOILERPLATE_PAGE_SHARE = 0.4
MAX_BOILERPLATE_LINE_CHARS = 200


def find_boilerplate_lines(page_texts: list) -> set:
    """Lines that appear on a large share of pages: nav bars, running headers, page furniture.
    They add nothing to retrieval and dilute every page's embedding towards the same centre."""
    if len(page_texts) < 5:
        return set()
    counts = {}
    for text in page_texts:
        for line in {ln.strip() for ln in text.splitlines() if ln.strip()}:
            if len(line) <= MAX_BOILERPLATE_LINE_CHARS:
                counts[line] = counts.get(line, 0) + 1
    threshold = max(2, int(len(page_texts) * BOILERPLATE_PAGE_SHARE))
    return {line for line, n in counts.items() if n >= threshold}


def strip_boilerplate(text: str, boilerplate: set) -> str:
    return "\n".join(ln for ln in text.splitlines() if ln.strip() not in boilerplate).strip()


def select_page_images(page, page_number: int, dest_dir: str) -> list:
    """Save the largest MAX_IMAGES_PER_PAGE images of a page, biggest first.

    Every image is measured before any is written: a catalogue page can carry dozens of images
    (logos, colour swatches, icons), so taking the first ones that pass a size floor picks a
    banner rather than the product. images[0] becomes the brochure's hero image.
    """
    candidates = []
    for img_idx, img_file in enumerate(page.images):
        if len(img_file.data) < MIN_IMAGE_BYTES:
            continue  # icons/logos are never the hero image
        try:
            width, height = img_file.image.size
            area = width * height
        except Exception:
            area = len(img_file.data)  # undecodable: byte size is a reasonable proxy
        candidates.append((area, img_idx, img_file))

    candidates.sort(key=lambda c: c[0], reverse=True)
    paths = []
    for area, img_idx, img_file in candidates[:MAX_IMAGES_PER_PAGE]:
        ext = os.path.splitext(img_file.name)[1] if img_file.name else ".png"
        if not ext or ext == ".":
            ext = ".png"
        name = f"page_{page_number}_img_{img_idx}{ext}"
        with open(os.path.join(dest_dir, name), "wb") as f:
            f.write(img_file.data)
        paths.append(f"/storage/extracted_images/{name}")
    return paths


def _crop_block_image(plumber_page, block, page_number: int, block_index: int, dest_dir: str, rendered) -> Optional[str]:
    """Save this block's product photo, cropped out of the rendered page.

    Cropping the render (rather than pulling the embedded image stream) sidesteps exotic
    encodings a browser could not display anyway, and keeps the picture tied to where the
    product actually sits on the page.
    """
    # A catalogue usually sets the photo *beside* its text, not inside it: on the real
    # catalogue's page 31 the balls sit in a column at x -14..170 while their text blocks
    # start at x 175. So prefer images whose vertical span overlaps this block, and among
    # those take the closest horizontally.
    def vertical_overlap(im):
        return max(0.0, min(block.bottom, im["bottom"]) - max(block.top, im["top"]))

    def horizontal_distance(im):
        return abs((im["x0"] + im["x1"]) / 2 - (block.x0 + block.x1) / 2)

    def area(im):
        return (im["x1"] - im["x0"]) * (im["bottom"] - im["top"])

    candidates = [im for im in plumber_page.images if area(im) >= MIN_PRODUCT_IMAGE_AREA and vertical_overlap(im) > 0]
    if not candidates:
        return None
    image = max(candidates, key=lambda im: (round(vertical_overlap(im) / max(block.bottom - block.top, 1), 1), -horizontal_distance(im)))

    scale = IMAGE_RENDER_DPI / 72
    box = (max(image["x0"] * scale, 0), max(image["top"] * scale, 0),
           min(image["x1"] * scale, rendered.width), min(image["bottom"] * scale, rendered.height))
    if box[2] - box[0] < 20 or box[3] - box[1] < 20:
        return None
    name = f"page_{page_number}_block_{block_index}.png"
    rendered.crop(box).save(os.path.join(dest_dir, name))
    return f"/storage/extracted_images/{name}"


def chunk_pdf_by_layout(pdf_bytes: bytes, dest_dir: str, job_id: str) -> tuple:
    """Split every page into product-sized blocks with their own images (see app/rag/layout.py).

    Returns (chunks, stats). Falls back to one chunk per page - the previous behaviour - for
    any page pdfplumber cannot read.
    """
    chunks, stats = [], {"pages_with_text": 0, "image_failures": 0, "pages_fallback": 0}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_index, plumber_page in enumerate(pdf.pages):
            page_number = page_index + 1
            try:
                blocks = segment_words(plumber_page.extract_words())
            except Exception as e:
                log_stage(logger, job_id, "ingest", f"layout parsing failed on page {page_number}: {e}", level="warning")
                blocks = []
                stats["pages_fallback"] += 1

            kept = [b for b in blocks if len(b.text) >= MIN_CHUNK_CHARS]
            if not kept:
                continue
            stats["pages_with_text"] += 1

            rendered = None
            for block_index, block in enumerate(kept):
                image_path = None
                try:
                    if plumber_page.images:
                        if rendered is None:
                            rendered = plumber_page.to_image(resolution=IMAGE_RENDER_DPI).original
                        image_path = _crop_block_image(plumber_page, block, page_number, block_index, dest_dir, rendered)
                except Exception as e:
                    stats["image_failures"] += 1
                    log_stage(logger, job_id, "ingest", f"image crop failed on page {page_number}: {e}", level="warning")

                chunks.append({
                    "chunk_id": f"p{page_number}b{block_index}",
                    "page_number": page_number,
                    "block_index": block_index,
                    "content": block.text,
                    "images": [image_path] if image_path else [],
                })
            plumber_page.close()  # pdfplumber caches per-page objects; large catalogs need this
    return chunks, stats


def chunk_pdf_by_page(pdf_bytes: bytes, dest_dir: str, job_id: str) -> tuple:
    """Fallback chunking: one chunk per page, images picked by size (pypdf)."""
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    chunks, stats = [], {"pages_with_text": 0, "image_failures": 0, "pages_fallback": len(reader.pages)}
    for i, page in enumerate(reader.pages):
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        stats["pages_with_text"] += 1
        try:
            images = select_page_images(page, i + 1, dest_dir)
        except Exception as e:
            images = []
            stats["image_failures"] += 1
            log_stage(logger, job_id, "ingest", f"failed to extract images on page {i+1}: {e}", level="warning")
        chunks.append({"chunk_id": f"p{i + 1}b0", "page_number": i + 1, "block_index": 0,
                       "content": text, "images": images})
    return chunks, stats


def ingest_pdf(pdf_bytes: bytes, job_id: str = "unknown", filename: str = "catalog.pdf") -> dict:
    content_hash = hashlib.sha256(pdf_bytes).hexdigest()

    # Re-uploading byte-identical content skips re-embedding (saves Gemini quota) - unless the
    # stored vectors were mock ones from a run without a working key, which are worth replacing.
    current = get_catalog()
    if current and current["sha256"] == content_hash and current.get("embeddings") == "real":
        log_stage(logger, job_id, "ingest", f"content hash matches currently indexed catalog ({content_hash[:12]}...), skipping re-embed")
        return {"success": True, "indexed_pages": current["indexed_pages"],
                "indexed_chunks": current.get("indexed_chunks", current["indexed_pages"]),
                "collection_name": current.get("collection"), "reused": True}

    start = time.monotonic()
    log_stage(logger, job_id, "ingest", f"starting ingest of {len(pdf_bytes)} bytes")

    # 1. A fresh collection for this catalog (and goodbye to any previous one)
    collection = collection_for(content_hash)
    initialize_collection(collection)

    # Served at /storage/extracted_images/<name> by app.main
    backend_storage = str(settings.storage_dir / "extracted_images")
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

    _catalog_meta_path().write_text(json.dumps({
        "filename": filename,
        "sha256": content_hash,
        "collection": collection,
        "indexed_pages": indexed_count,
        "indexed_chunks": len(chunks),
        "indexed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "embeddings": diagnostics.get_status("embeddings")["mode"],
    }), encoding="utf-8")

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
