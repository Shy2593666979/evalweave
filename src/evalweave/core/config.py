from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApplicationConfig(StrictModel):
    name: str = "EvalWeave"
    version: str = "0.1.0"
    debug: bool = False


class ServerConfig(StrictModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    reload: bool = False


class DatabaseConfig(StrictModel):
    url: str
    echo: bool = False
    pool_size: int = Field(default=10, ge=1)
    max_overflow: int = Field(default=20, ge=0)
    pool_recycle: int = Field(default=3600, ge=0)


class RedisConfig(StrictModel):
    url: str


class CeleryConfig(StrictModel):
    broker_url: str
    result_backend: str
    timezone: str = "UTC"
    task_time_limit: int = Field(default=600, ge=1)
    worker_concurrency: int = Field(default=4, ge=1)


class StorageConfig(StrictModel):
    type: Literal["local"] = "local"
    local_directory: Path = Path("./data/uploads")


class LoggingConfig(StrictModel):
    level: str = "INFO"
    format: Literal["console", "json"] = "console"
    directory: Path = Path("./logs")


class EvaluationConfig(StrictModel):
    default_timeout_seconds: int = Field(default=60, ge=1)
    default_concurrency: int = Field(default=5, ge=1)
    max_file_size_mb: int = Field(default=50, ge=1)
    allowed_extensions: list[str] = Field(default_factory=lambda: ["json", "jsonl", "csv", "xlsx"])


class TargetAuthFlowConfig(StrictModel):
    login_url: str
    body: dict[str, Any] = Field(default_factory=dict)
    token_path: str = ""
    header_name: str = "Authorization"
    header_prefix: str = "Bearer "


class AgentConfig(StrictModel):
    enabled: bool = False
    api_mode: Literal["responses", "chat_completions"] = "responses"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = ""
    timeout_seconds: int = Field(default=120, ge=1)
    max_repair_attempts: int = Field(default=3, ge=0, le=10)
    dry_run_cases: int = Field(default=5, ge=1, le=100)
    require_approval: bool = True
    # Accepted only so existing application.yaml files continue to load. Target requests are not
    # restricted by this legacy setting anymore.
    allowed_target_hosts: list[str] = Field(default_factory=list, exclude=True)
    target_headers: dict[str, str] = Field(default_factory=dict)
    target_auth_flows: dict[str, TargetAuthFlowConfig] = Field(default_factory=dict)


class WeComNotificationConfig(StrictModel):
    enabled: bool = False
    webhook_url: str = ""
    timeout_seconds: int = Field(default=10, ge=1)


class EmailNotificationConfig(StrictModel):
    enabled: bool = False
    host: str = ""
    port: int = Field(default=465, ge=1, le=65535)
    username: str = ""
    password: str = ""
    from_address: str = ""
    use_ssl: bool = True
    starttls: bool = False
    timeout_seconds: int = Field(default=10, ge=1)


class NotificationConfig(StrictModel):
    platform_base_url: str = "http://127.0.0.1:5173"
    wecom: WeComNotificationConfig = Field(default_factory=WeComNotificationConfig)
    email: EmailNotificationConfig = Field(default_factory=EmailNotificationConfig)


class BootstrapAdminConfig(StrictModel):
    enabled: bool = True
    username: str = "admin"
    password: str = Field(min_length=8)


class AuthConfig(StrictModel):
    jwt_secret: str = Field(min_length=32)
    token_expire_minutes: int = Field(default=480, ge=5)
    cookie_name: str = "evalweave_session"
    cookie_secure: bool = False
    allow_registration: bool = True
    bootstrap_admin: BootstrapAdminConfig


class Settings(StrictModel):
    application: ApplicationConfig
    server: ServerConfig
    database: DatabaseConfig
    redis: RedisConfig
    celery: CeleryConfig
    storage: StorageConfig
    logging: LoggingConfig
    evaluation: EvaluationConfig
    agent: AgentConfig = Field(default_factory=AgentConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    auth: AuthConfig


DEFAULT_CONFIG_PATH = Path("config/application.yaml")
_active_config_path = DEFAULT_CONFIG_PATH


def set_config_path(path: str | Path) -> None:
    global _active_config_path
    _active_config_path = Path(path)
    get_settings.cache_clear()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    path = _active_config_path.resolve()
    if not path.is_file():
        raise FileNotFoundError(
            f"Configuration file not found: {path}. "
            "Copy config/application.example.yaml to config/application.yaml."
        )
    with path.open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream) or {}
    return Settings.model_validate(raw)
