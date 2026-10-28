"""Stable domain errors: raw provider exceptions never become caller messages."""

from __future__ import annotations

import os
import re


class AnalysisServiceError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def redact_text(text: str) -> str:
    """Defense in depth for report text, not a substitute for credential isolation."""
    for key, value in os.environ.items():
        if len(value) >= 8 and any(
            part in key.upper() for part in ("API_KEY", "TOKEN", "PASSWORD", "SECRET")
        ):
            text = text.replace(value, "[REDACTED]")
    text = re.sub(
        r"(?i)\b(?:postgres(?:ql)?|https?)://[^\s/@]+:[^\s/@]+@[^\s]+", "[REDACTED_URL]", text
    )
    text = re.sub(r"\b(?:sk-|ghp_|sb_secret_)[A-Za-z0-9_-]{8,}", "[REDACTED]", text)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._-]+", "Bearer [REDACTED]", text)
    return text


def classify_error(exc: Exception) -> AnalysisServiceError:
    name = type(exc).__name__
    if getattr(exc, "code", None) in {
        "insufficient_quota",
        "credit_balance_exhausted",
        "billing_hard_limit_reached",
    }:
        return AnalysisServiceError("PROVIDER_QUOTA_ERROR")
    if name in {"RateLimitError", "InternalServerError", "APIConnectionError", "APITimeoutError"}:
        return AnalysisServiceError("PROVIDER_TEMPORARY_ERROR", retryable=True)
    if name == "ChatGPTUnreachable":
        return AnalysisServiceError("CHATGPT_TEMPORARILY_UNAVAILABLE", retryable=True)
    if name == "ChatGPTAuthError":
        return AnalysisServiceError("CHATGPT_LOGIN_REQUIRED")
    if name in {"AuthenticationError", "PermissionDeniedError"}:
        return AnalysisServiceError("PROVIDER_ACCESS_ERROR")
    return AnalysisServiceError("ANALYSIS_EXECUTION_FAILED")
