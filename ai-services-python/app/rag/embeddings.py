"""Turning text into vectors, and surviving free-tier rate limits while doing it.

Embeddings are the one cost that scales with catalog size: one request per block, against a
free tier of 100 requests/minute and 1000/day. A batch that is rate-limited waits and retries
(a catalog whose later blocks hold mock vectors is silently broken for the rest of its life);
a *query* embedding, with a user waiting, retries once briefly and then degrades.
"""
import logging
import re
import time

import google.generativeai as genai

from app import diagnostics
from app.config import settings

logger = logging.getLogger("rag.embeddings")


VECTOR_DIMENSION = 768  # text-embedding-004 was retired; gemini-embedding-001 defaults to 3072


                        # dims but supports output_dimensionality to request this size instead
EMBEDDING_MODEL = "models/gemini-embedding-001"


# One request per EMBED_BATCH pages instead of one request for the whole catalog. A big
# catalog (hundreds of pages) made that single request fail, and the whole index silently
# fell back to mock vectors.
EMBED_BATCH = 50


# Embedding models cap input length; a page far longer than this adds nothing to retrieval.
MAX_EMBED_CHARS = 8000


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


def _is_rate_limit(error: Exception) -> bool:
    text = str(error).lower()
    return "429" in text or "quota" in text or "rate limit" in text


def _is_daily_cap(error: Exception) -> bool:
    """A per-minute cap clears in a minute; a per-day cap does not clear today, so retrying it
    just burns minutes before failing anyway."""
    text = str(error).lower()
    return "perday" in text.replace(" ", "") or "per day" in text or "requestsperday" in text.replace("_", "")


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
                if _is_daily_cap(e):
                    logger.error(f"[embed_texts] daily embedding quota reached; the rest of this catalog cannot be indexed today: {e}")
                    diagnostics.set_status("embeddings", "mock", f"daily quota reached: {e}")
                    vectors.extend([[0.1] * VECTOR_DIMENSION] * len(batch))
                    break
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
