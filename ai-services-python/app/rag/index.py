"""The vector index and the record of which catalog is in it.

One collection per catalog, named from the catalog's content hash: qdrant-client 1.9's on-disk
mode does not really drop a deleted collection's points, so reusing one name let an old
catalog's products surface in searches for a new one (docs/DECISIONS.md D18). The catalog
record (storage/catalog.json) also carries INGEST_VERSION, so an index built by older ingest
rules is rebuilt instead of served (D17).
"""
import json
import logging
import shutil
from typing import Optional

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from app.config import settings
from app.rag import extraction_cache, keyword
from app.rag.embeddings import VECTOR_DIMENSION

logger = logging.getLogger("rag.index")


# One collection per catalog, named from its content hash. qdrant-client 1.9's on-disk mode
# does not really drop a deleted collection's points - after delete + create they are still
# there - so reusing a single name left the previous catalog's products turning up in searches
# for the new one (884 points indexed for a 644-chunk catalogue). A fresh name per catalog
# sidesteps that entirely; the old collection is deleted too, for tidiness.
COLLECTION_PREFIX = "catalog_"


# What an index built by *this* code looks like. An indexed catalog is only reused when it was
# built by the same version, so changing how ingest works re-indexes on the next upload instead
# of quietly serving an index built by the old rules.
#
# Bump this whenever ingest output changes: chunking or segmentation, block filtering, the
# stored payload, embedding model or dimensions, image selection, brand detection.
#   1: one chunk per page
#   2: layout-aware product blocks, per-block images
#   3: non-product blocks filtered out, catalog brand detected
INGEST_VERSION = 3


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
    keyword.forget()
    current = get_catalog()
    if _collection_exists():
        get_client().delete_collection(_current_collection())
    _catalog_meta_path().unlink(missing_ok=True)
    if current:
        extraction_cache.clear(current["sha256"])


def initialize_collection(name: str) -> None:
    """Create an empty collection for this catalog, leaving any other catalog's alone.

    Only this one is touched: an ingest that fails half way (the embedding quota runs out, the
    process is killed) must not take the previously working catalog with it. Older collections
    are dropped by drop_other_collections() once the new one is indexed and recorded.
    """
    keyword.forget()
    client = get_client()
    if _collection_exists(name):
        client.delete_collection(collection_name=name)
    client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=VECTOR_DIMENSION, distance=Distance.COSINE),
    )


def _drop_other_image_folders(keep: str) -> None:
    """Remove images belonging to catalogs no longer indexed (one catalogue's crops are ~30 MB)."""
    root = settings.storage_dir / "extracted_images"
    if not root.is_dir():
        return
    for entry in root.iterdir():
        if entry.name != keep:
            shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink(missing_ok=True)


def drop_other_collections(keep: str) -> None:
    client = get_client()
    for existing in client.get_collections().collections:
        if existing.name.startswith(COLLECTION_PREFIX) and existing.name != keep:
            client.delete_collection(collection_name=existing.name)
