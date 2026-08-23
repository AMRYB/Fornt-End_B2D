"""Environment-based configuration for the agentic core.

Secrets are only ever read from environment variables / `.env` and are never
logged. Gemini uses a distinct key for every agent so concurrent calls do not
share one credential's rate-limit budget.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent

GEMINI_AGENT_NAMES = (
    "discovery",
    "requirements",
    "architecture",
    "database",
    "api",
    "devops",
    "reviewer",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Credentials
    cursor_api_key: str = ""
    kimi_api_key: str = ""
    openrouter_api_key: str = ""
    llm_api_key: str = ""
    gemini_discovery_api_key: str = ""
    gemini_requirements_api_key: str = ""
    gemini_architecture_api_key: str = ""
    gemini_database_api_key: str = ""
    gemini_api_api_key: str = ""
    gemini_devops_api_key: str = ""
    gemini_reviewer_api_key: str = ""

    # Supabase is the durable production store and authentication provider.
    # The anon key is intentionally public and is exposed to the browser through
    # ``GET /api/config``.  The service-role key is server-only and must never be
    # returned by an endpoint or bundled into frontend assets.
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""

    # Deployment / security
    app_env: str = "development"
    vercel: bool = False
    frontend_origin: str = ""
    app_base_url: str = ""
    allow_anonymous_local: bool = True
    # Hard wall-clock budget for one HTTP-triggered agent stage. Vercel Hobby
    # allows 300 seconds; the clamp below leaves time to persist a checkpoint
    # and return a response after provider/structured-output retries.
    request_deadline_s: float = 190.0
    daily_project_limit: int = 5
    daily_discovery_limit: int = 30
    daily_generation_stage_limit: int = 40

    # LLM provider selection
    llm_provider: str = "cursor"
    llm_model: str = "default"
    llm_base_url: str = "https://api.cursor.com/v1"
    kimi_base_url: str = "https://api.moonshot.cn/v1"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_model: str = "gemini-2.5-flash"
    llm_request_timeout_s: float = 120.0
    llm_max_tokens: int = 8192
    llm_poll_interval_s: float = 1.0
    llm_poll_timeout_s: float = 300.0

    # Workflow behaviour
    structured_output_max_retries: int = 1
    # The reviewer runs at most this many times per workflow (exactly one
    # review round: PASS or regenerate once, then complete).
    max_review_rounds: int = 1
    # Each artifact may be regenerated at most this many times per workflow.
    max_artifact_revisions: int = 1
    # Bounded retries for transient provider/transport failures.
    max_llm_retries: int = 1

    # Persistence
    data_dir: Path = ROOT_DIR / "data"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "b2d.db"

    @property
    def supabase_configured(self) -> bool:
        return bool(
            self.supabase_url.strip()
            and self.supabase_anon_key.strip()
            and self.supabase_service_role_key.strip()
        )

    @property
    def auth_enabled(self) -> bool:
        return self.supabase_configured

    @property
    def is_production(self) -> bool:
        return self.vercel or self.app_env.strip().lower() in {
            "production",
            "prod",
        }

    @property
    def effective_request_deadline_s(self) -> float:
        return min(max(float(self.request_deadline_s), 10.0), 210.0)

    @property
    def cors_origins(self) -> list[str]:
        origins = [
            origin.strip().rstrip("/")
            for origin in self.frontend_origin.split(",")
            if origin.strip()
        ]
        if self.app_base_url.strip():
            origins.append(self.app_base_url.strip().rstrip("/"))
        # Same-origin production requests do not need CORS.  These two local
        # origins keep development convenient without opening production to '*'.
        origins.extend(["http://localhost:3000", "http://localhost:8000"])
        return list(dict.fromkeys(origins))

    @property
    def supabase_host(self) -> str:
        return urlparse(self.supabase_url).hostname or ""

    def effective_provider(self) -> str:
        return (self.llm_provider or "cursor").strip().lower()

    def effective_model(self) -> str:
        if self.effective_provider() == "gemini":
            if self.llm_model and self.llm_model != "default":
                return self.llm_model
            return self.gemini_model or "gemini-2.5-flash"
        if self.effective_provider() == "kimi":
            return (
                self.llm_model
                if self.llm_model and self.llm_model != "default"
                else "moonshotai/Kimi-K2-Instruct"
            )
        if self.effective_provider() == "openrouter":
            return (
                self.llm_model
                if self.llm_model and self.llm_model != "default"
                else "moonshotai/kimi-k2.6:free"
            )
        if self.llm_model and self.llm_model != "default":
            return self.llm_model
        return "default"

    def effective_model_name(self) -> str:
        return self.effective_model()

    def effective_base_url(self) -> str:
        if self.effective_provider() == "gemini":
            return (
                self.gemini_base_url
                or self.llm_base_url
                or "https://generativelanguage.googleapis.com/v1beta"
            )
        if self.effective_provider() == "kimi":
            return self.kimi_base_url or self.llm_base_url or "https://api.moonshot.cn/v1"
        if self.effective_provider() == "openrouter":
            return self.openrouter_base_url or self.llm_base_url or "https://openrouter.ai/api/v1"
        return self.llm_base_url or "https://api.cursor.com/v1"

    def effective_base_url_value(self) -> str:
        return self.effective_base_url()

    def effective_api_key(self) -> str:
        if self.effective_provider() == "gemini":
            # Generic callers use the discovery credential. Normal application
            # wiring calls gemini_api_key_for() for each individual agent.
            return self.gemini_api_key_for("discovery")
        if self.effective_provider() == "kimi":
            return self.kimi_api_key or self.llm_api_key or ""
        if self.effective_provider() == "openrouter":
            return self.openrouter_api_key or self.llm_api_key or ""
        return self.llm_api_key or self.cursor_api_key or ""

    def effective_api_key_value(self) -> str:
        return self.effective_api_key()

    def gemini_api_key_for(self, agent_name: str) -> str:
        """Return the dedicated Gemini credential for one known agent."""
        keys = {
            "discovery": self.gemini_discovery_api_key,
            "requirements": self.gemini_requirements_api_key,
            "architecture": self.gemini_architecture_api_key,
            "database": self.gemini_database_api_key,
            "api": self.gemini_api_api_key,
            "devops": self.gemini_devops_api_key,
            "reviewer": self.gemini_reviewer_api_key,
        }
        try:
            return keys[agent_name].strip()
        except KeyError as exc:
            raise ValueError(f"Unknown Gemini agent: {agent_name!r}") from exc

    def ensure_dirs(self) -> None:
        # data_dir is the parent of the SQLite database; runs/artifacts remain
        # directory-based, so keep creating them.
        # Vercel's application bundle is read-only.  When Supabase is fully
        # configured every durable store is remote, so avoid touching disk at
        # import time.
        if self.supabase_configured:
            return
        for path in (self.data_dir, self.runs_dir, self.artifacts_dir):
            path.mkdir(parents=True, exist_ok=True)

    def check_credentials(self) -> None:
        if self.effective_provider() == "gemini":
            missing = [
                name for name in GEMINI_AGENT_NAMES if not self.gemini_api_key_for(name)
            ]
            if missing:
                variables = ", ".join(
                    f"GEMINI_{name.upper()}_API_KEY" for name in missing
                )
                raise RuntimeError(
                    "Missing dedicated Gemini API key(s): "
                    f"{variables}. Configure one different key per agent in `.env`."
                )
            keys = [self.gemini_api_key_for(name) for name in GEMINI_AGENT_NAMES]
            if len(set(keys)) != len(keys):
                raise RuntimeError(
                    "Gemini agent API keys must be distinct; duplicate credentials were found."
                )
            return
        if self.effective_provider() == "kimi":
            if not self.effective_api_key_value():
                raise RuntimeError(
                    "No Kimi API key configured. Set KIMI_API_KEY or LLM_API_KEY in `.env`."
                )
            return
        if self.effective_provider() == "openrouter":
            if not self.effective_api_key_value():
                raise RuntimeError(
                    "No OpenRouter API key configured. Set OPENROUTER_API_KEY or LLM_API_KEY in `.env`."
                )
            return
        if not self.effective_api_key_value():
            raise RuntimeError(
                "No Cursor API key configured. Set CURSOR_API_KEY or LLM_API_KEY in `.env`."
            )

    def check_supabase_configuration(self) -> None:
        values = {
            "SUPABASE_URL": self.supabase_url.strip(),
            "SUPABASE_ANON_KEY": self.supabase_anon_key.strip(),
            "SUPABASE_SERVICE_ROLE_KEY": self.supabase_service_role_key.strip(),
        }
        if any(values.values()) and not all(values.values()):
            missing = ", ".join(name for name, value in values.items() if not value)
            raise RuntimeError(
                "Supabase configuration is incomplete. Missing: " + missing
            )
        if self.is_production and not all(values.values()):
            raise RuntimeError(
                "Production requires SUPABASE_URL, SUPABASE_ANON_KEY, and "
                "SUPABASE_SERVICE_ROLE_KEY; local storage/auth fallback is disabled."
            )
        if self.is_production and self.allow_anonymous_local:
            raise RuntimeError(
                "Production requires ALLOW_ANONYMOUS_LOCAL=false."
            )

    def check_runtime_limits(self) -> None:
        limits = {
            "DAILY_PROJECT_LIMIT": self.daily_project_limit,
            "DAILY_DISCOVERY_LIMIT": self.daily_discovery_limit,
            "DAILY_GENERATION_STAGE_LIMIT": self.daily_generation_stage_limit,
        }
        invalid = [name for name, value in limits.items() if not 1 <= value <= 10000]
        if invalid:
            raise RuntimeError(
                "Daily usage limits must be between 1 and 10000: "
                + ", ".join(invalid)
            )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.check_supabase_configuration()
    settings.check_runtime_limits()
    settings.ensure_dirs()
    settings.check_credentials()
    return settings
