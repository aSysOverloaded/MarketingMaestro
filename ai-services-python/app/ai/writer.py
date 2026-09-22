import json
import logging

from langchain_core.prompts import ChatPromptTemplate

from app.ai.llm import invoke_structured
from app.ai.schemas import WriterOutput
from app.observability import log_stage

logger = logging.getLogger("ai.writer")

PROMPT = ChatPromptTemplate.from_template(
    """You are a professional copywriter agent. Write persuasive copy for a personalized marketing brochure.
Customer Segment: {segment}
Brochure Outline: {sections}
Product Specifications: {candidate}

Write a headline, subheadline, body paragraphs expanding on the planner outline points and product
specifications, and an action-oriented CTA (e.g. Schedule a live demonstration or contact our sales specialists).

IMPORTANT: Only reference features, materials, technologies, and specifications explicitly listed in
Product Specifications above. Do not invent, imply, or add any capability, feature, or claim that is
not present there, even if it sounds plausible or is common for this type of product. If you want to
emphasize a quality (e.g. comfort, durability, convenience), tie it explicitly back to one of the
listed specs rather than introducing a new unlisted feature to support it.

Reviewer feedback on the previous draft (address every point; "none" means this is the first draft):
{feedback}"""
)


def generate_copy(segment: str, sections: list, candidate: dict, job_id: str = "unknown", feedback: str = "") -> dict:
    parsed = invoke_structured("writer", PROMPT, WriterOutput, {
        "segment": segment,
        "sections": json.dumps(sections),
        "candidate": json.dumps(candidate),
        "feedback": feedback or "none",
    }, job_id)

    if len(parsed.paragraphs) == 0:
        log_stage(logger, job_id, "write", "parsed OK but paragraphs is empty", level="warning")

    return parsed.model_dump()
