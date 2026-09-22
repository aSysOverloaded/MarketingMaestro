"""The indexed catalog persists across runs and restarts, and is reused when no file is sent."""
from fastapi.testclient import TestClient

from app import diagnostics
from app.rag import blocks, embeddings, index
import app.rag.search as search


def _pdf(text: str) -> bytes:
    """Minimal one-page PDF with extractable text."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


PDF = _pdf("Trail Tent 2-person camping tent")


def _restart():
    index._client.close()
    index._client = None


def test_catalog_survives_restart_and_mock_embeddings_are_redone():
    result = search.ingest_pdf(PDF, filename="tents.pdf")
    assert result["indexed_pages"] == 1 and not result["reused"]

    _restart()
    catalog = search.get_catalog()
    assert catalog["filename"] == "tents.pdf" and catalog["embeddings"] == "mock"

    # Same bytes, but the stored vectors are mock ones: worth re-embedding.
    assert not search.ingest_pdf(PDF, filename="tents.pdf")["reused"]


def test_identical_upload_with_real_embeddings_is_not_re_embedded(monkeypatch):
    search.ingest_pdf(PDF, filename="tents.pdf")
    meta = index._catalog_meta_path()
    meta.write_text(meta.read_text().replace('"mock"', '"real"'))
    assert search.ingest_pdf(PDF, filename="tents.pdf")["reused"]


def test_run_without_upload_reuses_the_indexed_catalog_until_forgotten(no_llm, submit_job):
    from app.main import app

    client = TestClient(app)
    form = {"age": "40", "income": "90000", "family_size": "3", "location": "Denver", "hobbies": "camping"}

    first_job = submit_job(client, form, files={"brochure": ("tents.pdf", PDF, "application/pdf")})
    assert first_job["steps"][0]["name"] == "ingest" and first_job["steps"][0]["status"] == "done"
    first = first_job["result"]
    assert first["catalog"]["source"] == "uploaded"

    second = submit_job(client, form)["result"]
    assert second["catalog"]["filename"] == "tents.pdf" and second["catalog"]["source"] == "reused"
    assert second["rag_debug"]["active"]

    assert client.delete("/api/rag/catalog").status_code == 200
    third = submit_job(client, form)["result"]
    assert third["catalog"] is None and not third["rag_debug"]["active"]
    assert client.get("/api/rag/stats").json()["catalog"] is None


def test_large_catalogs_are_embedded_in_batches(monkeypatch):
    """One request per EMBED_BATCH pages. A single request for a whole large catalog used to
    fail and drop the entire index to mock vectors."""
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(embeddings.genai, "configure", lambda **kw: None)
    batches = []

    def fake_embed(model, content, task_type, output_dimensionality):
        batches.append(len(content))
        assert all(len(c) <= embeddings.MAX_EMBED_CHARS for c in content)
        return {"embedding": [[0.2] * embeddings.VECTOR_DIMENSION] * len(content)}

    monkeypatch.setattr(embeddings.genai, "embed_content", fake_embed)

    pages = [f"page {i} " + "x" * 20000 for i in range(120)]
    vectors = embeddings.embed_texts(pages)
    assert len(vectors) == 120
    assert batches == [embeddings.EMBED_BATCH, embeddings.EMBED_BATCH, 20]
    assert diagnostics.get_status("embeddings")["mode"] == "real"


def test_one_failing_batch_does_not_fake_the_whole_catalog(monkeypatch):
    """A non-retryable failure degrades only its own batch (rate limits are retried instead;
    see test_rate_limited_batches_wait_and_retry)."""
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(embeddings.genai, "configure", lambda **kw: None)
    calls = {"n": 0}

    def flaky(model, content, task_type, output_dimensionality):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("malformed request")
        return {"embedding": [[0.2] * embeddings.VECTOR_DIMENSION] * len(content)}

    monkeypatch.setattr(embeddings.genai, "embed_content", flaky)

    vectors = embeddings.embed_texts([f"page {i}" for i in range(120)])
    assert len(vectors) == 120
    assert vectors[0][0] == 0.2 and vectors[60][0] == 0.1  # batch 1 real, batch 2 mock
    assert diagnostics.get_status("embeddings")["mode"] == "mock"


def test_rate_limited_batches_wait_and_retry(monkeypatch):
    """Free-tier embedding quota is per minute, so a big catalog hits it mid-ingest. Waiting
    keeps the catalog fully indexed instead of leaving later pages on mock vectors."""
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(embeddings.genai, "configure", lambda **kw: None)
    slept = []
    monkeypatch.setattr(embeddings.time, "sleep", slept.append)
    calls = {"n": 0}

    def rate_limited_once(model, content, task_type, output_dimensionality):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429 quota exceeded ... retry_delay { seconds: 21 }")
        return {"embedding": [[0.3] * embeddings.VECTOR_DIMENSION] * len(content)}

    monkeypatch.setattr(embeddings.genai, "embed_content", rate_limited_once)

    vectors = embeddings.embed_texts([f"page {i}" for i in range(10)])
    assert slept == [23]  # provider's own retry_delay + a margin
    assert vectors[0][0] == 0.3  # real vectors, not the mock fallback
    assert diagnostics.get_status("embeddings")["mode"] == "real"


def test_query_embeddings_give_up_quickly(monkeypatch):
    """A query embedding runs while the user waits: retry once, briefly, then degrade. Ingest
    (test above) waits much longer because it is a one-off cost and mock pages stay wrong."""
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(embeddings.genai, "configure", lambda **kw: None)
    slept = []
    monkeypatch.setattr(embeddings.time, "sleep", slept.append)

    def always_rate_limited(**kw):
        raise RuntimeError("429 quota exceeded ... retry_delay { seconds: 45 }")

    monkeypatch.setattr(embeddings.genai, "embed_content", always_rate_limited)

    vector = embeddings.embed_text("gear for camping", is_query=True)
    assert slept == [embeddings.MAX_QUERY_WAIT_SECONDS]  # capped, not the provider's 45s
    assert vector == [0.1] * embeddings.VECTOR_DIMENSION  # degraded, and reported as mock
    assert diagnostics.get_status("embeddings")["mode"] == "mock"


def test_ingesting_a_new_catalog_drops_the_previous_one(monkeypatch):
    """A stale catalog must not survive into the next one's index: an on-disk collection kept
    the old points, so searches for a new catalog returned products from the previous one."""
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "")  # mock vectors: no API needed

    search.ingest_pdf(_pdf("Trail Tent 2-person camping tent"), filename="first.pdf")
    first_points = index.get_client().get_collection(index._current_collection()).points_count
    assert first_points >= 1

    search.ingest_pdf(_pdf("Rhenium Basketball Ball size 7"), filename="second.pdf")
    contents = " ".join(
        (p.payload.get("content") or "")
        for p in index.get_client().scroll(collection_name=index._current_collection(), limit=100, with_payload=True)[0]
    )
    assert "Rhenium" in contents
    assert "Trail Tent" not in contents
    assert search.get_catalog()["filename"] == "second.pdf"


def test_non_product_blocks_are_dropped_when_the_catalog_prices_things():
    """Covers, index and brand-story pages cost an embedding request each and compete in
    search (a real run matched the cover page)."""
    chunks = [{"content": c} for c in [
        "RHENIUM BASKETBALL BALL UVP 59,99", "PROMETIUM BALL 80000274 8 panels",
        "NOBIUM PRO BALL EUR 98.99", "A SUCCESS STORY since the beginning we have designed",
    ]]
    kept = [c["content"] for c in blocks.filter_product_blocks(chunks)]
    assert len(kept) == 3 and not any("SUCCESS STORY" in c for c in kept)


def test_a_catalog_without_prices_or_codes_is_kept_whole():
    chunks = [{"content": t} for t in ["Trail Tent, ripstop nylon", "Camp Stove, piezo ignition", "About our company"]]
    assert len(blocks.filter_product_blocks(chunks)) == 3  # filtering would throw it all away


def test_changing_how_ingest_works_reindexes_the_same_catalog(monkeypatch):
    """Reuse must consider the code that built the index, not just the PDF's hash. Otherwise a
    catalog indexed by older rules (page-sized chunks, unfiltered pages) is served forever."""
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "")
    assert not search.ingest_pdf(PDF, filename="c.pdf")["reused"]
    assert search.get_catalog()["ingest_version"] == index.INGEST_VERSION
    # same bytes, same ingest version: no work
    meta = index._catalog_meta_path()
    meta.write_text(meta.read_text().replace('"mock"', '"real"'))
    assert search.ingest_pdf(PDF, filename="c.pdf")["reused"]

    # a newer ingest version invalidates it, with no user action
    monkeypatch.setattr(search, "INGEST_VERSION", index.INGEST_VERSION + 1)
    assert not search.ingest_pdf(PDF, filename="c.pdf")["reused"]
    assert search.get_catalog()["ingest_version"] == search.INGEST_VERSION


def test_reindexing_drops_products_cached_against_the_old_chunks(monkeypatch):
    from app.config import settings
    from app.rag import extraction_cache

    monkeypatch.setattr(settings, "gemini_api_key", "")
    search.ingest_pdf(PDF, filename="c.pdf")
    sha = search.get_catalog()["sha256"]
    extraction_cache.put_many(sha, {"p1b0": [{"id": "old", "model": "From the old chunking"}]})

    monkeypatch.setattr(search, "INGEST_VERSION", index.INGEST_VERSION + 1)
    search.ingest_pdf(PDF, filename="c.pdf")
    assert extraction_cache.get_many(sha, ["p1b0"]) == {}


def test_a_failed_ingest_keeps_the_previous_catalog(monkeypatch):
    """A half-finished ingest (quota gone, process killed) must not leave the app with no
    catalog: the working one stays until the new index actually exists."""
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "")
    search.ingest_pdf(PDF, filename="good.pdf")
    good = search.get_catalog()
    assert good["filename"] == "good.pdf"

    empty_pdf = _pdf("")  # nothing indexable
    result = search.ingest_pdf(empty_pdf, filename="broken.pdf")
    assert result["success"] is False and result["indexed_chunks"] == 0
    assert search.get_catalog() == good, "the working catalog was replaced by a failed ingest"


def test_a_daily_quota_is_not_retried(monkeypatch):
    """A per-minute cap clears in a minute and is worth waiting for; a per-day cap is not."""
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(embeddings.genai, "configure", lambda **kw: None)
    slept = []
    monkeypatch.setattr(embeddings.time, "sleep", slept.append)

    def daily_cap(**kw):
        raise RuntimeError('429 quota exceeded quota_id: "EmbedContentRequestsPerDayPerUserPerProjectPerModel-FreeTier"')

    monkeypatch.setattr(embeddings.genai, "embed_content", daily_cap)
    vectors = embeddings.embed_texts(["page one", "page two"])

    assert slept == [], "waited on a quota that will not clear today"
    assert vectors == [[0.1] * embeddings.VECTOR_DIMENSION] * 2
    assert "daily quota" in diagnostics.get_status("embeddings")["detail"]
