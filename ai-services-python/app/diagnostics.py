"""Central registry for cross-cutting diagnostic state.

Every entry here must be driven by the outcome of the LAST ACTUAL call for that
subsystem - never by whether config (e.g. an API key) is merely present. A set
GEMINI_API_KEY does not guarantee calls actually succeed (bad key, quota, wrong
model name), and deriving status from config presence is exactly what let the
embedding pipeline silently return a constant mock vector while reporting fine.

This module holds state only; it has no opinion on HOW a subsystem decides its
status - that logic stays at the call site (e.g. app/rag/search.py), which is
also why LangChain wrapper types must never replace the call site directly.
"""
import threading
from typing import Any, Dict, Optional

_lock = threading.Lock()
_status: Dict[str, Dict[str, Any]] = {}
_ingest_meta: Dict[str, Any] = {}


def set_status(subsystem: str, mode: str, detail: Optional[str] = None) -> None:
    """Record the outcome of the most recent call for a subsystem.

    subsystem: a short dotted name, e.g. "embeddings", "llm.planner", "llm.evaluator"
    mode: e.g. "real", "mock", "failed", "degraded"
    detail: human-readable reason, typically an exception message
    """
    with _lock:
        _status[subsystem] = {"mode": mode, "detail": detail}


def get_status(subsystem: str) -> Dict[str, Any]:
    with _lock:
        return dict(_status.get(subsystem, {"mode": "unknown", "detail": None}))


def get_all_status() -> Dict[str, Dict[str, Any]]:
    with _lock:
        return {k: dict(v) for k, v in _status.items()}


def set_ingest_meta(meta: Dict[str, Any]) -> None:
    with _lock:
        _ingest_meta.clear()
        _ingest_meta.update(meta)


def get_ingest_meta() -> Optional[Dict[str, Any]]:
    with _lock:
        return dict(_ingest_meta) if _ingest_meta else None
