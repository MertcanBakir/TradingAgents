"""Map real graph fields to twelve cards without another LLM or invented text."""

from __future__ import annotations

import json
import re
from datetime import datetime
from uuid import UUID

from tradingagents import __version__
from tradingagents.agents.rating import RATINGS_5_TIER, parse_rating

from .config import effective_config
from .errors import AnalysisServiceError, redact_text
from .models import AnalysisRequest, AnalysisResult, ReportSection, ServiceSettings

TITLES = {
    "tr": (
        "Yönetici özeti",
        "Teknik analiz",
        "Temel analiz",
        "Haber analizi",
        "Piyasa duyarlılığı",
        "Boğa görüşü",
        "Ayı görüşü",
        "İşlem planı",
        "Risk değerlendirmesi",
        "Nihai değerlendirme",
        "Kullanılan veri ve modeller",
        "Yasal uyarı",
    ),
    "en": (
        "Executive summary",
        "Technical analysis",
        "Fundamentals",
        "News",
        "Sentiment",
        "Bull case",
        "Bear case",
        "Trade plan",
        "Risk assessment",
        "Final assessment",
        "Data and models",
        "Disclaimer",
    ),
}
DISCLAIMER = {
    "tr": "Bu içerik yapay zekâ tarafından üretilmiştir; hata veya eksik veri içerebilir. "
    "Kişiye özel yatırım tavsiyesi değildir. Geçmiş sonuçlar gelecek performansı garanti etmez.",
    "en": "AI-generated research may contain errors or missing data. It is not personalized "
    "investment advice. Past outcomes do not guarantee future performance.",
}


def _text(value) -> str | None:
    return redact_text(value.strip()) if isinstance(value, str) and value.strip() else None


def _summary(decision: str) -> str | None:
    # Headers are rendered by the existing PortfolioDecision schema. Missing
    # or free-form summaries remain missing; we never fabricate a replacement.
    match = re.search(
        r"(?ims)^\s*\*\*Executive Summary\*\*\s*:\s*(.*?)"
        r"(?=^\s*\*\*[^\n]+\*\*\s*:|\Z)",
        decision,
    )
    return _text(match.group(1)) if match else None


def normalize_result(
    *,
    state: dict,
    request: AnalysisRequest,
    settings: ServiceSettings,
    analysis_id: UUID,
    started_at: datetime,
    finished_at: datetime,
    duration_seconds: float,
    data_sources: dict | None = None,
) -> AnalysisResult:
    decision = _text(state.get("final_trade_decision"))
    if not decision:
        raise AnalysisServiceError("MISSING_FINAL_DECISION")
    if (
        state.get("company_of_interest") != request.ticker
        or str(state.get("trade_date")) != request.analysis_date.isoformat()
    ):
        raise AnalysisServiceError("RESULT_IDENTITY_MISMATCH")
    explicit = state.get("final_rating")
    parsed = parse_rating(decision)
    rating = explicit if explicit in RATINGS_5_TIER else parsed
    warnings = []
    if explicit not in (None, "REVIEW", *RATINGS_5_TIER):
        warnings.append("INVALID_EXPLICIT_RATING")
        rating = "REVIEW"
    if explicit == "REVIEW" or (explicit in RATINGS_5_TIER and parsed not in (explicit, "REVIEW")):
        rating = "REVIEW"
        warnings.append("RATING_REQUIRES_REVIEW")
    trade = _text(state.get("trader_investment_plan"))
    action_match = re.search(
        r"(?im)^\s*(?:[-*]\s+)?Action\s*:\s*(Buy|Hold|Sell)\b", (trade or "").replace("**", "")
    )
    action = action_match.group(1).title() if action_match else None
    if action is None:
        warnings.append("TRADE_ACTION_UNPARSEABLE")
    debate = state.get("investment_debate_state") or {}
    risk = state.get("risk_debate_state") or {}
    risk_parts = {
        key: text
        for key in ("aggressive", "conservative", "neutral")
        if (text := _text(risk.get(f"{key}_history")))
    }
    config = effective_config(request, settings)
    provenance = json.dumps(
        {
            "tradingagents_version": __version__,
            "analysis_date": request.analysis_date.isoformat(),
            "configuration": config.model_dump(mode="json"),
            "configured_data_sources": {
                key: value
                for key, value in (data_sources or {}).items()
                if isinstance(key, str)
                and re.fullmatch(r"[a-z_]{1,80}", key)
                and isinstance(value, str)
                and re.fullmatch(r"[a-z_, -]{1,120}", value)
            },
            "data_freshness": "not_independently_verified",
        },
        ensure_ascii=False,
    )
    values = [
        ("executive_summary", _summary(decision), {}),
        ("technical", _text(state.get("market_report")), {}),
        ("fundamentals", _text(state.get("fundamentals_report")), {}),
        ("news", _text(state.get("news_report")), {}),
        ("sentiment", _text(state.get("sentiment_report")), {}),
        ("bull", _text(debate.get("bull_history")), {}),
        ("bear", _text(debate.get("bear_history")), {}),
        (
            "trade_plan",
            trade,
            {"research_manager": text} if (text := _text(state.get("investment_plan"))) else {},
        ),
        ("risk", None, risk_parts),
        ("final_assessment", decision, {}),
        ("provenance", redact_text(provenance), {}),
        ("disclaimer", DISCLAIMER[request.language], {}),
    ]
    sections = tuple(
        ReportSection(code=code, title=title, content_markdown=text, parts=parts)
        for (code, text, parts), title in zip(values, TITLES[request.language], strict=True)
    )
    requested = {
        "market": "technical",
        "fundamentals": "fundamentals",
        "news": "news",
        "social": "sentiment",
    }
    required = {requested[key] for key in request.analysts} | {
        "bull",
        "bear",
        "trade_plan",
        "executive_summary",
    }
    for section in sections:
        if section.code in required and section.content_markdown is None:
            warnings.append(f"MISSING_SECTION_{section.code.upper()}")
    if len(risk_parts) != 3:
        warnings.append("INCOMPLETE_RISK_ASSESSMENT")
    if not state.get("investment_plan"):
        warnings.append("MISSING_RESEARCH_PLAN")
    quality = (
        "needs_review"
        if rating == "REVIEW" or action is None
        else "partial"
        if warnings
        else "complete"
    )
    return AnalysisResult(
        analysis_id=analysis_id,
        ticker=request.ticker,
        analysis_date=request.analysis_date,
        final_rating=rating,
        trade_action=action,
        quality=quality,
        sections=sections,
        effective_config=config,
        upstream_version=__version__,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=max(0, duration_seconds),
        warning_codes=tuple(warnings),
    )
