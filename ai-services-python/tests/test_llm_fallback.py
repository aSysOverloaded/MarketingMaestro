"""A failed call on the primary provider is retried once on the backup provider."""
import pytest
from langchain_core.prompts import ChatPromptTemplate

import app.ai.llm as llm
from app import diagnostics
from app.ai.schemas import ProfileOutput
from app.config import settings

PROMPT = ChatPromptTemplate.from_template("hi {x}")
ANSWER = ProfileOutput(segment="Adventure", budget_tier="Premium")


@pytest.fixture
def with_fallback(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_url", "https://primary.example/v1")
    monkeypatch.setattr(settings, "llm_api_key", "primary-key")
    monkeypatch.setattr(settings, "llm_model", "primary/model")
    monkeypatch.setattr(settings, "llm_fallback_api_url", "https://backup.example/v1")
    monkeypatch.setattr(settings, "llm_fallback_api_key", "backup-key")
    monkeypatch.setattr(settings, "llm_fallback_model", "backup/model")


def _stub(monkeypatch, outcomes):
    """outcomes: {provider: Exception or result}"""
    seen = []

    def fake(purpose, prompt, schema, inputs, provider):
        seen.append(provider)
        outcome = outcomes[provider]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(llm, "_invoke_once", fake)
    return seen


def test_primary_success_never_calls_the_backup(with_fallback, monkeypatch):
    seen = _stub(monkeypatch, {"primary": ANSWER})
    assert llm.invoke_structured("profile", PROMPT, ProfileOutput, {"x": 1}, "j") is ANSWER
    assert seen == ["primary"]
    assert diagnostics.get_status("llm.profile")["mode"] == "real"
    assert not llm.used_fallback("profile")


def test_primary_failure_is_retried_on_the_backup(with_fallback, monkeypatch):
    seen = _stub(monkeypatch, {"primary": RuntimeError("429 daily cap"), "fallback": ANSWER})
    assert llm.invoke_structured("writer", PROMPT, ProfileOutput, {"x": 1}, "j") is ANSWER
    assert seen == ["primary", "fallback"]
    assert llm.used_fallback("writer")
    assert "429 daily cap" in diagnostics.get_status("llm.writer")["detail"]


def test_both_providers_failing_raises_and_is_recorded(with_fallback, monkeypatch):
    _stub(monkeypatch, {"primary": RuntimeError("503 overloaded"), "fallback": RuntimeError("also down")})
    with pytest.raises(RuntimeError, match="also down"):
        llm.invoke_structured("critic", PROMPT, ProfileOutput, {"x": 1}, "j")
    status = diagnostics.get_status("llm.critic")
    assert status["mode"] == "failed" and "503 overloaded" in status["detail"] and "also down" in status["detail"]


def test_without_a_backup_configured_the_primary_error_propagates(monkeypatch):
    monkeypatch.setattr(settings, "llm_fallback_api_url", "")
    seen = _stub(monkeypatch, {"primary": RuntimeError("boom")})
    with pytest.raises(RuntimeError, match="boom"):
        llm.invoke_structured("planner", PROMPT, ProfileOutput, {"x": 1}, "j")
    assert seen == ["primary"]


def test_each_provider_uses_its_own_url_key_and_model(with_fallback, monkeypatch):
    monkeypatch.setattr(settings, "llm_critic_model", "strong/model")
    primary, backup = llm.get_chat_model("critic"), llm.get_chat_model("writer", llm.FALLBACK)
    assert (primary.model_name, str(primary.openai_api_base)) == ("strong/model", "https://primary.example/v1")
    assert (backup.model_name, str(backup.openai_api_base)) == ("backup/model", "https://backup.example/v1")
    assert backup.openai_api_key.get_secret_value() == "backup-key"


def test_pipeline_warns_when_the_backup_answered(with_fallback, monkeypatch):
    import app.pipeline.brochure as brochure
    from app.catalog import CustomerInput

    _stub(monkeypatch, {"primary": RuntimeError("503"), "fallback": ANSWER})
    ctx = brochure.JobContext(job_id="job_" + "0" * 32, trace_id="t",
                              customer=CustomerInput(age=40, income=90000, family_size=3, hobbies=["camping"], location="Denver"))
    brochure.profile_step(ctx)
    assert ctx.profile.segment == "Adventure"  # the backup's answer, not the rule-based fallback
    assert any("fallback provider" in w["message"] for w in ctx.warnings)
