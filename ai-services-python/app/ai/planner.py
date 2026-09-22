import json
import logging

from langchain_core.prompts import ChatPromptTemplate

from app.ai.llm import invoke_structured
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
    parsed = invoke_structured("planner", PROMPT, PlannerOutput, {
        "segment": segment,
        "recommendation": json.dumps(recommendation),
    }, job_id)

    if len(parsed.sections) == 0:
        log_stage(logger, job_id, "plan", "parsed OK but sections is empty", level="warning")

    return [section.model_dump() for section in parsed.sections]
