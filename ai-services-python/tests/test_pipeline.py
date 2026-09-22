import app.pipeline.brochure as brochure
from app.catalog import CustomerInput, Product, Recommendation
from app.render.brochure import compile_html, embed_local_image
from fastapi.testclient import TestClient

CUSTOMER = CustomerInput(age=40, income=90000, family_size=3, hobbies=["camping"], location="Denver, CO")


def run(ctx=None):
    ctx = ctx or brochure.JobContext(job_id="job_" + "0" * 32, trace_id="trace_t", customer=CUSTOMER)
    brochure.build_workflow().run(ctx)
    return ctx


def test_offline_run_completes_and_reports_every_fallback(no_llm):
    ctx = run()
    steps = {w["step"] for w in ctx.warnings}
    # profile and plan make no LLM call by default (see docs/PIPELINE.md), so they cannot fail
    assert {"recommend", "copy", "pdf"} <= steps
    assert ctx.profile.segment == "Adventure"  # rule-based, from the "camping" hobby
    assert ctx.copy == brochure.fallback_copy("Adventure")
    assert ctx.html_path.is_file() and ctx.pdf_path is None
    assert ctx.review["ranker_removed"] == []  # the rule-based fallback's own reasons are grounded


def _stub_reviewed_copy(monkeypatch, critic_results):
    calls = []

    def writer(segment, sections, candidate, job_id, feedback=""):
        calls.append(feedback)
        return {"headline": "H", "subheadline": "S", "paragraphs": [f"draft {len(calls)}"], "cta": "C"}

    results = iter(critic_results)
    monkeypatch.setattr(brochure, "generate_copy", writer)
    monkeypatch.setattr(brochure, "audit_copy", lambda *a, **k: next(results))
    return calls


def test_critic_rejection_revises_with_feedback(no_llm, monkeypatch):
    calls = _stub_reviewed_copy(monkeypatch, [
        {"passed": False, "feedback": "capacity is 26.5, not 30"},
        {"passed": True, "feedback": "ok"},
    ])
    ctx = run()
    assert calls == ["", "Spec accuracy: capacity is 26.5, not 30"]
    assert ctx.copy["paragraphs"] == ["draft 2"]
    assert ctx.review["revisions"] == 1


def test_copy_rejected_every_time_falls_back_to_claim_free_copy(no_llm, monkeypatch):
    calls = _stub_reviewed_copy(monkeypatch, [{"passed": False, "feedback": "wrong"}] * 3)
    ctx = run()
    assert len(calls) == brochure.MAX_REVISIONS + 1
    assert ctx.copy == brochure.fallback_copy(ctx.profile.segment)
    assert any("still rejected" in w["message"] for w in ctx.warnings)


def test_compile_html_renders_copy_and_only_stated_specs(tmp_path):
    product = Product(id="p", model="Trail Tent", category="Tent")  # no price, capacity or features
    rec = Recommendation(recommendation_id="r", product_id="p", score=80, matched_rules=["Fits"], explanation="Good fit")
    copy = {"headline": "H", "subheadline": "S", "paragraphs": ["one", "two", "three", "four"], "cta": "Book a demo <now>"}
    html = compile_html(job_id="j", trace_id="t", segment="Adventure", copy=copy,
                        recommendations=[rec], products=[product], output_dir=tmp_path).read_text(encoding="utf-8")
    assert "three" in html and "four" not in html  # capped at MAX_COVER_PARAGRAPHS
    assert "Book a demo &lt;now&gt;" in html  # autoescaped
    assert "Price on request" in html and "$0" not in html

    priced = Product(id="p2", model="Rhenium Ball", base_price=59, currency="EUR")
    rec2 = Recommendation(recommendation_id="r2", product_id="p2", score=90, matched_rules=[], explanation="")
    euro_html = compile_html(job_id="j2", trace_id="t", segment="Adventure", copy=copy,
                             recommendations=[rec2], products=[priced], output_dir=tmp_path).read_text(encoding="utf-8")
    assert "€59.00" in euro_html and "$59.00" not in euro_html  # the catalog's currency, not a dollar sign
    assert "Certified" not in html and "Available" not in html


def test_embed_local_image_stays_inside_storage(isolated_storage):
    images = isolated_storage / "extracted_images"
    images.mkdir(parents=True)
    (images / "a.png").write_bytes(b"\x89PNG")
    assert embed_local_image("/storage/extracted_images/a.png").startswith("data:image/png;base64,")
    assert embed_local_image("/storage/../app/main.py") == "/storage/../app/main.py"
    assert embed_local_image("https://example.com/x.jpg") == "https://example.com/x.jpg"


def test_api_recommend_reports_progress_and_send_email(no_llm, submit_job):
    from app.main import app

    client = TestClient(app)
    job = submit_job(client, {"age": "40", "income": "90000", "family_size": "3", "location": "", "hobbies": "camping"})
    assert job["status"] == "done"
    assert [s["name"] for s in job["steps"]] == ["profile", "recommend", "plan", "copy", "html", "pdf"]
    assert all(s["status"] == "done" and s["ms"] is not None for s in job["steps"])
    body = job["result"]
    assert body["success"] and body["pdf_url"] is None and body["recommendations"][0]["model"]
    assert any(w["step"] == "input" and "location" in w["message"] for w in body["warnings"])

    assert client.post("/api/send-email", data={"job_id": "../../etc", "email": "a@b.co"}).status_code == 400
    assert client.post("/api/send-email", data={"job_id": body["job_id"], "email": "a@b.co"}).status_code == 404
    assert client.get("/api/jobs/job_unknown").status_code == 404


def test_failed_job_reports_the_failed_step(no_llm, submit_job, monkeypatch):
    import app.pipeline.brochure as brochure_module
    from app.main import app

    def broken_html(ctx):
        raise RuntimeError("template missing")

    monkeypatch.setattr(brochure_module, "html_step", broken_html)
    job = submit_job(TestClient(app), {"hobbies": "camping"})
    assert job["status"] == "failed"
    assert job["error"]["failed_step"] == "html" and "template missing" in job["error"]["error"]
    assert next(s for s in job["steps"] if s["name"] == "html")["status"] == "failed"
    assert any(w["step"] == "input" for w in job["error"]["warnings"])


def test_grounding_check_rejects_even_when_critic_passes(no_llm, monkeypatch):
    drafts = iter([
        {"headline": "H", "subheadline": "S", "paragraphs": ["Control it from the SmartThings app."], "cta": "C"},
        {"headline": "H", "subheadline": "S", "paragraphs": ["Triple Cooling System keeps food fresh."], "cta": "C"},
    ])
    feedback_seen = []

    def writer(segment, sections, candidate, job_id, feedback=""):
        feedback_seen.append(feedback)
        return next(drafts)

    monkeypatch.setattr(brochure, "generate_copy", writer)
    monkeypatch.setattr(brochure, "audit_copy", lambda *a, **k: {"passed": True, "feedback": "ok"})
    monkeypatch.setattr(brochure, "rank_products", lambda customer, seg, tier, cands, job_id: brochure.score_products(customer, cands[:1], job_id))

    ctx = run()
    assert "SmartThings" in feedback_seen[1]
    assert ctx.copy["paragraphs"] == ["Triple Cooling System keeps food fresh."]
    assert ctx.review["ungrounded_terms"] == []


def test_ranker_reasons_with_unsupported_terms_are_removed(no_llm, monkeypatch):
    def ranked(customer, segment, tier, candidates, job_id):
        fridge = candidates[0]
        return [Recommendation(
            recommendation_id="r1", product_id=fridge.id, score=90,
            matched_rules=["Fits your family of 3", "Works with the SmartThings app", "Within your $90,000 budget"],
            explanation="Match score 92: its NFC pairing suits you.",
        )]

    monkeypatch.setattr(brochure, "rank_products", ranked)
    ctx = run()
    rec = ctx.recommendations[0]
    assert rec.matched_rules == ["Fits your family of 3", "Within your $90,000 budget"]  # customer facts are fine
    assert rec.explanation == brochure.SAFE_EXPLANATION
    assert {t for r in ctx.review["ranker_removed"] for t in r["terms"]} == {"SmartThings", "92", "NFC"}
    assert any("ranking reason" in w["message"] for w in ctx.warnings)
