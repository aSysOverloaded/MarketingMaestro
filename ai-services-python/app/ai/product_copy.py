"""A short, fact-checked line of copy for every recommended product.

The writer only ever saw the top product, so options 2-4 got a brochure page with no copy
written about them - just the ranker's reason and a spec list. This fills that gap in one call
for all of them (a call per product would triple the slowest step), and each blurb is checked
against its own product's specs by the same deterministic grounding check the cover copy uses.
"""
import json
import logging
from typing import Dict, List

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.ai.grounding import find_ungrounded_in_text
from app.ai.llm import invoke_structured
from app.catalog import Product
from app.observability import log_stage

logger = logging.getLogger("ai.product_copy")

PROMPT = ChatPromptTemplate.from_template(
    """You are a product copywriter. For each product below, write ONE sentence (max 25 words)
for a customer in the "{segment}" segment, saying what it offers them.

Products:
{products}

Use only what each product's specifications state. Do not add features, materials,
technologies, apps or numbers that are not listed for that product - a claim that is not in
its specifications is a false claim to the customer. Return one entry per product, using the
product's exact id."""
)


class ProductBlurb(BaseModel):
    product_id: str = Field(description="The product's id, exactly as given")
    blurb: str = Field(description="One sentence, max 25 words, grounded in that product's specs")


class ProductBlurbs(BaseModel):
    blurbs: List[ProductBlurb]


def write_product_blurbs(segment: str, products: List[Product], job_id: str = "unknown") -> Dict[str, str]:
    """{product_id: blurb} for the products whose blurb passed the grounding check.

    A blurb that mentions anything absent from that product's specs is dropped rather than
    revised: it is one sentence beside a spec list, so losing it costs little, while another
    round trip would cost the customer another 10 seconds of waiting.
    """
    specs = [p.model_dump(exclude={"hero_image", "page_number"}) for p in products]
    parsed = invoke_structured("product_copy", PROMPT, ProductBlurbs, {
        "segment": segment,
        "products": json.dumps(specs),
    }, job_id)

    by_id = {p.id: p for p in products}
    accepted: Dict[str, str] = {}
    for item in parsed.blurbs:
        product = by_id.get(item.product_id)
        if product is None:
            continue
        ungrounded = find_ungrounded_in_text(item.blurb, product.model_dump(exclude={"hero_image", "page_number"}))
        if ungrounded:
            log_stage(logger, job_id, "product_copy",
                      f"dropped blurb for {item.product_id}: mentions {ungrounded} which its specs do not", level="warning")
            continue
        accepted[item.product_id] = item.blurb.strip()
    return accepted
