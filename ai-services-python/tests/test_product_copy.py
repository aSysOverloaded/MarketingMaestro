"""One fact-checked sentence per recommended product (options 2-4 used to get none)."""
import app.ai.product_copy as product_copy
import app.pipeline.brochure as brochure
from app.ai.product_copy import ProductBlurb, ProductBlurbs
from app.catalog import CustomerInput, Product
from app.render.brochure import compile_html

BALL = Product(id="ball", model="Rhenium Ball", base_price=59, currency="EUR",
               features=["Surface: high density PU", "Panels: 8 panels"])
SHIRT = Product(id="shirt", model="Golem Shirt", base_price=33, currency="EUR", features=["Micromesh back"])


def _stub(monkeypatch, blurbs):
    monkeypatch.setattr(product_copy, "invoke_structured",
                        lambda *a, **k: ProductBlurbs(blurbs=[ProductBlurb(product_id=pid, blurb=text) for pid, text in blurbs]))


def test_each_product_gets_its_own_sentence(monkeypatch):
    _stub(monkeypatch, [("ball", "An 8-panel ball with a high density PU surface."),
                        ("shirt", "A shirt with a micromesh back.")])
    assert product_copy.write_product_blurbs("Adventure", [BALL, SHIRT], "j") == {
        "ball": "An 8-panel ball with a high density PU surface.",
        "shirt": "A shirt with a micromesh back.",
    }


def test_a_blurb_claiming_what_the_specs_do_not_say_is_dropped(monkeypatch):
    _stub(monkeypatch, [("ball", "Pairs with the SmartThings app over NFC."),
                        ("shirt", "A shirt with a micromesh back.")])
    accepted = product_copy.write_product_blurbs("Adventure", [BALL, SHIRT], "j")
    assert "ball" not in accepted and "shirt" in accepted


def test_blurbs_for_unknown_products_are_ignored(monkeypatch):
    _stub(monkeypatch, [("not_recommended", "Some other product.")])
    assert product_copy.write_product_blurbs("Adventure", [BALL], "j") == {}


def test_the_pipeline_reports_pages_left_without_copy(no_llm, monkeypatch):
    ctx = brochure.JobContext(job_id="job_" + "0" * 32, trace_id="t",
                              customer=CustomerInput(age=30, income=60000, family_size=1, hobbies=["basketball"], location="Munich"))
    ctx.profile = brochure.rule_based_profile(ctx.customer, ctx.job_id)
    ctx.selected_products = [BALL, SHIRT]
    monkeypatch.setattr(brochure, "write_product_blurbs", lambda segment, products, job_id: {"ball": "Grounded line."})

    brochure.product_copy_step(ctx)
    assert ctx.product_blurbs == {"ball": "Grounded line."}
    assert any("specs only" in w["message"] for w in ctx.warnings)


def test_the_blurb_is_rendered_on_the_product_page(tmp_path):
    from app.catalog import Recommendation

    rec = Recommendation(recommendation_id="r", product_id="ball", score=90, matched_rules=["Fits"], explanation="Good fit")
    html = compile_html(job_id="j", trace_id="t", segment="Adventure",
                        copy={"headline": "H", "subheadline": "S", "paragraphs": [], "cta": "C"},
                        recommendations=[rec], products=[BALL], output_dir=tmp_path,
                        blurbs={"ball": "An 8-panel ball with a high density PU surface."}).read_text(encoding="utf-8")
    assert "An 8-panel ball with a high density PU surface." in html

    without = compile_html(job_id="j2", trace_id="t", segment="Adventure",
                           copy={"headline": "H", "subheadline": "S", "paragraphs": [], "cta": "C"},
                           recommendations=[rec], products=[BALL], output_dir=tmp_path).read_text(encoding="utf-8")
    assert "Why this option fits your profile" in without  # page still works with no blurb
