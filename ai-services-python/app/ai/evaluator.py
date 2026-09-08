import json
import logging

from langchain_core.prompts import ChatPromptTemplate

from app import diagnostics
from app.ai.llm import get_chat_model
from app.ai.schemas import EvaluatorLLMOutput
from app.observability import log_stage

logger = logging.getLogger("ai.evaluator")

BANNED_WORDS = ["cheap", "unreliable", "garbage", "competitor", "ford", "toyota"]

PROMPT = ChatPromptTemplate.from_template(
    """You are a brand quality evaluation agent (Evaluator).
Analyze the following marketing copy draft and grade it on readability, style consistency, and alignment with a professional, helpful tone.

Copy Draft:
{copy}

Rate the tone, grade the overall suitability score (0-100), and determine if it meets brand voice standards (passing score >= 75)."""
)


def _deterministic_banned_word_scan(copy: dict) -> list:
    headline = copy.get("headline", "").lower()
    subheadline = copy.get("subheadline", "").lower()
    paragraphs = " ".join(copy.get("paragraphs", [])).lower()
    cta = copy.get("cta", "").lower()
    full_text = f"{headline} {subheadline} {paragraphs} {cta}"
    return [w for w in BANNED_WORDS if w in full_text]


def evaluate_copy(copy: dict, job_id: str = "unknown") -> dict:
    # This endpoint must never 500: it is the only step Go hard-fails the whole
    # workflow on, and Go's own fallback for it silently drops banned-word
    # enforcement. The deterministic scan always runs; the LLM call degrades
    # gracefully instead of raising.
    found_banned = _deterministic_banned_word_scan(copy)

    try:
        llm = get_chat_model("evaluator")
        chain = PROMPT | llm.with_structured_output(EvaluatorLLMOutput, include_raw=True)
        result = chain.invoke({"copy": json.dumps(copy)})
        if result["parsing_error"] or result["parsed"] is None:
            raise RuntimeError(str(result["parsing_error"]))

        diagnostics.set_status("llm.evaluator", "real", None)
        log_stage(logger, job_id, "evaluate", f"raw={result['raw']}")
        parsed: EvaluatorLLMOutput = result["parsed"]
        llm_passed, tone_assessment, llm_score = parsed.passed, parsed.tone_assessment, parsed.score
    except Exception as e:
        diagnostics.set_status("llm.evaluator", "degraded", str(e))
        log_stage(logger, job_id, "evaluate", f"LLM evaluation degraded, using deterministic-only result: {e}", level="warning")
        llm_passed, tone_assessment, llm_score = True, f"DEGRADED: {e}", 70

    passed = llm_passed and len(found_banned) == 0
    score = llm_score if len(found_banned) == 0 else min(llm_score, 50)

    return {
        "passed": passed,
        "banned_words_found": found_banned,
        "tone_assessment": tone_assessment,
        "score": score,
    }
