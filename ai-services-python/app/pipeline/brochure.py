"""The personalized-brochure pipeline: profile -> recommend -> plan -> write & review ->
compile HTML -> render PDF.

Every fallback is recorded with ctx.warn(), and the warnings are returned to the caller. The
Go version this replaces fell back silently at almost every step (canned profile, fake
catalog, auto-passed critic, mock PDF) while still reporting success.
"""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app import diagnostics
from app.ai.critic import audit_copy
from app.ai.evaluator import banned_words_in, evaluate_copy
from app.ai.extractor import extract_products
from app.ai.grounding import find_ungrounded_in_text, find_ungrounded_terms
from app.ai.llm import used_fallback
from app.ai.planner import generate_plan
from app.ai.product_copy import write_product_blurbs
from app.ai.profile import classify_profile, rule_based_profile
from app.ai.ranker import MAX_RECOMMENDATIONS, rank_products, score_products
from app.ai.writer import generate_copy
from app.catalog import DEFAULT_CATALOG, CustomerInput, Product, Recommendation, UserProfile
from app.config import settings
from app.observability import log_stage
from app.pipeline.workflow import Step, Workflow
from app.rag import extraction_cache
from app.rag.search import get_catalog, search_catalog
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
    # product id -> one fact-checked sentence for that product's brochure page
    product_blurbs: Dict[str, str] = field(default_factory=dict)
    review: Dict[str, Any] = field(default_factory=dict)
    html_path: Optional[Path] = None
    pdf_path: Optional[Path] = None

    # product id -> the customer interests whose search found it
    product_hobbies: Dict[str, set] = field(default_factory=dict)
    rag_debug: Dict[str, Any] = field(default_factory=lambda: {"active": False})
    warnings: List[Dict[str, str]] = field(default_factory=list)
    # Receives short human-readable progress notes ("Writing draft 2") for the UI.
    on_note: Optional[Callable[[str], None]] = None
    # Notices already emitted this run, so a repeated condition warns once.
    seen_notices: set = field(default_factory=set)

    def warn(self, step: str, message: str) -> None:
        self.warnings.append({"step": step, "message": message})
        log_stage(logger, self.job_id, step, f"WARNING: {message}", level="warning")

    def note(self, message: str) -> None:
        if self.on_note:
            self.on_note(message)


# --- Steps ---------------------------------------------------------------------------

def _note_fallback(ctx: JobContext, purpose: str, step: str) -> None:
    """Say so when the primary provider failed and the backup answered instead - otherwise a
    run silently depends on the backup and nobody notices the primary is down or capped."""
    if used_fallback(purpose) and f"fallback:{purpose}" not in ctx.seen_notices:
        ctx.seen_notices.add(f"fallback:{purpose}")
        ctx.warn(step, f"The {purpose} step was answered by the fallback provider; the primary one failed.")


def profile_step(ctx: JobContext) -> None:
    if not settings.use_llm_profile:
        # Deliberate: the rules derive the segment from the same inputs, instantly. Not a
        # fallback, so it is not reported as one.
        ctx.profile = rule_based_profile(ctx.customer, ctx.job_id)
        return
    try:
        ctx.profile = classify_profile(ctx.customer, ctx.job_id)
        _note_fallback(ctx, "profile", "profile")
    except Exception as e:
        ctx.profile = rule_based_profile(ctx.customer, ctx.job_id)
        ctx.warn("profile", f"AI profiling unavailable ({e}); used rule-based segment '{ctx.profile.segment}'.")


def _match_product_to_chunk(product: Product, chunks: List[dict]) -> Optional[dict]:
    """Which retrieved chunk a product came from: the one naming it, else one from its page.

    Used to give the product the image cropped from its own block, rather than any image on
    the page - a catalogue page often holds four products.
    """
    name = re.sub(r"[^a-z0-9]", "", (product.model or "").lower())
    if name:
        for chunk in chunks:
            if name in re.sub(r"[^a-z0-9]", "", chunk["content"].lower()):
                return chunk
    return next((c for c in chunks if c["page_number"] == product.page_number), None)


def _extract_with_cache(ctx: JobContext, matches: List[dict]) -> List[Product]:
    """Extract products from the matched chunks, reusing anything extracted before.

    The same popular chunks match run after run, and extraction is the largest prompt in the
    pipeline, so results are cached per chunk against the catalog's content hash.
    """
    catalog = get_catalog() or {}
    catalog_sha = catalog.get("sha256", "")
    cached = extraction_cache.get_many(catalog_sha, [m["chunk_id"] for m in matches]) if catalog_sha else {}

    products: List[Product] = []
    for chunk_id, raw_products in cached.items():
        products.extend(Product(**raw) for raw in raw_products)

    fresh_chunks = [m for m in matches if m["chunk_id"] not in cached]
    ctx.review["extraction_cache"] = {"hits": len(cached), "misses": len(fresh_chunks)}
    if cached:
        log_stage(logger, ctx.job_id, "recommend", f"extraction cache: {len(cached)} hit(s), {len(fresh_chunks)} miss(es)")
    if not fresh_chunks:
        return products

    ctx.note(f"Extracting products from {len(fresh_chunks)} new catalog section(s)")
    chunks_text = "".join(f"--- PAGE {c['page_number']} SECTION {c['block_index']} ---\n{c['content']}\n" for c in fresh_chunks)
    extracted = extract_products(chunks_text, ctx.job_id)
    _note_fallback(ctx, "extractor", "recommend")

    # Attribute each product to the chunk it came from, for its image and for the cache.
    by_chunk: Dict[str, list] = {c["chunk_id"]: [] for c in fresh_chunks}
    for product in extracted:
        chunk = _match_product_to_chunk(product, fresh_chunks)
        if chunk:
            by_chunk[chunk["chunk_id"]].append(product.model_dump())
    if catalog_sha:
        extraction_cache.put_many(catalog_sha, by_chunk)

    products.extend(extracted)
    return products


def _retrieve_candidates(ctx: JobContext) -> List[Product]:
    """One retrieval query per hobby (catalog text describes products, not demographics),
    merged by chunk, then one LLM extraction over the chunks not already cached."""
    hobbies = [h for h in ctx.customer.hobbies if h.strip()] or ["general everyday use"]
    queries = [f"Gear and equipment for {h.strip()}." for h in hobbies]
    ctx.rag_debug = {"active": True, "query": " | ".join(queries), "match_count": 0, "matches": []}

    ctx.note(f"Searching the catalog ({len(queries)} {'query' if len(queries) == 1 else 'queries'})")
    merged: Dict[str, dict] = {}
    for hobby, query in zip(hobbies, queries):
        for m in search_catalog(query, PER_HOBBY_LIMIT, job_id=ctx.job_id):
            chunk_id = m["chunk_id"]
            if chunk_id not in merged or m["score"] > merged[chunk_id]["score"]:
                merged[chunk_id] = {**m, "hobbies": set()}
            # Remember which interest found this block, so the brochure can cover each of them.
            merged[chunk_id]["hobbies"].add(hobby)

    if diagnostics.get_status("embeddings")["mode"] == "mock":
        ctx.warn("recommend", "Embeddings unavailable (mock vectors) - catalog retrieval ranking is meaningless for this run.")

    matches = sorted(merged.values(), key=lambda m: m["score"], reverse=True)[:MAX_COMBINED_MATCHES]
    ctx.rag_debug["match_count"] = len(matches)
    ctx.rag_debug["matches"] = [
        {"page_number": m["page_number"], "block_index": m.get("block_index", 0), "score": m["score"],
         "image_count": len(m["images"]), "content_length": len(m["content"])}
        for m in matches
    ]
    if not matches:
        raise RuntimeError(f"retrieval returned 0 matches across {len(queries)} hobby queries")

    products = _extract_with_cache(ctx, matches)
    for product in products:
        chunk = _match_product_to_chunk(product, matches)
        if chunk:
            ctx.product_hobbies[product.id] = set(chunk.get("hobbies") or ())
            if chunk["images"]:
                product.hero_image = chunk["images"][0]  # cropped from this product's own block
    if not products:
        raise RuntimeError("no products described on the matched catalog sections")
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

    ctx.note(f"Ranking {len(candidates)} products")
    try:
        ctx.recommendations = rank_products(ctx.customer, ctx.profile.segment, ctx.profile.budget_tier, candidates, ctx.job_id)
        _note_fallback(ctx, "ranker", "recommend")
    except Exception as e:
        ctx.recommendations = score_products(ctx.customer, candidates, ctx.job_id)
        ctx.warn("recommend", f"AI ranking unavailable ({e}); used rule-based scoring.")

    by_id = {p.id: p for p in candidates}
    _cover_every_hobby(ctx, candidates)
    ctx.selected_products = [by_id[r.product_id] for r in ctx.recommendations]
    _ground_recommendations(ctx)


def _cover_every_hobby(ctx: JobContext, candidates: List[Product]) -> None:
    """Make sure each stated interest is represented, when the catalog has something for it.

    Retrieval searches per hobby, but ranking then picks the best overall - so a customer who
    said "basketball, running" could get four basketballs and nothing for running.
    """
    hobbies = {h for hs in ctx.product_hobbies.values() for h in hs}
    if len(hobbies) < 2:
        return

    def hobbies_of(product_id: str) -> set:
        return ctx.product_hobbies.get(product_id, set())

    covered = {h for r in ctx.recommendations for h in hobbies_of(r.product_id)}
    by_id = {p.id: p for p in candidates}
    for hobby in sorted(hobbies - covered):
        pick = next((p for p in candidates if hobby in hobbies_of(p.id) and p.id not in {r.product_id for r in ctx.recommendations}), None)
        if pick is None:
            continue
        # Deterministic reasons, so a swapped-in product is described as accurately as a ranked one.
        scored = score_products(ctx.customer, [pick], ctx.job_id)[0]
        scored.recommendation_id = f"rec_{ctx.job_id}_{hobby}"
        if len(ctx.recommendations) < MAX_RECOMMENDATIONS:
            ctx.recommendations.append(scored)
        else:
            # Drop the lowest-ranked product whose interests are already covered twice.
            droppable = [r for r in ctx.recommendations
                         if any(sum(1 for x in ctx.recommendations if h in hobbies_of(x.product_id)) > 1
                                for h in hobbies_of(r.product_id))]
            if not droppable:
                continue
            ctx.recommendations[ctx.recommendations.index(droppable[-1])] = scored
        ctx.review.setdefault("hobby_coverage_added", []).append({"hobby": hobby, "product_id": pick.id})
        log_stage(logger, ctx.job_id, "recommend", f"added a product for '{hobby}', which ranking had left out")


# Replaces a ranker explanation that fails the grounding check. Customer-facing.
SAFE_EXPLANATION = "Selected for how it fits your needs and budget."


def _ground_recommendations(ctx: JobContext) -> None:
    """The per-product explanation and matched rules are printed in the brochure, so check
    them like the cover copy. There is no revise loop for the ranker: an explanation with
    unsupported terms is replaced with a safe one, and such matched rules are dropped."""
    removed = []
    for rec, product in zip(ctx.recommendations, ctx.selected_products):
        # Explanations may legitimately cite the customer ("fits your family of 3"), so the
        # customer profile counts as a source of truth alongside the product specs.
        source = {
            "product": product.model_dump(exclude={"hero_image", "page_number"}),
            "customer": ctx.customer.model_dump(),
            "segment": ctx.profile.segment,
            "budget_tier": ctx.profile.budget_tier,
        }
        kept = []
        for rule in rec.matched_rules:
            terms = find_ungrounded_in_text(rule, source)
            if terms:
                removed.append({"product_id": rec.product_id, "text": rule, "terms": terms})
            else:
                kept.append(rule)
        rec.matched_rules = kept

        terms = find_ungrounded_in_text(rec.explanation, source)
        if terms:
            removed.append({"product_id": rec.product_id, "text": rec.explanation, "terms": terms})
            rec.explanation = SAFE_EXPLANATION

    ctx.review["ranker_removed"] = removed
    if removed:
        terms = sorted({t for r in removed for t in r["terms"]})
        ctx.warn("recommend", f"Removed {len(removed)} ranking reason(s) citing things not in the specs or profile: {', '.join(terms)}.")


def plan_step(ctx: JobContext) -> None:
    if not settings.use_llm_planner:
        # Deliberate: the writer is asked to structure the copy itself, saving a round trip.
        ctx.sections = []
        return
    try:
        ctx.sections = generate_plan(ctx.profile.segment, ctx.recommendations[0].model_dump(), job_id=ctx.job_id)
        _note_fallback(ctx, "planner", "plan")
    except Exception as e:
        ctx.sections = FALLBACK_SECTIONS
        ctx.warn("plan", f"AI content planner unavailable ({e}); used a default outline.")


@contextmanager
def _timed(ctx: JobContext, call: str, draft: int):
    """Record how long one call inside the copy step took (ctx.review["calls"]), so the
    benchmark can see where the copy step's time goes."""
    start = time.monotonic()
    try:
        yield
    finally:
        ctx.review.setdefault("calls", []).append({"call": call, "draft": draft, "ms": int((time.monotonic() - start) * 1000)})


def _review(ctx: JobContext, draft: dict, product: dict, warned: set, attempt: int = 1) -> List[str]:
    """Review a draft; return the list of issues (empty = approved).

    Order matters for speed. The deterministic checks run first and are instant. If they
    already reject the draft, the LLM critic and evaluator are skipped: the draft goes back to
    the writer either way, and their verdict on a draft about to be rewritten is wasted time.
    Otherwise the critic and evaluator run in parallel - they are independent - so the review
    costs max(critic, evaluator) instead of the sum.
    """
    issues: List[str] = []

    # Deterministic, so it still runs when the LLM critic is down.
    ungrounded = find_ungrounded_terms(draft, product)
    ctx.review["ungrounded_terms"] = ungrounded
    if ungrounded:
        issues.append(
            f"Not in the product specs, remove: {', '.join(ungrounded)}. "
            "Only mention features, apps, services and numbers that the specs list."
        )
    banned = banned_words_in(draft)
    if banned:
        issues.append(f"Remove banned words: {', '.join(banned)}")

    if issues:
        ctx.review.setdefault("skipped_llm_reviews", 0)
        ctx.review["skipped_llm_reviews"] += 1
        return issues

    def run_critic():
        with _timed(ctx, "critic", attempt):
            return audit_copy(draft, product, job_id=ctx.job_id)

    def run_evaluator():
        with _timed(ctx, "evaluator", attempt):
            return evaluate_copy(draft, job_id=ctx.job_id)  # never raises

    ctx.review.setdefault("calls", [])
    with ThreadPoolExecutor(max_workers=2) as pool:
        critic_future = pool.submit(run_critic)
        # The tone score is reported, never acted on, so it is off by default (USE_LLM_TONE_EVALUATOR).
        evaluation = pool.submit(run_evaluator).result() if settings.use_llm_tone_evaluator else None
        try:
            critic = critic_future.result()
            _note_fallback(ctx, "critic", "copy")
        except Exception as e:
            critic = None
            if "critic" not in warned:
                ctx.warn("copy", f"Spec critic unavailable ({e}); only the deterministic spec-term check ran.")
                warned.add("critic")

    if critic is not None:
        ctx.review["critic"] = critic
        if not critic["passed"]:
            issues.append(f"Spec accuracy: {critic['feedback']}")

    if evaluation is not None:
        ctx.review["evaluator"] = evaluation
        if evaluation.get("degraded") and "evaluator" not in warned:
            ctx.warn("copy", "AI tone evaluation unavailable; only the banned-word check ran.")
            warned.add("evaluator")
        # Banned words were already checked above; this only adds the tone assessment.

    return issues


def copy_step(ctx: JobContext) -> None:
    """Write -> review -> revise with feedback. Replaces the Go behaviour of re-running the
    critic on the *same* copy until a non-deterministic model happened to pass it."""
    segment = ctx.profile.segment
    product = ctx.selected_products[0].model_dump(exclude={"hero_image", "page_number"})
    feedback = ""
    warned: set = set()

    for attempt in range(MAX_REVISIONS + 1):
        ctx.note(f"Writing draft {attempt + 1}" + (" (revising with reviewer feedback)" if attempt else ""))
        try:
            with _timed(ctx, "writer", attempt + 1):
                draft = generate_copy(segment, ctx.sections, product, job_id=ctx.job_id, feedback=feedback)
            _note_fallback(ctx, "writer", "copy")
        except Exception as e:
            ctx.copy = fallback_copy(segment)
            ctx.warn("copy", f"AI copywriter unavailable ({e}); used generic copy.")
            return

        ctx.note(f"Fact-checking draft {attempt + 1}")
        issues = _review(ctx, draft, product, warned, attempt + 1)
        ctx.review["revisions"] = attempt
        if not issues:
            ctx.copy = draft
            return
        feedback = " ".join(issues)
        log_stage(logger, ctx.job_id, "copy", f"draft {attempt + 1} rejected: {feedback}")

    ctx.copy = fallback_copy(segment)
    ctx.warn("copy", f"Copy still rejected after {MAX_REVISIONS} revisions ({feedback}); used generic copy.")


def product_copy_step(ctx: JobContext) -> None:
    """One sentence per recommended product, so options 2-4 are not left with bare spec lists."""
    ctx.note(f"Writing copy for {len(ctx.selected_products)} product(s)")
    try:
        ctx.product_blurbs = write_product_blurbs(ctx.profile.segment, ctx.selected_products, ctx.job_id)
        _note_fallback(ctx, "product_copy", "product_copy")
    except Exception as e:
        ctx.warn("product_copy", f"Per-product copy unavailable ({e}); those pages show the specs only.")
        return
    missing = [p.id for p in ctx.selected_products if p.id not in ctx.product_blurbs]
    if missing:
        ctx.warn("product_copy", f"{len(missing)} product page(s) show specs only: the copy written for them was not supported by their specs.")


def html_step(ctx: JobContext) -> None:
    ctx.html_path = compile_html(
        job_id=ctx.job_id,
        trace_id=ctx.trace_id,
        segment=ctx.profile.segment,
        copy=ctx.copy,
        recommendations=ctx.recommendations,
        products=ctx.selected_products,
        blurbs=ctx.product_blurbs,
        output_dir=settings.storage_dir / "temp_brochures",
        catalog_brand=(get_catalog() or {}).get("brand"),
    )


# Generated brochures are ~1.5 MB each and nothing ever deleted them (29 MB after a day of
# testing). The last few are worth keeping so a user can still open a previous PDF.
KEEP_RECENT_OUTPUTS = 20


def prune_old_outputs(keep: int = KEEP_RECENT_OUTPUTS) -> None:
    for folder, pattern in ((settings.storage_dir / "generated_brochures", "*.pdf"),
                            (settings.storage_dir / "temp_brochures", "*.html")):
        if not folder.is_dir():
            continue
        files = sorted(folder.glob(pattern), key=lambda f: f.stat().st_mtime, reverse=True)
        for stale in files[keep:]:
            stale.unlink(missing_ok=True)


def pdf_path_for(job_id: str) -> Path:
    return settings.storage_dir / "generated_brochures" / f"brochure_{job_id}.pdf"


def pdf_step(ctx: JobContext) -> None:
    if settings.disable_pdf:
        ctx.warn("pdf", "PDF rendering is disabled (DISABLE_PDF=true); no brochure file was produced.")
        return
    path = pdf_path_for(ctx.job_id)
    render_pdf(ctx.html_path, path)
    ctx.pdf_path = path
    prune_old_outputs()


def _remove_pdf(ctx: JobContext) -> None:
    if ctx.pdf_path and ctx.pdf_path.exists():
        ctx.pdf_path.unlink()


def build_workflow() -> Workflow:
    return Workflow("PersonalizedBrochure", [
        Step("profile", profile_step),
        Step("recommend", recommend_step),
        Step("plan", plan_step),
        Step("copy", copy_step),
        Step("product_copy", product_copy_step),
        Step("html", html_step),
        Step("pdf", pdf_step, retries=1, compensate=_remove_pdf),
    ])
