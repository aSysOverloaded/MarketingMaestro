"""Single seam for constructing the chat model used by every AI module.

Isolating this behind one function means "does the LangChain wrapper reach the
provider correctly" is a question this file answers alone, separate from
prompt/parsing logic in planner.py/writer.py/critic.py/evaluator.py, and means
switching providers (Gemini <-> OpenRouter <-> anything OpenAI-compatible) is a
one-file change.

Embeddings (app/rag/search.py) are NOT built here - OpenRouter has no embeddings
endpoint, so that call stays on the Gemini SDK directly regardless of what's
configured here.
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


def get_chat_model(purpose: str) -> ChatOpenAI:
    # purpose (e.g. "planner", "writer") is used by callers as the diagnostics
    # subsystem key (llm.<purpose>) and in job-correlated logging, not here.
    return ChatOpenAI(
        model=settings.llm_model,
        openai_api_key=settings.llm_api_key or "unset",
        openai_api_base=settings.llm_api_url,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )


def invoke_structured(purpose: str, prompt: ChatPromptTemplate, schema: Type[T], inputs: Dict[str, Any], job_id: str) -> T:
    """Run prompt -> chat model -> schema, recording the outcome under diagnostics key
    llm.<purpose>. Raises on transport or parsing failure; callers decide the fallback."""
    chain = prompt | get_chat_model(purpose).with_structured_output(schema, include_raw=True)
    try:
        result = chain.invoke(inputs)
    except Exception as e:
        diagnostics.set_status(f"llm.{purpose}", "failed", str(e))
        raise

    if result["parsing_error"] or result["parsed"] is None:
        diagnostics.set_status(f"llm.{purpose}", "failed", str(result["parsing_error"]))
        log_stage(logger, job_id, purpose, f"structured output failed: {result['parsing_error']}", level="warning")
        raise RuntimeError(f"{purpose} structured output failed: {result['parsing_error']}")

    diagnostics.set_status(f"llm.{purpose}", "real", None)
    log_stage(logger, job_id, purpose, f"raw={result['raw']}")
    return result["parsed"]
