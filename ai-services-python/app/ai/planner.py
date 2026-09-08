import json
import logging

from langchain_core.prompts import ChatPromptTemplate

from app import diagnostics
from app.ai.llm import get_chat_model
from app.ai.schemas import PlannerOutput
from app.observability import log_stage

logger = logging.getLogger("ai.planner")

PROMPT = ChatPromptTemplate.from_template(
    """You are a content planning agent. Your task is to plan the sections of a personalized marketing brochure.
Customer Segment: {segment}
Recommended Product: {recommendation}

Plan the sections of the brochure. Each section needs a title and a list of key writing points to cover."""
)


def generate_plan(segment: str, recommendation: dict, job_id: str = "unknown") -> list:
    llm = get_chat_model("planner")
    chain = PROMPT | llm.with_structured_output(PlannerOutput, include_raw=True)

    result = chain.invoke({
        "segment": segment,
        "recommendation": json.dumps(recommendation),
    })

    if result["parsing_error"] or result["parsed"] is None:
        diagnostics.set_status("llm.planner", "failed", str(result["parsing_error"]))
        log_stage(logger, job_id, "plan", f"structured output failed: {result['parsing_error']}", level="warning")
        raise RuntimeError(f"planner structured output failed: {result['parsing_error']}")

    diagnostics.set_status("llm.planner", "real", None)
    log_stage(logger, job_id, "plan", f"raw={result['raw']}")

    parsed: PlannerOutput = result["parsed"]
    if len(parsed.sections) == 0:
        log_stage(logger, job_id, "plan", "parsed OK but sections is empty", level="warning")

    return [section.model_dump() for section in parsed.sections]
