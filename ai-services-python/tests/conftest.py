import pytest

from app import diagnostics
from app.config import settings


def _offline(*_args, **_kwargs):
    raise RuntimeError("LLM offline (test)")


@pytest.fixture
def no_llm(monkeypatch):
    """Every chat-model call fails, forcing each step onto its fallback path."""
    import app.ai.critic
    import app.ai.evaluator
    import app.ai.llm
    import app.ai.planner
    import app.ai.writer

    for module in (app.ai.llm, app.ai.planner, app.ai.writer, app.ai.critic, app.ai.evaluator):
        monkeypatch.setattr(module, "get_chat_model", _offline)


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    """Generated files go to a temp dir, never the real storage/; PDFs off by default."""
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(settings, "disable_pdf", True)
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "smtp_user", "")
    diagnostics._status.clear()
    return tmp_path / "storage"
