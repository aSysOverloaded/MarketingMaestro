"""The indexed catalog persists across runs and restarts, and is reused when no file is sent."""
from fastapi.testclient import TestClient

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
    search._client.close()
    search._client = None


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
    meta = search._catalog_meta_path()
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
    monkeypatch.setattr(search.genai, "configure", lambda **kw: None)
    batches = []

    def fake_embed(model, content, task_type, output_dimensionality):
        batches.append(len(content))
        assert all(len(c) <= search.MAX_EMBED_CHARS for c in content)
        return {"embedding": [[0.2] * search.VECTOR_DIMENSION] * len(content)}

    monkeypatch.setattr(search.genai, "embed_content", fake_embed)

    pages = [f"page {i} " + "x" * 20000 for i in range(120)]
    vectors = search.embed_texts(pages)
    assert len(vectors) == 120
    assert batches == [search.EMBED_BATCH, search.EMBED_BATCH, 20]
    assert search.diagnostics.get_status("embeddings")["mode"] == "real"


def test_one_failing_batch_does_not_fake_the_whole_catalog(monkeypatch):
    """A non-retryable failure degrades only its own batch (rate limits are retried instead;
    see test_rate_limited_batches_wait_and_retry)."""
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(search.genai, "configure", lambda **kw: None)
    calls = {"n": 0}

    def flaky(model, content, task_type, output_dimensionality):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("malformed request")
        return {"embedding": [[0.2] * search.VECTOR_DIMENSION] * len(content)}

    monkeypatch.setattr(search.genai, "embed_content", flaky)

    vectors = search.embed_texts([f"page {i}" for i in range(120)])
    assert len(vectors) == 120
    assert vectors[0][0] == 0.2 and vectors[60][0] == 0.1  # batch 1 real, batch 2 mock
    assert search.diagnostics.get_status("embeddings")["mode"] == "mock"


def test_rate_limited_batches_wait_and_retry(monkeypatch):
    """Free-tier embedding quota is per minute, so a big catalog hits it mid-ingest. Waiting
    keeps the catalog fully indexed instead of leaving later pages on mock vectors."""
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(search.genai, "configure", lambda **kw: None)
    slept = []
    monkeypatch.setattr(search.time, "sleep", slept.append)
    calls = {"n": 0}

    def rate_limited_once(model, content, task_type, output_dimensionality):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429 quota exceeded ... retry_delay { seconds: 21 }")
        return {"embedding": [[0.3] * search.VECTOR_DIMENSION] * len(content)}

    monkeypatch.setattr(search.genai, "embed_content", rate_limited_once)

    vectors = search.embed_texts([f"page {i}" for i in range(10)])
    assert slept == [23]  # provider's own retry_delay + a margin
    assert vectors[0][0] == 0.3  # real vectors, not the mock fallback
    assert search.diagnostics.get_status("embeddings")["mode"] == "real"


def test_query_embeddings_give_up_quickly(monkeypatch):
    """A query embedding runs while the user waits: retry once, briefly, then degrade. Ingest
    (test above) waits much longer because it is a one-off cost and mock pages stay wrong."""
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(search.genai, "configure", lambda **kw: None)
    slept = []
    monkeypatch.setattr(search.time, "sleep", slept.append)

    def always_rate_limited(**kw):
        raise RuntimeError("429 quota exceeded ... retry_delay { seconds: 45 }")

    monkeypatch.setattr(search.genai, "embed_content", always_rate_limited)

    vector = search.embed_text("gear for camping", is_query=True)
    assert slept == [search.MAX_QUERY_WAIT_SECONDS]  # capped, not the provider's 45s
    assert vector == [0.1] * search.VECTOR_DIMENSION  # degraded, and reported as mock
    assert search.diagnostics.get_status("embeddings")["mode"] == "mock"
