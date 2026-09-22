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
Fail the audit if the copy:
- states a number, feature, or metric that contradicts the specifications, OR
- mentions any feature, app, service, integration, certification, warranty, or capability that is NOT
  listed in the specifications - even if it is plausible or commonly true for this kind of product.
  (Example: promising a companion phone app when the specs list no app is a failure.)
Subjective benefit language tied to a listed spec ("keeps food fresh" for a listed cooling system) is fine.

Drafted Marketing Copy:
{copy}

Official Product Specifications:
{candidate}

Determine if the copy has passed or failed the audit. If failed, list each unsupported or incorrect claim so the writer can remove or fix it."""
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
