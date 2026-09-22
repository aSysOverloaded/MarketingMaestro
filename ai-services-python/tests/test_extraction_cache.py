"""Extraction results are cached per catalog chunk, so repeat runs skip the LLM call."""
import app.pipeline.brochure as brochure
from app.catalog import CustomerInput
from app.rag import extraction_cache

SHA = "abc123def456"
CUSTOMER = CustomerInput(age=40, income=90000, family_size=3, hobbies=["camping"], location="Denver")


def test_round_trip_and_clear():
    extraction_cache.put_many(SHA, {"p1b0": [{"id": "tent", "model": "Trail Tent"}]})
    assert extraction_cache.get_many(SHA, ["p1b0", "p9b9"]) == {"p1b0": [{"id": "tent", "model": "Trail Tent"}]}
    assert extraction_cache.get_many("other-catalog-sha", ["p1b0"]) == {}  # keyed by catalog

    extraction_cache.clear(SHA)
    assert extraction_cache.get_many(SHA, ["p1b0"]) == {}


def test_a_corrupt_cache_file_is_ignored(isolated_storage):
    path = isolated_storage / "extraction_cache" / f"{SHA[:16]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert extraction_cache.get_many(SHA, ["p1b0"]) == {}


def _matches():
    return [
        {"chunk_id": "p5b0", "page_number": 5, "block_index": 0, "score": 0.9,
         "content": "GOLEM MATCH DAY SHIRT polyester", "images": ["/storage/extracted_images/page_5_block_0.png"]},
        {"chunk_id": "p5b1", "page_number": 5, "block_index": 1, "score": 0.8,
         "content": "WISP MATCH DAY SHORTS polyester", "images": ["/storage/extracted_images/page_5_block_1.png"]},
    ]


def test_second_run_reuses_cached_products(monkeypatch):
    from app.catalog import Product

    calls = []

    def fake_extract(text, job_id):
        calls.append(text)
        return [Product(id="golem", model="GOLEM", page_number=5), Product(id="wisp", model="WISP", page_number=5)]

    monkeypatch.setattr(brochure, "extract_products", fake_extract)
    monkeypatch.setattr(brochure, "get_catalog", lambda: {"sha256": SHA})

    ctx = brochure.JobContext(job_id="job_" + "0" * 32, trace_id="t", customer=CUSTOMER)
    first = brochure._extract_with_cache(ctx, _matches())
    assert len(calls) == 1 and {p.model for p in first} == {"GOLEM", "WISP"}
    assert ctx.review["extraction_cache"] == {"hits": 0, "misses": 2}

    ctx2 = brochure.JobContext(job_id="job_" + "1" * 32, trace_id="t", customer=CUSTOMER)
    second = brochure._extract_with_cache(ctx2, _matches())
    assert len(calls) == 1, "second run should not call the extractor again"
    assert {p.model for p in second} == {"GOLEM", "WISP"}
    assert ctx2.review["extraction_cache"] == {"hits": 2, "misses": 0}


def test_each_product_gets_the_image_from_its_own_block():
    from app.catalog import Product

    matches = _matches()
    wisp = Product(id="wisp", model="WISP MATCH DAY SHORTS", page_number=5)
    chunk = brochure._match_product_to_chunk(wisp, matches)
    assert chunk["chunk_id"] == "p5b1"  # not the first block on the page

    unknown = Product(id="x", model="Something Else", page_number=5)
    assert brochure._match_product_to_chunk(unknown, matches)["page_number"] == 5  # falls back to the page
