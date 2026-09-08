"""Single source of truth for sidecar configuration.

Replaces scattered os.getenv() calls. Loads from a .env file in this service's
own working directory (ai-services-python/.env) via pydantic-settings, plus
real process environment variables (which take precedence over .env).

This exists because of a real bug: GEMINI_API_KEY was exported in one shell
and the sidecar was started in a different one, so the key silently "existed"
from the developer's point of view but was unset in the actual process. A
config object that logs whether it actually loaded a key - not just whether
one is expected - makes that failure mode visible at startup instead of three
layers of fallback later.
"""
import logging
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("config")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Used only for embeddings (app/rag/search.py) - OpenRouter has no embeddings endpoint,
    # so this is the one call that can't move off Gemini.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"

    # Used for the 4 chat steps (planner/writer/critic/evaluator). Names mirror
    # backend-go/.env so the two services share a mental model, even though each
    # process reads its own .env file and the key must be set in both.
    llm_api_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_model: str = "nvidia/nemotron-3-super-120b-a12b:free"

    @property
    def has_gemini_key(self) -> bool:
        # A set-but-empty-string env var must be treated as unset, not as a valid key.
        return bool(self.gemini_api_key.strip())

    @property
    def has_llm_key(self) -> bool:
        return bool(self.llm_api_key.strip())


settings = Settings()


def log_startup_config() -> None:
    """Log config state at process start - redacted, but with enough signal
    (presence + length) to catch a missing/empty key immediately instead of
    three fallback layers deep during an actual request."""
    if settings.has_gemini_key:
        logger.info(f"[config] GEMINI_API_KEY is set (length={len(settings.gemini_api_key.strip())}) - used for embeddings only")
    else:
        logger.warning("[config] GEMINI_API_KEY is NOT set - embedding calls will use mock vector fallback")
    logger.info(f"[config] GEMINI_MODEL={settings.gemini_model}")

    if settings.has_llm_key:
        logger.info(f"[config] LLM_API_KEY is set (length={len(settings.llm_api_key.strip())}) - used for plan/write/critic/evaluate")
    else:
        logger.warning("[config] LLM_API_KEY is NOT set - plan/write/critic will fail loud, evaluate will degrade")
    logger.info(f"[config] LLM_API_URL={settings.llm_api_url}")
    logger.info(f"[config] LLM_MODEL={settings.llm_model}")
