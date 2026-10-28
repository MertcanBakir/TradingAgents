"""Reuse upstream hosted providers; never accept arbitrary backend URLs."""

from __future__ import annotations

import httpx

from ..errors import AnalysisServiceError
from .contracts import APIProvider


def endpoint(provider: str) -> str:
    if provider == "openai":
        return "https://api.openai.com/v1"
    if provider == "google":
        return "https://generativelanguage.googleapis.com/v1beta"
    if provider == "anthropic":
        return "https://api.anthropic.com/v1"
    from typing import get_args

    from tradingagents.llm_clients.openai_client import OPENAI_COMPATIBLE_PROVIDERS

    if provider not in get_args(APIProvider):
        raise AnalysisServiceError("PROVIDER_NOT_AVAILABLE")
    return OPENAI_COMPATIBLE_PROVIDERS[provider].base_url


class ProviderProbe:
    """Validate key using a model-list request, never a paid completion.

    Some vendors do not expose /models: return a clear unsupported verification
    error rather than claim that an untested key works.
    """

    def __init__(self, client: httpx.Client):
        self.client = client

    def verify(self, provider: str, key: str) -> None:
        headers = {"Authorization": "Bearer " + key}
        if provider == "google":
            headers = {"x-goog-api-key": key}
        elif provider == "anthropic":
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        try:
            response = self.client.get(
                endpoint(provider).rstrip("/") + "/models",
                headers=headers,
                timeout=15,
                follow_redirects=False,
            )
            if response.status_code in (401, 403):
                raise AnalysisServiceError("PROVIDER_ACCESS_ERROR")
            if response.status_code in (404, 405):
                raise AnalysisServiceError("PROVIDER_VERIFICATION_UNSUPPORTED")
            if response.status_code != 200:
                raise AnalysisServiceError("PROVIDER_VERIFICATION_UNAVAILABLE", retryable=True)
        except AnalysisServiceError:
            raise
        except Exception:
            raise AnalysisServiceError(
                "PROVIDER_VERIFICATION_UNAVAILABLE", retryable=True
            ) from None
