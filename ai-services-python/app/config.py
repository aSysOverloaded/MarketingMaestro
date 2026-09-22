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
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("config")

# ai-services-python/ - templates, static and storage are resolved from here so the
# service works regardless of which directory it is launched from.
SERVICE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=SERVICE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    # Used only for embeddings (app/rag/search.py) - OpenRouter has no embeddings endpoint,
    # so this is the one call that can't move off Gemini.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"

    # Used for every chat step (profile, extraction, ranking, planner, writer, critic, evaluator).
    llm_api_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    # Free-tier lineups rotate and get overloaded; check docs/IMPROVEMENTS.md for what last worked.
    llm_model: str = "nex-agi/nex-n2.5-pro:free"
    # Optional stronger model for the spec critic only (same endpoint/key). Fact-checking is
    # where a weak model hurts most, and it is one call per draft. Empty = use LLM_MODEL.
    llm_critic_model: str = ""

    # Backup provider, used when a call to the primary one fails (free tiers are frequently
    # overloaded or capped). Normally a different vendor, so both rarely fail at once.
    llm_fallback_api_url: str = ""
    llm_fallback_api_key: str = ""
    llm_fallback_model: str = ""

    # Chat-model call limits. Kept tight on purpose: every step has its own fallback, so a
    # hung provider should fail over quickly rather than hold the request for minutes.
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2

    # Optional SMTP for /api/send-email. Unset host/user = email is logged locally, not sent.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""

    # Skip PDF rendering entirely (e.g. no Chromium available). The response then carries
    # pdf_url = null plus a warning - there is no mock PDF.
    disable_pdf: bool = False

    # Root for generated files (compiled HTML, PDFs, extracted catalog images, email logs).
    # Relative paths resolve against this service's directory, not the process cwd.
    storage_dir: Path = SERVICE_DIR / "storage"

    @property
    def has_fallback_provider(self) -> bool:
        return bool(self.llm_fallback_api_url.strip() and self.llm_fallback_api_key.strip() and self.llm_fallback_model.strip())

    @property
    def has_smtp(self) -> bool:
        return bool(self.smtp_host.strip() and self.smtp_user.strip())

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
        logger.info(f"[config] LLM_API_KEY is set (length={len(settings.llm_api_key.strip())}) - used for every chat step")
    else:
        logger.warning("[config] LLM_API_KEY is NOT set - every chat step will run on its deterministic fallback")
    logger.info(f"[config] LLM_API_URL={settings.llm_api_url}")
    logger.info(f"[config] LLM_MODEL={settings.llm_model}")
    if settings.llm_critic_model:
        logger.info(f"[config] LLM_CRITIC_MODEL={settings.llm_critic_model}")
    if settings.has_fallback_provider:
        logger.info(f"[config] fallback provider: {settings.llm_fallback_model} @ {settings.llm_fallback_api_url}")
    else:
        logger.warning("[config] no fallback provider configured (LLM_FALLBACK_*) - a failed call means that step falls back to its deterministic path")
    logger.info(f"[config] SMTP {'configured for ' + settings.smtp_host if settings.has_smtp else 'NOT configured - emails are logged locally'}")
    logger.info(f"[config] PDF rendering {'DISABLED' if settings.disable_pdf else 'enabled'}; storage_dir={settings.storage_dir}")
