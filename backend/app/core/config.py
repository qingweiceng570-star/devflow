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
    model_output_token_limit: int = Field(default=1000, ge=100, le=4000)
    worker_heartbeat_seconds: int = Field(default=10, ge=1, le=30)

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

    @field_validator("llm_base_url")
    @classmethod
    def valid_model_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("LLM_BASE_URL must be http(s)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Do not place credentials in LLM_BASE_URL")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote model endpoints must use HTTPS")
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
