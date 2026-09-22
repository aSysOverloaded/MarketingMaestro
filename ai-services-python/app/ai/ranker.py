import json
import re
from typing import List

from langchain_core.prompts import ChatPromptTemplate

from app.ai.llm import invoke_structured
from app.ai.schemas import RankingOutput
from app.catalog import CustomerInput, Product, Recommendation

MAX_RECOMMENDATIONS = 4

PROMPT = ChatPromptTemplate.from_template(
    """You are an expert sales and recommendation agent.
Given the following customer profile and candidate product catalog, rank the candidates in
descending order of how well they match the customer's needs. Select up to {max_items} suitable
candidates (fewer is fine). Only use product ids from the catalog below - never invent products.

Customer Profile:
{profile}

Candidate Catalog:
{catalog}

For each recommended product give:
- A match score (0 to 100) based on budget suitability, capacity/size needs, hobbies vs product
  features, and segment fit. A base_price of 0 means the price is unknown (not free) - do not
  claim it fits the budget; score it on the other criteria.
- Matched rules: specific, concise reasons why it matches (e.g. "Large capacity fits family size").
- Explanation: one sentence on why this product is recommended for this customer."""
)


def rank_products(customer: CustomerInput, segment: str, budget_tier: str, candidates: List[Product], job_id: str) -> List[Recommendation]:
    profile = {**customer.model_dump(), "segment": segment, "budget_tier": budget_tier}
    catalog = [c.model_dump(exclude={"hero_image", "page_number"}) for c in candidates]
    parsed = invoke_structured("ranker", PROMPT, RankingOutput, {
        "max_items": MAX_RECOMMENDATIONS,
        "profile": json.dumps(profile),
        "catalog": json.dumps(catalog),
    }, job_id)

    known_ids = {c.id for c in candidates}
    recs: List[Recommendation] = []
    for item in sorted(parsed.recommendations, key=lambda r: r.score, reverse=True):
        # Drop invented or duplicate ids rather than trusting the model's list verbatim.
        if item.product_id not in known_ids or any(r.product_id == item.product_id for r in recs):
            continue
        recs.append(Recommendation(
            recommendation_id=f"rec_{job_id}_{len(recs) + 1}",
            product_id=item.product_id,
            score=max(0, min(100, item.score)),
            matched_rules=item.matched_rules,
            explanation=item.explanation,
        ))
        if len(recs) == MAX_RECOMMENDATIONS:
            break

    if not recs:
        raise RuntimeError("ranker returned no recommendations that match catalog ids")
    return recs


def _words(text: str) -> set:
    return set(re.findall(r"[a-z]+", text.lower()))


def score_products(customer: CustomerInput, candidates: List[Product], job_id: str) -> List[Recommendation]:
    """Deterministic fallback when the LLM ranker is unavailable. Adapted from the old Go
    ProductMatcher, minus its hardcoded capacity-string checks ("26", "5.0", ...) that only
    made sense for the original demo catalog."""
    scored = []
    for p in candidates:
        score = 50
        rules: List[str] = []

        if p.base_price > 0:
            monthly_income = customer.income / 12
            monthly_payment = p.base_price / 36 * 1.05  # 3-year financing estimate
            if monthly_income * 0.15 >= monthly_payment:
                score += 25
                rules.append("Budget fits comfortably within household guidelines")
            elif monthly_income * 0.30 < monthly_payment:
                score -= 30
                rules.append("Budget warning: price exceeds standard budget guidelines")
            else:
                score += 10
                rules.append("Budget acceptable")
        else:
            rules.append("Price not listed - budget fit not assessed")

        product_words = _words(" ".join([p.model, p.category or "", *p.features]))
        matched = [h for h in customer.hobbies if _words(h) & product_words]
        if matched:
            score += 10 * len(matched)
            rules.append(f"Relevant to your interests: {', '.join(matched)}")

        if len(p.features) >= 3:
            score += 15
            rules.append("Broad feature set")

        scored.append((max(0, min(100, score)), p, rules))

    scored.sort(key=lambda s: s[0], reverse=True)
    return [
        Recommendation(
            recommendation_id=f"rec_{job_id}_{i + 1}",
            product_id=p.id,
            score=score,
            matched_rules=rules,
            # Customer-facing (printed in the brochure). The fact that this came from the
            # fallback is reported to the operator via the pipeline warnings, not here.
            explanation=f"Selected for how it fits your budget and interests across {len(rules)} criteria.",
        )
        for i, (score, p, rules) in enumerate(scored[:MAX_RECOMMENDATIONS])
    ]
