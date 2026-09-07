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

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"

    @property
    def has_gemini_key(self) -> bool:
        # A set-but-empty-string env var must be treated as unset, not as a valid key.
        return bool(self.gemini_api_key.strip())


settings = Settings()


def log_startup_config() -> None:
    """Log config state at process start - redacted, but with enough signal
    (presence + length) to catch a missing/empty key immediately instead of
    three fallback layers deep during an actual request."""
    if settings.has_gemini_key:
        logger.info(f"[config] GEMINI_API_KEY is set (length={len(settings.gemini_api_key.strip())})")
    else:
        logger.warning("[config] GEMINI_API_KEY is NOT set - LLM and embedding calls will use mock/fallback behavior")
    logger.info(f"[config] GEMINI_MODEL={settings.gemini_model}")
