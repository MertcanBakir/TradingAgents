"""Versioned service contracts; client requests cannot supply credentials or paths."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

Analyst = Literal["market", "social", "news", "fundamentals"]
Rating = Literal["Buy", "Overweight", "Hold", "Underweight", "Sell", "REVIEW"]
Provider = Literal[
    "chatgpt", "openai", "anthropic", "google", "xai", "deepseek", "qwen", "qwen-cn",
    "glm", "glm-cn", "minimax", "minimax-cn", "openrouter", "mistral", "kimi", "groq", "nvidia",
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def validate_ticker(value: str) -> str:
    value = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.^=-]{0,31}", value):
        raise ValueError("INVALID_TICKER")
    if ".." in value:
        raise ValueError("INVALID_TICKER")
    return value


class AnalysisRequest(Contract):
    ticker: str
    analysis_date: date
    profile: Literal["standard"] = "standard"
    language: Literal["tr", "en"] = "tr"
    analysts: tuple[Analyst, ...] = ("market", "social", "news", "fundamentals")

    _ticker = field_validator("ticker")(validate_ticker)

    @field_validator("analysis_date", mode="before")
    @classmethod
    def iso_date(cls, value):
        if isinstance(value, str) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("INVALID_ANALYSIS_DATE")
        if isinstance(value, (datetime, int, float, bool)):
            raise ValueError("INVALID_ANALYSIS_DATE")
        return value

    @field_validator("analysis_date")
    @classmethod
    def no_future_date(cls, value):
        if value > datetime.now(UTC).date():
            raise ValueError("FUTURE_ANALYSIS_DATE")
        return value

    @field_validator("analysts")
    @classmethod
    def unique_analysts(cls, value):
        if not value or len(set(value)) != len(value):
            raise ValueError("INVALID_ANALYST_SELECTION")
        return value


class LLMChoice(Contract):
    provider: Provider
    model: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")

    @field_validator("model")
    @classmethod
    def no_endpoint(cls, value):
        if "://" in value or ".." in value:
            raise ValueError("INVALID_MODEL_ID")
        return value


class ServiceSettings(Contract):
    """Trusted server settings, never merged from a mobile request."""

    quick: LLMChoice
    deep: LLMChoice
    llm_max_retries: int = Field(default=1, ge=0, le=2, strict=True)
    max_tool_rounds: int = Field(default=12, ge=1, le=20, strict=True)
    memory_context_chars: int = Field(default=8000, ge=500, le=16000, strict=True)
    evaluation_trading_days: int = Field(default=5, ge=1, le=252, strict=True)
    benchmark: str = "SPY"

    _benchmark = field_validator("benchmark")(validate_ticker)


class EffectiveConfig(Contract):
    quick: LLMChoice
    deep: LLMChoice
    research_rounds: Literal[1] = 1
    risk_rounds: Literal[1] = 1
    analysts: tuple[Analyst, ...]
    language: Literal["tr", "en"]


SectionCode = Literal[
    "executive_summary",
    "technical",
    "fundamentals",
    "news",
    "sentiment",
    "bull",
    "bear",
    "trade_plan",
    "risk",
    "final_assessment",
    "provenance",
    "disclaimer",
]


class ReportSection(Contract):
    code: SectionCode
    title: str
    content_markdown: str | None = None
    parts: dict[str, str] = Field(default_factory=dict)


class AnalysisResult(Contract):
    schema_version: Literal["1"] = "1"
    analysis_id: UUID
    ticker: str
    analysis_date: date
    final_rating: Rating
    trade_action: Literal["Buy", "Hold", "Sell"] | None
    quality: Literal["complete", "partial", "needs_review"]
    sections: tuple[ReportSection, ...]
    effective_config: EffectiveConfig
    upstream_version: str
    started_at: datetime
    finished_at: datetime
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    warning_codes: tuple[str, ...] = ()
    memory_saved: bool = False
