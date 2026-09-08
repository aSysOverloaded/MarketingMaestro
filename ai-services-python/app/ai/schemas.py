"""Structured-output models for the LLM agents.

Field names/casing must match the Go structs exactly - Go's json.Decoder is
lenient and will not error on a mismatch, it will just silently leave a field
at its zero value (empty string, empty slice). Keep these in sync by hand:

- SectionOutline / PlannerOutput  <- backend-go/internal/steps/planner.go (SectionOutline, PlannerResult)
- WriterOutput                    <- backend-go/internal/steps/writer.go (WriterResult)
- CriticOutput                    <- backend-go/internal/steps/critic.go (CriticResult)
- EvaluatorLLMOutput              <- backend-go/internal/steps/evaluator.go (EvaluatorResult), LLM-derived fields only;
                                     the deterministic banned-word list and final merge live in evaluator.py, not here.
"""
from typing import List

from pydantic import BaseModel, Field


class SectionOutline(BaseModel):
    title: str = Field(description="Title of the brochure page/section")
    points: List[str] = Field(description="Key items and writing points to cover in this section")


class PlannerOutput(BaseModel):
    sections: List[SectionOutline]


class WriterOutput(BaseModel):
    headline: str = Field(description="A short, catchy, benefit-driven headline")
    subheadline: str = Field(description="A supporting subheadline highlighting suitability")
    paragraphs: List[str] = Field(description="Paragraphs expanding on the planner outline and product specifications")
    cta: str = Field(description="Action-oriented CTA text")


class CriticOutput(BaseModel):
    passed: bool
    feedback: str = Field(description="Details on what specs failed, or a pass confirmation")


class EvaluatorLLMOutput(BaseModel):
    passed: bool
    tone_assessment: str = Field(description="Short description of the voice, e.g. professional and helpful")
    score: int = Field(description="Overall suitability score, 0 to 100")
