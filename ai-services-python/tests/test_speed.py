"""The copy step's speed optimisations, verified with fake LLMs that just sleep."""
import time

import pytest

import app.pipeline.brochure as brochure
from app.catalog import CustomerInput
from app.config import settings

CUSTOMER = CustomerInput(age=40, income=90000, family_size=3, hobbies=["camping"], location="Denver, CO")
GROUNDED = {"headline": "H", "subheadline": "S", "paragraphs": ["Triple Cooling System keeps food fresh."], "cta": "C"}
LLM_DELAY = 0.4


def run():
    ctx = brochure.JobContext(job_id="job_" + "0" * 32, trace_id="t", customer=CUSTOMER)
    brochure.build_workflow().run(ctx)
    return ctx


@pytest.fixture(autouse=True)
def tone_evaluator_on(monkeypatch):
    """These tests are about the review's shape, so they run with every reviewer enabled."""
    monkeypatch.setattr(settings, "use_llm_tone_evaluator", True)


def _fake_reviewers(monkeypatch, calls):
    def critic(copy, candidate, job_id):
        calls.append("critic")
        time.sleep(LLM_DELAY)
        return {"passed": True, "feedback": "ok"}

    def evaluator(copy, job_id):
        calls.append("evaluator")
        time.sleep(LLM_DELAY)
        return {"passed": True, "banned_words_found": [], "tone_assessment": "fine", "score": 90, "degraded": False}

    monkeypatch.setattr(brochure, "audit_copy", critic)
    monkeypatch.setattr(brochure, "evaluate_copy", evaluator)


def _fake_writer(monkeypatch, drafts):
    drafts = iter(drafts)
    monkeypatch.setattr(brochure, "generate_copy", lambda *a, **k: next(drafts))
    # Pin the top product to the fridge so GROUNDED really is grounded.
    monkeypatch.setattr(brochure, "rank_products", lambda customer, seg, tier, cands, job_id: brochure.score_products(customer, cands[:1], job_id))


def test_grounded_draft_goes_to_both_reviewers(no_llm, monkeypatch):
    calls = []
    _fake_reviewers(monkeypatch, calls)
    _fake_writer(monkeypatch, [GROUNDED])

    ctx = run()
    assert sorted(calls) == ["critic", "evaluator"]
    assert all(c["ms"] >= LLM_DELAY * 1000 * 0.9 for c in ctx.review["calls"] if c["call"] != "writer")
    assert ctx.copy == GROUNDED


def test_review_wall_time_is_max_not_sum(no_llm, monkeypatch):
    # Sequential review would take >= 2 x LLM_DELAY; in parallel it is ~1 x.
    _fake_reviewers(monkeypatch, [])
    _fake_writer(monkeypatch, [GROUNDED])
    ctx = brochure.JobContext(job_id="job_" + "0" * 32, trace_id="t", customer=CUSTOMER)
    for step in brochure.build_workflow().steps[:3]:
        step.run(ctx)

    start = time.monotonic()
    brochure.copy_step(ctx)
    elapsed = time.monotonic() - start
    assert elapsed < LLM_DELAY * 1.6, f"review took {elapsed:.2f}s; expected ~{LLM_DELAY}s in parallel"


def test_llm_reviews_are_skipped_when_the_spec_check_already_failed(no_llm, monkeypatch):
    calls = []
    _fake_reviewers(monkeypatch, calls)
    invented = {**GROUNDED, "paragraphs": ["Control it from the SmartThings app."]}
    _fake_writer(monkeypatch, [invented, GROUNDED])

    ctx = run()
    assert ctx.copy == GROUNDED
    assert ctx.review["skipped_llm_reviews"] == 1
    assert sorted(calls) == ["critic", "evaluator"]  # only the second draft went to the LLMs
    assert [c["call"] for c in ctx.review["calls"] if c["draft"] == 1] == ["writer"]


def test_banned_words_trigger_a_revision_without_llm_calls(no_llm, monkeypatch):
    calls = []
    _fake_reviewers(monkeypatch, calls)
    cheap = {**GROUNDED, "headline": "Cheap and cheerful"}
    feedback_seen = []
    drafts = iter([cheap, GROUNDED])

    def writer(*a, feedback="", **k):
        feedback_seen.append(feedback)
        return next(drafts)

    monkeypatch.setattr(brochure, "generate_copy", writer)
    monkeypatch.setattr(brochure, "rank_products", lambda customer, seg, tier, cands, job_id: brochure.score_products(customer, cands[:1], job_id))

    ctx = run()
    assert "cheap" in feedback_seen[1]
    assert ctx.copy == GROUNDED and sorted(calls) == ["critic", "evaluator"]


def test_optional_llm_calls_are_off_by_default(no_llm, monkeypatch):
    """Profile, planner and tone evaluator are deliberate omissions, not failures: no LLM
    call, no warning, and the copy still gets written and fact-checked."""
    monkeypatch.setattr(settings, "use_llm_tone_evaluator", False)
    calls = []
    _fake_reviewers(monkeypatch, calls)
    _fake_writer(monkeypatch, [GROUNDED])

    ctx = run()
    assert calls == ["critic"]  # evaluator not called
    assert ctx.profile.segment == "Adventure" and not any(w["step"] == "profile" for w in ctx.warnings)
    assert ctx.sections == [] and not any(w["step"] == "plan" for w in ctx.warnings)
    assert ctx.copy == GROUNDED  # still written and reviewed
    assert "evaluator" not in ctx.review


def test_banned_words_are_still_enforced_without_the_tone_evaluator(no_llm, monkeypatch):
    monkeypatch.setattr(settings, "use_llm_tone_evaluator", False)
    calls = []
    _fake_reviewers(monkeypatch, calls)
    feedback_seen = []
    drafts = iter([{**GROUNDED, "headline": "Cheap and cheerful"}, GROUNDED])

    def writer(*a, feedback="", **k):
        feedback_seen.append(feedback)
        return next(drafts)

    monkeypatch.setattr(brochure, "generate_copy", writer)
    monkeypatch.setattr(brochure, "rank_products", lambda customer, seg, tier, cands, job_id: brochure.score_products(customer, cands[:1], job_id))

    ctx = run()
    assert "cheap" in feedback_seen[1] and ctx.copy == GROUNDED
