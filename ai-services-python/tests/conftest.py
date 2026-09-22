import pytest

from app import diagnostics
from app.config import settings


def _offline(*_args, **_kwargs):
    raise RuntimeError("LLM offline (test)")


@pytest.fixture
def no_llm(monkeypatch):
    """Every chat-model call fails, forcing each step onto its deterministic fallback.
    One patch point covers every AI module, since they all go through invoke_structured."""
    import app.ai.llm

    monkeypatch.setattr(app.ai.llm, "_invoke_once", _offline)


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    """Generated files and the Qdrant index go to a temp dir, never the real storage/.
    PDFs off, SMTP off, and no Gemini key, so embeddings are local mock vectors."""
    import app.rag.search as search

    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(settings, "disable_pdf", True)
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(search, "_client", None)
    diagnostics._status.clear()
    yield tmp_path / "storage"
    if search._client is not None:
        search._client.close()


@pytest.fixture
def submit_job():
    """POST /api/recommend, then poll GET /api/jobs/{id} until it finishes; returns the job."""
    import time

    def submit(client, data, files=None, timeout=30):
        resp = client.post("/api/recommend", data=data, files=files)
        assert resp.status_code == 202, resp.text
        status_url = resp.json()["status_url"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = client.get(status_url).json()
            if job["status"] != "running":
                return job
            time.sleep(0.05)
        raise AssertionError(f"job did not finish within {timeout}s: {job}")

    return submit
