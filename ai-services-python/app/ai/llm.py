"""Single seam for every chat-model call: which provider, and one structured invocation.

Isolating this here means "does the LangChain wrapper reach the provider correctly" is a
question this file answers alone, separate from the prompt/parsing logic in planner.py /
writer.py / critic.py / evaluator.py / profile.py / extractor.py / ranker.py, and switching
providers (Gemini <-> OpenRouter <-> anything OpenAI-compatible) stays a config change.

Free tiers fail often - overloaded (503) or daily cap reached (429) - and a failed call costs
the whole step its LLM output. So a call that fails on the primary provider is retried once on
the configured fallback provider (LLM_FALLBACK_*), which is normally a different vendor
entirely. The fallback is also used for a structured-output parsing failure, since another
model may well produce valid output.

Embeddings (app/rag/search.py) are NOT built here - OpenRouter has no embeddings endpoint, so
that call stays on the Gemini SDK directly regardless of what's configured here.
"""
import logging
from typing import Any, Dict, Type, TypeVar

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from app import diagnostics
from app.config import settings
from app.observability import log_stage

logger = logging.getLogger("ai.llm")

T = TypeVar("T", bound=BaseModel)

PRIMARY = "primary"
FALLBACK = "fallback"


def get_chat_model(purpose: str, provider: str = PRIMARY) -> ChatOpenAI:
    # purpose (e.g. "planner", "critic") is also the diagnostics key (llm.<purpose>).
    # Only "critic" can be routed to a different model, via LLM_CRITIC_MODEL.
    if provider == FALLBACK:
        api_url, api_key, model = settings.llm_fallback_api_url, settings.llm_fallback_api_key, settings.llm_fallback_model
    else:
        api_url, api_key = settings.llm_api_url, settings.llm_api_key
        model = settings.llm_critic_model if purpose == "critic" and settings.llm_critic_model else settings.llm_model
    return ChatOpenAI(
        model=model,
        openai_api_key=api_key or "unset",
        openai_api_base=api_url,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )


def _invoke_once(purpose: str, prompt: ChatPromptTemplate, schema: Type[T], inputs: Dict[str, Any], provider: str) -> T:
    """One attempt against one provider. Separated so tests can stub it."""
    chain = prompt | get_chat_model(purpose, provider).with_structured_output(schema, include_raw=True)
    result = chain.invoke(inputs)
    if result["parsing_error"] or result["parsed"] is None:
        raise RuntimeError(f"structured output failed: {result['parsing_error']}")
    return result["parsed"]


def invoke_structured(purpose: str, prompt: ChatPromptTemplate, schema: Type[T], inputs: Dict[str, Any], job_id: str) -> T:
    """Run prompt -> chat model -> schema, falling back to the secondary provider if the
    primary one fails. Records the outcome under diagnostics key llm.<purpose>:
    "real" (primary), "fallback" (secondary), or "failed" (both). Raises if all fail."""
    providers = [PRIMARY] + ([FALLBACK] if settings.has_fallback_provider else [])
    primary_error: Exception = RuntimeError("no provider attempted")

    for provider in providers:
        try:
            parsed = _invoke_once(purpose, prompt, schema, inputs, provider)
        except Exception as e:
            if provider == PRIMARY:
                primary_error = e
                if FALLBACK in providers:
                    log_stage(logger, job_id, purpose, f"primary provider failed ({e}); retrying on the fallback provider", level="warning")
                continue
            diagnostics.set_status(f"llm.{purpose}", "failed", f"primary: {primary_error} | fallback: {e}")
            log_stage(logger, job_id, purpose, f"fallback provider also failed: {e}", level="warning")
            raise e from primary_error

        if provider == PRIMARY:
            diagnostics.set_status(f"llm.{purpose}", "real", None)
        else:
            diagnostics.set_status(f"llm.{purpose}", "fallback", f"primary failed: {primary_error}")
            log_stage(logger, job_id, purpose, f"answered by the fallback provider ({settings.llm_fallback_model})")
        log_stage(logger, job_id, purpose, "ok")
        return parsed

    diagnostics.set_status(f"llm.{purpose}", "failed", str(primary_error))
    raise primary_error


def used_fallback(purpose: str) -> bool:
    return diagnostics.get_status(f"llm.{purpose}")["mode"] == "fallback"
