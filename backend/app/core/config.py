from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    app_env: Literal["local", "test", "production"] = "local"
    app_origin: str = "http://localhost:3100"
    database_url: SecretStr
    storage_root: Path = PROJECT_ROOT / "var"
    cookie_secure: bool = False
    session_hours: int = Field(default=12, ge=1, le=168)
    llm_provider: Literal["openai_compatible", "fixture"] = "openai_compatible"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_token_parameter: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"
    llm_structured_output: Literal["json_schema", "json_object"] = "json_schema"
    model_timeout_seconds: float = Field(default=20, ge=0.1, le=60)
    agent_timeout_seconds: float = Field(default=60, ge=1, le=180)
    agent_max_steps: int = Field(default=8, ge=2, le=32)
    agent_max_tool_calls: int = Field(default=12, ge=1, le=64)
    agent_total_token_budget: int = Field(default=30000, ge=1000, le=100000)
    model_output_token_limit: int = Field(default=3000, ge=100, le=4000)
    model_input_token_limit: int = Field(default=24000, ge=2000, le=200000)
    model_output_reserve: int = Field(default=4000, ge=500, le=32000)
    history_max_turns: int = Field(default=12, ge=0, le=50)
    max_message_chars: int = Field(default=16000, ge=1, le=64000)
    max_diff_files: int = Field(default=200, ge=1, le=1000)
    max_diff_bytes: int = Field(default=2097152, ge=1024, le=20971520)
    repo_health_fresh_seconds: int = Field(default=300, ge=30, le=3600)
    tool_result_max_bytes: int = Field(default=65536, ge=4096, le=1048576)
    # Cumulative model-visible tool output per run (bounds context growth until
    # the M6 context budgeter takes over).
    tool_bytes_total_limit: int = Field(default=262144, ge=16384, le=4194304)
    worker_heartbeat_seconds: int = Field(default=10, ge=1, le=30)
    github_read_token: SecretStr = SecretStr("")
    github_write_token: SecretStr = SecretStr("")
    github_api_base: str = "https://api.github.com"
    # httpx trust_env: system proxy/CA settings. Default off so GitHub requests
    # egress deterministically; enable when the network requires a proxy.
    github_trust_env: bool = False
    # Optional HTTP(S) proxy for git fetch/clone of GitHub repositories, e.g.
    # http://127.0.0.1:7897. Empty means direct connections.
    github_git_proxy: str = ""
    sync_max_pages: int = Field(default=4, ge=1, le=20)
    sync_issue_window_days: int = Field(default=90, ge=7, le=365)
    sync_ci_window_days: int = Field(default=30, ge=1, le=365)
    sync_overlap_minutes: int = Field(default=5, ge=0, le=60)
    sync_review_max_prs: int = Field(default=10, ge=1, le=50)
    sync_ci_job_max_runs: int = Field(default=5, ge=0, le=25)
    job_lease_seconds: int = Field(default=60, ge=30, le=600)
    job_max_attempts: int = Field(default=3, ge=1, le=10)
    job_retry_delay_seconds: int = Field(default=30, ge=5, le=3600)
    job_retry_max_delay_seconds: int = Field(default=900, ge=30, le=7200)
    worker_poll_seconds: float = Field(default=2, ge=0.2, le=30)
    worker_recovery_seconds: int = Field(default=60, ge=10, le=600)
    http_read_timeout_seconds: float = Field(default=20, ge=1, le=120)
    http_max_retries: int = Field(default=2, ge=0, le=5)
    max_code_file_bytes: int = Field(default=1048576, ge=1024, le=10485760)
    max_read_lines: int = Field(default=400, ge=1, le=2000)
    max_search_matches: int = Field(default=100, ge=1, le=500)
    search_timeout_seconds: float = Field(default=5, ge=1, le=60)

    @field_validator("app_origin")
    @classmethod
    def valid_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("APP_ORIGIN must be an http(s) origin")
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("APP_ORIGIN must contain only scheme and host/port")
        return value.rstrip("/")

    @field_validator("llm_base_url", "github_api_base", "github_git_proxy")
    @classmethod
    def valid_model_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("URL must be http(s)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Do not place credentials in the URL")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote endpoints must use HTTPS")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_security(self) -> "Settings":
        if self.app_env == "production" and (
            not self.cookie_secure or not self.app_origin.startswith("https://")
        ):
            raise ValueError("Production requires HTTPS and secure cookies")
        if not self.database_url.get_secret_value().startswith("postgresql+psycopg://"):
            if self.app_env != "test":
                raise ValueError("DATABASE_URL must use postgresql+psycopg")
        if not self.storage_root.is_absolute():
            self.storage_root = (PROJECT_ROOT / self.storage_root).resolve()
        return self

    @property
    def model_configured(self) -> bool:
        return self.llm_provider == "fixture" or bool(
            self.llm_base_url and self.llm_model and self.llm_api_key.get_secret_value()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
