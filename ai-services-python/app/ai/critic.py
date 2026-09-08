import json
import logging

from langchain_core.prompts import ChatPromptTemplate

from app import diagnostics
from app.ai.llm import get_chat_model
from app.ai.schemas import CriticOutput
from app.observability import log_stage

logger = logging.getLogger("ai.critic")

PROMPT = ChatPromptTemplate.from_template(
    """You are an audit agent (Spec Critic).
Your job is to compare the drafted marketing copy against the official product specifications and verify that all claims are accurate.
If the copy references numbers, features, or metrics that DO NOT exist or contradict the specifications sheet, fail the validation.

Drafted Marketing Copy:
{copy}

Official Product Specifications:
{candidate}

Determine if the copy has passed or failed the audit. If failed, provide correction feedback outlining which specs were incorrect."""
)


def audit_copy(copy: dict, candidate: dict, job_id: str = "unknown") -> dict:
    llm = get_chat_model("critic")
    chain = PROMPT | llm.with_structured_output(CriticOutput, include_raw=True)

    result = chain.invoke({
        "copy": json.dumps(copy),
        "candidate": json.dumps(candidate),
    })

    if result["parsing_error"] or result["parsed"] is None:
        diagnostics.set_status("llm.critic", "failed", str(result["parsing_error"]))
        log_stage(logger, job_id, "critic", f"structured output failed: {result['parsing_error']}", level="warning")
        raise RuntimeError(f"critic structured output failed: {result['parsing_error']}")

    diagnostics.set_status("llm.critic", "real", None)
    log_stage(logger, job_id, "critic", f"raw={result['raw']}")

    return result["parsed"].model_dump()
