from typing import List

from langchain_core.prompts import ChatPromptTemplate

from app.ai.llm import invoke_structured
from app.ai.schemas import ExtractionOutput
from app.catalog import Product

PROMPT = ChatPromptTemplate.from_template(
    """You are an expert product catalog extraction agent.
Analyze the following text from relevant pages of a PDF product brochure and extract the
catalog items described. Each page starts with a '--- PAGE N ---' marker.

IMPORTANT - extract only what the pages actually state. Everything you output is printed in a
customer-facing brochure and used as the ground truth the copy is audited against, so an
invented value becomes a false claim to a customer:
- If a price is not explicitly stated, set base_price to 0. Never estimate a price.
- Only include features, specs and colors that are written on the page. Leave a field empty
  rather than guessing it.
- If a page describes a product without a distinct model name, you may use the page's own
  heading or the product category it names as the model name, but do not add details to it.
- If a page describes no product at all, skip it. Returning fewer items (or none) is correct
  when the pages do not describe products.

Matched Pages Content:
{pages}"""
)


def extract_products(pages_text: str, job_id: str) -> List[Product]:
    parsed = invoke_structured("extractor", PROMPT, ExtractionOutput, {"pages": pages_text}, job_id)

    products: List[Product] = []
    seen_ids = set()
    for item in parsed.products:
        # The ranker refers to products by id, so ids must be unique within a run.
        pid = item.id or "product"
        if pid in seen_ids:
            pid = f"{pid}_{len(seen_ids) + 1}"
        seen_ids.add(pid)
        products.append(Product(**{**item.model_dump(), "id": pid}))
    return products
