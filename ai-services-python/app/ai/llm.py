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
from langchain_openai import ChatOpenAI

from app.config import settings


def get_chat_model(purpose: str) -> ChatOpenAI:
    # purpose (e.g. "planner", "writer") is used by callers as the diagnostics
    # subsystem key (llm.<purpose>) and in job-correlated logging, not here.
    return ChatOpenAI(
        model=settings.llm_model,
        openai_api_key=settings.llm_api_key or "unset",
        openai_api_base=settings.llm_api_url,
    )
