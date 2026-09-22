"""Hybrid retrieval: BM25 keyword search fused with vector search.

Vector search is poor at identifiers - an embedding of "80000274" sits near every other product
code - so a catalogue's own codes and brand names were hard to find.
"""
from app.rag import keyword

CHUNKS = [
    {"chunk_id": "p1b0", "page_number": 1, "content": "GOLEM MATCH DAY SHIRT 80000000 softlock micromesh polyester"},
    {"chunk_id": "p1b1", "page_number": 1, "content": "WISP MATCH DAY SHIRT 80000274 softlock mesh polyester"},
    {"chunk_id": "p2b0", "page_number": 2, "content": "RHENIUM BASKETBALL BALL high density PU 8 panels"},
    {"chunk_id": "p3b0", "page_number": 3, "content": "A success story: our brand has designed sportswear since 1971"},
]


def _index():
    return keyword.KeywordIndex(CHUNKS)


def test_an_exact_product_code_finds_exactly_that_block():
    hits = _index().search("80000274", limit=3)
    assert hits and hits[0][0]["chunk_id"] == "p1b1"


def test_rarer_words_outrank_common_ones():
    # "shirt" appears in two blocks, "rhenium" in one: the rare term should win its block
    hits = _index().search("rhenium shirt", limit=4)
    assert hits[0][0]["chunk_id"] == "p2b0"


def test_a_query_with_no_matching_terms_returns_nothing():
    assert _index().search("kayak paddle", limit=3) == []
    assert keyword.KeywordIndex([]).search("anything", limit=3) == []


def test_fusion_ranks_a_block_found_by_both_engines_first():
    vector = [{"chunk_id": "p2b0", "score": 0.7}, {"chunk_id": "p1b0", "score": 0.6}]
    keyword_hits = [{"chunk_id": "p1b0", "score": 9.1}, {"chunk_id": "p3b0", "score": 2.0}]

    fused = keyword.fuse(vector, keyword_hits, limit=3)

    assert fused[0]["chunk_id"] == "p1b0"  # 2nd for vector, 1st for keyword, but found by both
    assert sorted(fused[0]["matched_by"]) == ["keyword", "vector"]
    assert fused[0]["score"] == 0.6  # the vector score is kept for display


def test_a_keyword_only_hit_survives_fusion_and_has_no_vector_score():
    fused = keyword.fuse([{"chunk_id": "p2b0", "score": 0.7}], [{"chunk_id": "p1b1", "score": 5.0}], limit=5)
    by_id = {hit["chunk_id"]: hit for hit in fused}
    assert by_id["p1b1"]["matched_by"] == ["keyword"] and "score" not in by_id["p1b1"]


def test_the_index_is_cached_per_collection_and_dropped_on_reindex():
    calls = []

    def load():
        calls.append(1)
        return CHUNKS

    keyword.forget()
    keyword.get_index("catalog_a", load)
    keyword.get_index("catalog_a", load)
    assert len(calls) == 1, "rebuilt an index it already had"

    keyword.get_index("catalog_b", load)  # a different catalog must not reuse it
    assert len(calls) == 2

    keyword.forget()
    keyword.get_index("catalog_b", load)
    assert len(calls) == 3
