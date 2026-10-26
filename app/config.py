"""All settings in one place.

pydantic-settings reads each field from an environment variable with the same
name (case-insensitive), falling back to the .env file, then to the default
written here. Real environment variables win over .env, so a deployed server can override a local file.
"""
import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent  # the project folder (parent of app/)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # "LLM_MODEL=" in .env means "use the default"
        extra="ignore",  # other variables in .env are not an error
        populate_by_name=True,  # lets tests write Settings(llm_api_key="...")
    )

    # --- LLM (any OpenAI-compatible API; defaults target OpenRouter) ---
    llm_api_key: str = Field("", validation_alias=AliasChoices("LLM_API_KEY", "OPENROUTER_API_KEY"))
    llm_model: str = "openrouter/free"  # router that picks a currently-available free model
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_fallback_model: str = ""  # comma-separated models to try, in order, if the first fails
    llm_timeout_seconds: float = 60  # max wait for one model request
    llm_max_tokens: int = 8000  # answer plus any "thinking"
    llm_idle_seconds: float = 30  # max silence on the connection
    llm_first_token_seconds: float = 45  # give up on a model still "thinking" with no answer
    llm_cooldown_seconds: float = 600  # skip a model that just failed for this long
    llm_total_seconds: float = 120  # time budget across the whole model chain
    llm_reasoning_effort: str = "off"  # off = ask the model not to think (default; falls back if refused) | none = send no reasoning setting | low | medium | high
    llm_max_reasoning_chars: int = 9000  # give up when thinking runs past this with no answer

    # --- caching ---
    cache_days: float = 14
    cache_dir: Path = Path(tempfile.gettempdir()) / "surgery-plan-cache"

    # --- reviewed plans (see app/procedures.py and app/make_plans.py) ---
    plans_dir: Path = ROOT / "plans"  # approved plans, shipped with the app
    drafts_dir: Path = ROOT / "drafts"  # work in progress, never shipped

    # --- web server ---
    rate_limit_per_hour: int = 36  # per IP; one plan is one request
    # Only turn this on behind a proxy you trust (Render, Vercel, nginx...). Otherwise anyone
    # can fake the X-Forwarded-For header and dodge the rate limit.
    trust_proxy_headers: bool = False
    host: str = "127.0.0.1"
    port: int = 3000

    @field_validator("llm_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def model_chain(self) -> list[str]:
        """Main model first, then each fallback, without duplicates."""
        fallbacks = [m.strip() for m in self.llm_fallback_model.split(",") if m.strip()]
        return list(dict.fromkeys([self.llm_model, *fallbacks]))


@lru_cache
def get_settings() -> Settings:
    """Build Settings once and reuse it. FastAPI routes get it via Depends(get_settings)."""
    return Settings()
