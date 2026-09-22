"""Identify the brand a catalog belongs to, once per catalog at ingest.

Brand styling used to come from the product model name, which only recognised the two
appliance brands in the demo catalog - so a real Macron sports catalogue produced brochures
branded "Premium Home" in generic blue. The brand is a property of the catalog, not of each
product name, so it is detected once at ingest and stored with the catalog.
"""
import logging
import re
from typing import Optional

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.ai.llm import invoke_structured

logger = logging.getLogger("ai.catalog_brand")

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

PROMPT = ChatPromptTemplate.from_template(
    """These are excerpts from a product catalog. Identify the company whose catalog this is.

{sample}

Give the brand name exactly as the catalog writes it. If the excerpts do not name a company,
return an empty name rather than guessing. The colour is for styling a brochure: give the
brand's main colour as a hex code only if you are confident of it, otherwise leave it empty."""
)


class CatalogBrandOutput(BaseModel):
    name: str = Field(description="Brand/company name as written in the catalog, or empty if not stated")
    primary_color: str = Field(default="", description="Brand's main colour as #rrggbb, or empty if unsure")


def detect_catalog_brand(sample_text: str, job_id: str = "unknown") -> Optional[dict]:
    """{"name", "primary_color"} for the catalog, or None when it cannot be determined.

    Never raises: a failure here must not cost the catalog its ingest, it only means the
    brochure falls back to model-name-based branding.
    """
    try:
        parsed = invoke_structured("catalog_brand", PROMPT, CatalogBrandOutput, {"sample": sample_text[:6000]}, job_id)
    except Exception as e:
        logger.warning(f"catalog brand detection failed: {e}")
        return None

    name = parsed.name.strip()
    if not name or len(name) > 40:
        return None
    color = parsed.primary_color.strip()
    return {"name": name, "primary_color": color if HEX_COLOR.match(color) else ""}
