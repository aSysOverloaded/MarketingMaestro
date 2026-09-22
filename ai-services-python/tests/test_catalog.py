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
