"""Keyword (BM25) search over the indexed blocks, to sit alongside vector search.

Vector search is good at meaning and bad at identifiers: a customer or salesperson searching
"80000274" or "TurboWash" wants the block containing that exact token, and an embedding of a
product code is close to every other product code. BM25 is the opposite. Running both and
fusing the rankings covers each other's blind spot.

Small enough to keep in memory: a 133-page catalogue is ~450 blocks. The index is rebuilt when
the collection changes, so it never serves a catalog that is no longer loaded.
"""
import math
import re
from collections import Counter
from typing import Dict, List, Optional, Tuple

# BM25 constants: the usual defaults. k1 damps repeated terms, b controls length normalisation.
K1 = 1.5
B = 0.75

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> List[str]:
    return _TOKEN.findall(text.lower())


class KeywordIndex:
    """A BM25 index over chunk payloads, keyed by chunk id."""

    def __init__(self, chunks: List[dict]):
        self.chunks = chunks
        self.tokens = [tokenize(c.get("content") or "") for c in chunks]
        self.lengths = [len(t) for t in self.tokens]
        self.average_length = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        self.term_frequencies = [Counter(t) for t in self.tokens]
        document_frequency: Counter = Counter()
        for tokens in self.tokens:
            document_frequency.update(set(tokens))
        self.document_frequency = document_frequency
        self.total = len(chunks)

    def _idf(self, term: str) -> float:
        n = self.document_frequency.get(term, 0)
        if n == 0:
            return 0.0
        return math.log(1 + (self.total - n + 0.5) / (n + 0.5))

    def search(self, query: str, limit: int) -> List[Tuple[dict, float]]:
        terms = tokenize(query)
        if not terms or not self.total:
            return []

        scored = []
        for i, frequencies in enumerate(self.term_frequencies):
            score = 0.0
            for term in terms:
                tf = frequencies.get(term, 0)
                if not tf:
                    continue
                norm = 1 - B + B * (self.lengths[i] / self.average_length if self.average_length else 1)
                score += self._idf(term) * (tf * (K1 + 1)) / (tf + K1 * norm)
            if score > 0:
                scored.append((self.chunks[i], score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]


_cache: Dict[str, KeywordIndex] = {}


def get_index(collection: str, load_chunks) -> KeywordIndex:
    """Cached per collection; `load_chunks` is only called on a miss."""
    if collection not in _cache:
        _cache.clear()  # only one catalog is loaded at a time
        _cache[collection] = KeywordIndex(load_chunks())
    return _cache[collection]


def forget(collection: Optional[str] = None) -> None:
    _cache.pop(collection, None) if collection else _cache.clear()


def fuse(vector_hits: List[dict], keyword_hits: List[dict], limit: int, k: int = 60) -> List[dict]:
    """Reciprocal rank fusion: score each result by 1/(k+rank) in each list and add.

    Rank-based rather than score-based because a cosine similarity and a BM25 score are not on
    comparable scales, and normalising them invents a relationship that isn't there.
    """
    fused: Dict[str, dict] = {}
    for hits, source in ((vector_hits, "vector"), (keyword_hits, "keyword")):
        for rank, hit in enumerate(hits):
            # "score" means cosine similarity everywhere else, so a BM25 score must not be
            # carried in that field: it is a different scale and would be read as a similarity.
            entry = fused.setdefault(hit["chunk_id"],
                                     {**{key: value for key, value in hit.items() if key != "score"},
                                      "fused_score": 0.0, "matched_by": []})
            entry["fused_score"] += 1 / (k + rank + 1)
            entry["matched_by"].append(source)
            entry["score" if source == "vector" else "keyword_score"] = hit["score"]
    ordered = sorted(fused.values(), key=lambda h: h["fused_score"], reverse=True)
    return ordered[:limit]
