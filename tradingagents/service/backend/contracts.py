"""HTTP and trusted configuration contracts, separate from graph input."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Literal, get_args
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator

from ..models import AnalysisRequest, Contract, LLMChoice

APIProvider = Literal[
    "openai",
    "anthropic",
    "google",
    "xai",
    "deepseek",
    "qwen",
    "qwen-cn",
    "glm",
    "glm-cn",
    "minimax",
    "minimax-cn",
    "openrouter",
    "mistral",
    "kimi",
    "groq",
    "nvidia",
]


class ConnectionInput(Contract):
    provider: APIProvider
    api_key: SecretStr = Field(exclude=True)
    name: str = Field(default="", max_length=80, pattern=r"^[^\x00-\x1f\x7f]*$")

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value):
        raw = value.get_secret_value()
        if not 8 <= len(raw) <= 4096 or any(c.isspace() for c in raw):
            raise ValueError("INVALID_PROVIDER_KEY")
        return value


class ModelBinding(Contract):
    connection_id: UUID
    model: str

    @field_validator("model")
    @classmethod
    def valid_model(cls, value):
        return LLMChoice(provider="openai", model=value).model


class ModelBindings(Contract):
    quick: ModelBinding
    deep: ModelBinding


class StartAnalysis(Contract):
    request: AnalysisRequest
    auth_type: Literal["api_key", "personal_chatgpt"] = "api_key"
    models: ModelBindings | None = None
    quick_model: str | None = None
    deep_model: str | None = None

    @field_validator("quick_model", "deep_model")
    @classmethod
    def valid_model(cls, value):
        return LLMChoice(provider="openai", model=value).model if value is not None else None

    @model_validator(mode="after")
    def credential_contract(self):
        if self.auth_type == "api_key":
            valid = self.models is not None and self.quick_model is None and self.deep_model is None
        else:
            valid = self.models is None and self.quick_model is not None and self.deep_model is not None
        if not valid:
            raise ValueError("INVALID_CONNECTION_SELECTION")
        return self


class BackendSettings(Contract):
    database_url: SecretStr
    supabase_url: str
    supabase_publishable_key: SecretStr
    supabase_service_key: SecretStr
    encryption_keys: dict[str, SecretStr]
    active_encryption_key: str
    # Operator-curated API-key models. Personal ChatGPT models are discovered
    # by the trusted worker at runtime; no request-supplied URLs.
    allowed_models: dict[str, tuple[str, ...]]
    hard_timeout_seconds: int = Field(default=2100, ge=300, le=3600)
    lease_seconds: int = Field(default=120, ge=30, le=300)
    daily_analysis_limit: int = Field(default=10, ge=1, le=100)
    daily_connection_limit: int = Field(default=20, ge=1, le=100)
    allow_local_http: bool = False
    # Accepted for compatibility with existing settings. No longer used for access.
    allowed_user_ids: tuple[UUID, ...] = ()
    # This release intentionally keeps a single globally active analysis.
    # Queuing/per-user concurrency is a separate rollout.
    personal_user_id: UUID | None = None
    personal_chatgpt_enabled: bool = False
    personal_chatgpt_auth_file: Path | None = None

    def require_personal_owner(self, user_id: UUID):
        from ..errors import AnalysisServiceError

        if not self.personal_chatgpt_enabled or user_id != self.personal_user_id:
            raise AnalysisServiceError("ACCESS_DENIED")

    @property
    def max_active_analyses(self):
        return 1

    @model_validator(mode="after")
    def validate_trusted_settings(self):
        from psycopg.conninfo import conninfo_to_dict

        database = conninfo_to_dict(self.database_url.get_secret_value())
        if (
            database.get("host") not in ("127.0.0.1", "localhost", "::1", "host.docker.internal")
            and database.get("sslmode") != "verify-full"
        ):
            raise ValueError("DATABASE_VERIFIED_TLS_REQUIRED")
        url = urlsplit(self.supabase_url)
        local = url.hostname in ("127.0.0.1", "localhost", "host.docker.internal")
        if url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            raise ValueError("INVALID_SUPABASE_URL")
        if url.scheme != "https" and not (self.allow_local_http and local and url.scheme == "http"):
            raise ValueError("HTTPS_REQUIRED")
        if self.active_encryption_key not in self.encryption_keys:
            raise ValueError("ENCRYPTION_KEY_MISSING")
        for key_id, value in self.encryption_keys.items():
            if (
                not key_id.isalnum()
                or len(base64.b64decode(value.get_secret_value(), validate=True)) != 32
            ):
                raise ValueError("INVALID_ENCRYPTION_KEY")
        supported = set(get_args(APIProvider))
        if self.personal_chatgpt_enabled:
            if (
                self.personal_user_id is None
                or self.personal_chatgpt_auth_file is None
                or not self.personal_chatgpt_auth_file.is_absolute()
            ):
                raise ValueError("PERSONAL_CHATGPT_CONFIGURATION_REQUIRED")
            supported.add("chatgpt")
        elif self.personal_chatgpt_auth_file is not None:
            raise ValueError("PERSONAL_CHATGPT_DISABLED")
        if not self.allowed_models or not set(self.allowed_models) <= supported:
            raise ValueError("UNSUPPORTED_HOSTED_PROVIDER")
        for models in self.allowed_models.values():
            if not models:
                raise ValueError("EMPTY_MODEL_ALLOWLIST")
            for model in models:
                LLMChoice(provider="openai", model=model)
        return self
