"""Cache of products already extracted from a catalog chunk.

Extraction is an LLM call over the matched pages, and the same popular chunks match run after
run. The catalog's content hash is part of the key, so a re-uploaded (changed) catalog never
reads a stale entry; forgetting the catalog drops its cache file.
"""
import json
import logging
import threading
from typing import Dict, List, Optional

from app.config import settings

logger = logging.getLogger("rag.cache")
_lock = threading.Lock()


def _path(catalog_sha: str):
    return settings.storage_dir / "extraction_cache" / f"{catalog_sha[:16]}.json"


def _load(catalog_sha: str) -> Dict[str, list]:
    path = _path(catalog_sha)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # a corrupt cache must never break a run
        logger.warning(f"ignoring unreadable extraction cache {path}: {e}")
        return {}


def get_many(catalog_sha: str, chunk_ids: List[str]) -> Dict[str, list]:
    """Cached product dicts for the chunk ids that have them."""
    with _lock:
        cached = _load(catalog_sha)
    return {cid: cached[cid] for cid in chunk_ids if cid in cached}


def put_many(catalog_sha: str, products_by_chunk: Dict[str, list]) -> None:
    if not products_by_chunk:
        return
    with _lock:
        cached = _load(catalog_sha)
        cached.update(products_by_chunk)
        path = _path(catalog_sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cached), encoding="utf-8")


def clear(catalog_sha: Optional[str] = None) -> None:
    with _lock:
        directory = settings.storage_dir / "extraction_cache"
        if not directory.is_dir():
            return
        for path in ([_path(catalog_sha)] if catalog_sha else list(directory.glob("*.json"))):
            path.unlink(missing_ok=True)
