"""Build an isolated, bounded standard-profile graph config."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .models import AnalysisRequest, EffectiveConfig, ServiceSettings


def effective_config(request: AnalysisRequest, settings: ServiceSettings) -> EffectiveConfig:
    return EffectiveConfig(
        quick=settings.quick,
        deep=settings.deep,
        analysts=request.analysts,
        language=request.language,
    )


def build_graph_config(request: AnalysisRequest, settings: ServiceSettings, workdir: Path) -> dict:
    from tradingagents.default_config import DEFAULT_CONFIG

    config = deepcopy(DEFAULT_CONFIG)
    # Do not inherit endpoint, local memory or unbounded generation settings from
    # a developer's CLI environment. No request-level arbitrary config merging.
    config.update(
        llm_provider=settings.quick.provider,
        quick_think_provider=settings.quick.provider,
        quick_think_llm=settings.quick.model,
        deep_think_provider=settings.deep.provider,
        deep_think_llm=settings.deep.model,
        backend_url=None,
        quick_think_backend_url=None,
        deep_think_backend_url=None,
        max_debate_rounds=1,
        max_risk_discuss_rounds=1,
        max_tool_rounds=settings.max_tool_rounds,
        max_recur_limit=100,
        llm_max_retries=settings.llm_max_retries,
        max_tokens=None,
        temperature=None,
        openai_reasoning_effort=None,
        google_thinking_level=None,
        anthropic_effort=None,
        checkpoint_enabled=False,
        memory_log_path=None,
        results_dir=str(workdir / "results"),
        data_cache_dir=str(workdir / "cache"),
        output_language={"tr": "Turkish", "en": "English"}[request.language],
    )
    return config
