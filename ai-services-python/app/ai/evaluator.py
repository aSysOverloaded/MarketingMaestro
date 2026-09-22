import json
import logging
import re

from langchain_core.prompts import ChatPromptTemplate

from app.ai.llm import invoke_structured
from app.ai.schemas import EvaluatorLLMOutput
from app.observability import log_stage

logger = logging.getLogger("ai.evaluator")

BANNED_WORDS = ["cheap", "unreliable", "garbage", "competitor"]

PROMPT = ChatPromptTemplate.from_template(
    """You are a brand quality evaluation agent (Evaluator).
Analyze the following marketing copy draft and grade it on readability, style consistency, and alignment with a professional, helpful tone.

Copy Draft:
{copy}

Rate the tone, grade the overall suitability score (0-100), and determine if it meets brand voice standards (passing score >= 75)."""
)


def banned_words_in(copy: dict) -> list:
    headline = copy.get("headline", "").lower()
    subheadline = copy.get("subheadline", "").lower()
    paragraphs = " ".join(copy.get("paragraphs", [])).lower()
    cta = copy.get("cta", "").lower()
    full_text = f"{headline} {subheadline} {paragraphs} {cta}"
    # Whole-word match only: a plain substring check flagged "affordable" as containing
    # "ford" and hard-failed the whole workflow on perfectly normal budget-focused copy.
    return [w for w in BANNED_WORDS if re.search(rf"\b{re.escape(w)}\b", full_text)]


def evaluate_copy(copy: dict, job_id: str = "unknown") -> dict:
    # Must never raise: the deterministic banned-word scan always runs, and the LLM tone
    # check degrades gracefully (reported via "degraded") instead of failing the review.
    found_banned = banned_words_in(copy)

    try:
        parsed = invoke_structured("evaluator", PROMPT, EvaluatorLLMOutput, {"copy": json.dumps(copy)}, job_id)
        llm_passed, tone_assessment, llm_score = parsed.passed, parsed.tone_assessment, parsed.score
        degraded = False
    except Exception as e:
        log_stage(logger, job_id, "evaluate", f"LLM evaluation degraded, using deterministic-only result: {e}", level="warning")
        llm_passed, tone_assessment, llm_score = True, f"DEGRADED: {e}", 70
        degraded = True

    passed = llm_passed and len(found_banned) == 0
    score = llm_score if len(found_banned) == 0 else min(llm_score, 50)

    return {
        "passed": passed,
        "banned_words_found": found_banned,
        "tone_assessment": tone_assessment,
        "score": score,
        "degraded": degraded,
    }
