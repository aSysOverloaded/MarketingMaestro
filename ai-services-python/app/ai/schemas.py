"""Structured-output models for the LLM chains.

Each chain binds one of these via `with_structured_output`, so the model's reply is parsed
and validated here - a malformed reply surfaces as a parsing error and the calling step
falls back, instead of a silently zero-valued field.
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class SectionOutline(BaseModel):
    title: str = Field(description="Title of the brochure page/section")
    points: List[str] = Field(description="Key items and writing points to cover in this section")


class PlannerOutput(BaseModel):
    sections: List[SectionOutline]


class WriterOutput(BaseModel):
    headline: str = Field(description="A short, catchy, benefit-driven headline")
    subheadline: str = Field(description="A supporting subheadline highlighting suitability")
    # Rendered on a fixed-height A4 cover page with overflow hidden - long copy gets clipped
    # silently, so keep this short. The renderer also caps the count at MAX_COVER_PARAGRAPHS.
    paragraphs: List[str] = Field(description="2 to 3 short paragraphs (each under 60 words) expanding on the planner outline and product specifications")
    cta: str = Field(description="Action-oriented CTA text")


class CriticOutput(BaseModel):
    passed: bool
    feedback: str = Field(description="Details on what specs failed, or a pass confirmation")


class EvaluatorLLMOutput(BaseModel):
    passed: bool
    tone_assessment: str = Field(description="Short description of the voice, e.g. professional and helpful")
    score: int = Field(description="Overall suitability score, 0 to 100")


class ProfileOutput(BaseModel):
    segment: Literal["Adventure", "Executive", "Family", "Standard"]
    budget_tier: Literal["Economy", "Mid-Range", "Premium", "Ultra Luxury"]


class ExtractedProduct(BaseModel):
    id: str = Field(description="Short unique snake_case id, e.g. appliance_fridge_samsung")
    model: str = Field(description="Product model name exactly as stated, or the page heading if no model name is stated")
    base_price: float = Field(default=0, description="Price exactly as stated on the page; 0 if no price is stated. Never estimate.")
    currency: Optional[str] = Field(default=None, description="Currency of base_price exactly as printed on the page, e.g. EUR, USD, GBP")
    category: Optional[str] = Field(default=None, description="Product category if stated or clearly named, e.g. Refrigerator, Tent")
    capacity: Optional[str] = Field(default=None, description="Capacity/size only if stated")
    power: Optional[str] = Field(default=None, description="Power rating only if stated")
    features: List[str] = Field(default_factory=list, description="Features written on the page; do not add any")
    colors: List[str] = Field(default_factory=list, description="Colors written on the page; empty if none")
    page_number: int = Field(description="The N from the '--- PAGE N ---' marker this product came from")


class ExtractionOutput(BaseModel):
    products: List[ExtractedProduct]


class RankedItem(BaseModel):
    product_id: str = Field(description="The id of a product from the candidate catalog")
    score: int = Field(description="Match score, 0 to 100")
    matched_rules: List[str] = Field(description="Short, specific reasons it matches, e.g. 'Fits budget'")
    explanation: str = Field(description="One sentence on why this product suits the customer")


class RankingOutput(BaseModel):
    recommendations: List[RankedItem] = Field(description="Up to 4 items, ordered by score descending")
