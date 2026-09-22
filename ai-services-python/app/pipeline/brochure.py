"""The personalized-brochure pipeline: profile -> recommend -> plan -> write & review ->
compile HTML -> render PDF.

Every fallback is recorded with ctx.warn(), and the warnings are returned to the caller. The
Go version this replaces fell back silently at almost every step (canned profile, fake
catalog, auto-passed critic, mock PDF) while still reporting success.
"""
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app import diagnostics
from app.ai.critic import audit_copy
from app.ai.evaluator import evaluate_copy
from app.ai.extractor import extract_products
from app.ai.grounding import find_ungrounded_terms
from app.ai.planner import generate_plan
from app.ai.profile import classify_profile, rule_based_profile
from app.ai.ranker import rank_products, score_products
from app.ai.writer import generate_copy
from app.catalog import DEFAULT_CATALOG, CustomerInput, Product, Recommendation, UserProfile
from app.config import settings
from app.observability import log_stage
from app.pipeline.workflow import Step, Workflow
from app.rag.search import search_catalog
from app.render.brochure import compile_html
from app.render.pdf import render_pdf

logger = logging.getLogger("pipeline.brochure")

# Retrieval tuning (carried over from the Go recommender)
PER_HOBBY_LIMIT = 4
MAX_COMBINED_MATCHES = 6

# Writer revisions allowed after a critic/evaluator rejection, each with the reviewer's feedback.
MAX_REVISIONS = 2

FALLBACK_SECTIONS = [
    {"title": "Welcome and Overview", "points": ["Acknowledge segment goals", "Intro summary of matched selection suitability"]},
    {"title": "Product Spotlight", "points": ["Showcase the listed features", "Tie each benefit to a stated spec"]},
]


def fallback_copy(segment: str) -> dict:
    # Deliberately claim-free: this copy is used exactly when it could not be fact-checked
    # (writer down) or failed fact-checking, so it must not assert anything about the product.
    return {
        "headline": "Your Personalized Selection",
        "subheadline": f"Curated for your {segment} profile.",
        "paragraphs": [],
        "cta": "Contact our sales specialists or schedule a live demonstration to learn more.",
    }


@dataclass
class JobContext:
    job_id: str
    trace_id: str
    customer: CustomerInput
    catalog_indexed: bool = False

    profile: Optional[UserProfile] = None
    candidates: List[Product] = field(default_factory=list)
    recommendations: List[Recommendation] = field(default_factory=list)
    selected_products: List[Product] = field(default_factory=list)
    sections: List[dict] = field(default_factory=list)
    copy: Optional[dict] = None
    review: Dict[str, Any] = field(default_factory=dict)
    html_path: Optional[Path] = None
    pdf_path: Optional[Path] = None

    rag_debug: Dict[str, Any] = field(default_factory=lambda: {"active": False})
    warnings: List[Dict[str, str]] = field(default_factory=list)

    def warn(self, step: str, message: str) -> None:
        self.warnings.append({"step": step, "message": message})
        log_stage(logger, self.job_id, step, f"WARNING: {message}", level="warning")


# --- Steps ---------------------------------------------------------------------------

def profile_step(ctx: JobContext) -> None:
    try:
        ctx.profile = classify_profile(ctx.customer, ctx.job_id)
    except Exception as e:
        ctx.profile = rule_based_profile(ctx.customer, ctx.job_id)
        ctx.warn("profile", f"AI profiling unavailable ({e}); used rule-based segment '{ctx.profile.segment}'.")


def _retrieve_candidates(ctx: JobContext) -> List[Product]:
    """One retrieval query per hobby (catalog text describes products, not demographics),
    merged by page, then one LLM extraction over the merged pages."""
    hobbies = [h for h in ctx.customer.hobbies if h.strip()] or ["general everyday use"]
    queries = [f"Gear and equipment for {h.strip()}." for h in hobbies]
    ctx.rag_debug = {"active": True, "query": " | ".join(queries), "match_count": 0, "matches": []}

    merged: Dict[int, dict] = {}
    for query in queries:
        for m in search_catalog(query, PER_HOBBY_LIMIT, job_id=ctx.job_id):
            page = m["page_number"]
            if page not in merged or m["score"] > merged[page]["score"]:
                merged[page] = m

    if diagnostics.get_status("embeddings")["mode"] == "mock":
        ctx.warn("recommend", "Embeddings unavailable (mock vectors) - catalog retrieval ranking is meaningless for this run.")

    matches = sorted(merged.values(), key=lambda m: m["score"], reverse=True)[:MAX_COMBINED_MATCHES]
    ctx.rag_debug["match_count"] = len(matches)
    ctx.rag_debug["matches"] = [
        {"page_number": m["page_number"], "score": m["score"], "image_count": len(m["images"]), "content_length": len(m["content"])}
        for m in matches
    ]
    if not matches:
        raise RuntimeError(f"retrieval returned 0 matches across {len(queries)} hobby queries")

    pages_text = "".join(f"--- PAGE {m['page_number']} ---\n{m['content']}\n" for m in matches)
    products = extract_products(pages_text, ctx.job_id)
    images_by_page = {m["page_number"]: m["images"] for m in matches}
    for p in products:
        images = images_by_page.get(p.page_number) or []
        if images:
            p.hero_image = images[0]  # largest image on the page (sorted at ingest)
    if not products:
        raise RuntimeError("no products described on the matched catalog pages")
    return products


def recommend_step(ctx: JobContext) -> None:
    candidates: List[Product] = []
    if ctx.catalog_indexed:
        try:
            candidates = _retrieve_candidates(ctx)
        except Exception as e:
            ctx.rag_debug["error"] = str(e)
            ctx.warn("recommend", f"Could not use the uploaded catalog ({e}).")

    if not candidates:
        candidates = list(DEFAULT_CATALOG)
        ctx.warn("recommend", "Recommendations come from the built-in demo catalog, not an uploaded one.")
    ctx.candidates = candidates

    try:
        ctx.recommendations = rank_products(ctx.customer, ctx.profile.segment, ctx.profile.budget_tier, candidates, ctx.job_id)
    except Exception as e:
        ctx.recommendations = score_products(ctx.customer, candidates, ctx.job_id)
        ctx.warn("recommend", f"AI ranking unavailable ({e}); used rule-based scoring.")

    by_id = {p.id: p for p in candidates}
    ctx.selected_products = [by_id[r.product_id] for r in ctx.recommendations]


def plan_step(ctx: JobContext) -> None:
    try:
        ctx.sections = generate_plan(ctx.profile.segment, ctx.recommendations[0].model_dump(), job_id=ctx.job_id)
    except Exception as e:
        ctx.sections = FALLBACK_SECTIONS
        ctx.warn("plan", f"AI content planner unavailable ({e}); used a default outline.")


def _review(ctx: JobContext, draft: dict, product: dict, warned: set) -> List[str]:
    """Run the grounding check, critic and evaluator on a draft; return the list of issues
    (empty = approved)."""
    issues: List[str] = []

    # Deterministic, so it still runs when the LLM critic is down.
    ungrounded = find_ungrounded_terms(draft, product)
    ctx.review["ungrounded_terms"] = ungrounded
    if ungrounded:
        issues.append(
            f"Not in the product specs, remove: {', '.join(ungrounded)}. "
            "Only mention features, apps, services and numbers that the specs list."
        )

    try:
        critic = audit_copy(draft, product, job_id=ctx.job_id)
        ctx.review["critic"] = critic
        if not critic["passed"]:
            issues.append(f"Spec accuracy: {critic['feedback']}")
    except Exception as e:
        if "critic" not in warned:
            ctx.warn("copy", f"Spec critic unavailable ({e}); only the deterministic spec-term check ran.")
            warned.add("critic")

    evaluation = evaluate_copy(draft, job_id=ctx.job_id)
    ctx.review["evaluator"] = evaluation
    if evaluation.get("degraded") and "evaluator" not in warned:
        ctx.warn("copy", "AI tone evaluation unavailable; only the banned-word check ran.")
        warned.add("evaluator")
    if evaluation["banned_words_found"]:
        issues.append(f"Remove banned words: {', '.join(evaluation['banned_words_found'])}")

    return issues


def copy_step(ctx: JobContext) -> None:
    """Write -> review -> revise with feedback. Replaces the Go behaviour of re-running the
    critic on the *same* copy until a non-deterministic model happened to pass it."""
    segment = ctx.profile.segment
    product = ctx.selected_products[0].model_dump(exclude={"hero_image", "page_number"})
    feedback = ""
    warned: set = set()

    for attempt in range(MAX_REVISIONS + 1):
        try:
            draft = generate_copy(segment, ctx.sections, product, job_id=ctx.job_id, feedback=feedback)
        except Exception as e:
            ctx.copy = fallback_copy(segment)
            ctx.warn("copy", f"AI copywriter unavailable ({e}); used generic copy.")
            return

        issues = _review(ctx, draft, product, warned)
        ctx.review["revisions"] = attempt
        if not issues:
            ctx.copy = draft
            return
        feedback = " ".join(issues)
        log_stage(logger, ctx.job_id, "copy", f"draft {attempt + 1} rejected: {feedback}")

    ctx.copy = fallback_copy(segment)
    ctx.warn("copy", f"Copy still rejected after {MAX_REVISIONS} revisions ({feedback}); used generic copy.")


def html_step(ctx: JobContext) -> None:
    ctx.html_path = compile_html(
        job_id=ctx.job_id,
        trace_id=ctx.trace_id,
        segment=ctx.profile.segment,
        copy=ctx.copy,
        recommendations=ctx.recommendations,
        products=ctx.selected_products,
        output_dir=settings.storage_dir / "temp_brochures",
    )


def pdf_path_for(job_id: str) -> Path:
    return settings.storage_dir / "generated_brochures" / f"brochure_{job_id}.pdf"


def pdf_step(ctx: JobContext) -> None:
    if settings.disable_pdf:
        ctx.warn("pdf", "PDF rendering is disabled (DISABLE_PDF=true); no brochure file was produced.")
        return
    path = pdf_path_for(ctx.job_id)
    render_pdf(ctx.html_path, path)
    ctx.pdf_path = path


def _remove_pdf(ctx: JobContext) -> None:
    if ctx.pdf_path and ctx.pdf_path.exists():
        ctx.pdf_path.unlink()


def build_workflow() -> Workflow:
    return Workflow("PersonalizedBrochure", [
        Step("profile", profile_step),
        Step("recommend", recommend_step),
        Step("plan", plan_step),
        Step("copy", copy_step),
        Step("html", html_step),
        Step("pdf", pdf_step, retries=1, compensate=_remove_pdf),
    ])
