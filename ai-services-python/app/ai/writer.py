import json
import logging

from langchain_core.prompts import ChatPromptTemplate

from app import diagnostics
from app.ai.llm import get_chat_model
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
    llm = get_chat_model("writer")
    chain = PROMPT | llm.with_structured_output(WriterOutput, include_raw=True)

    result = chain.invoke({
        "segment": segment,
        "sections": json.dumps(sections),
        "candidate": json.dumps(candidate),
        "feedback": feedback or "none",
    })

    if result["parsing_error"] or result["parsed"] is None:
        diagnostics.set_status("llm.writer", "failed", str(result["parsing_error"]))
        log_stage(logger, job_id, "write", f"structured output failed: {result['parsing_error']}", level="warning")
        raise RuntimeError(f"writer structured output failed: {result['parsing_error']}")

    diagnostics.set_status("llm.writer", "real", None)
    log_stage(logger, job_id, "write", f"raw={result['raw']}")

    parsed: WriterOutput = result["parsed"]
    if len(parsed.paragraphs) == 0:
        log_stage(logger, job_id, "write", "parsed OK but paragraphs is empty", level="warning")

    return parsed.model_dump()
