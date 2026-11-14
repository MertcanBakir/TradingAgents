"""Shared analysis horizons and their technical and fundamental data windows."""

from typing import Literal

AnalysisHorizon = Literal["short_1m", "medium_3m", "long_12m"]

HORIZON_LABELS = {
    "short_1m": {"en": "Short (1 month)", "tr": "Kısa (1 ay)"},
    "medium_3m": {"en": "Medium (3 months)", "tr": "Orta (3 ay)"},
    "long_12m": {"en": "Long (12 months)", "tr": "Uzun (12 ay)"},
}

HORIZON_CONFIG = {
    "short_1m": {
        "required_groups": ("ema", "rsi", "macd", "atr", "rvol"),
        "target_days": 21,
        "history_rows": 180,
        "fundamentals_lookback_months": 7,
        "primary_timeframe": "1d",
        "sma": (20, 50),
        "ema": (10, 20),
        "macd": (8, 21, 5),
        "rsi": 14,
        "bollinger": (20, 2),
        "atr": 14,
        "vwma": 20,
        "mfi": 14,
        "roc": (5, 20),
        "rvol": 20,
        "adx": 14,
        "zone_lookback": 63,
        "obv_lookback": 20,
    },
    "medium_3m": {
        "required_groups": ("sma", "ema", "macd", "adx", "rvol", "atr"),
        "target_days": 63,
        "history_rows": 350,
        "fundamentals_lookback_months": 13,
        "primary_timeframe": "1d",
        "sma": (50, 200),
        "ema": (20, 50),
        "macd": (12, 26, 9),
        "rsi": 14,
        "bollinger": (20, 2),
        "atr": 14,
        "vwma": 50,
        "mfi": 14,
        "roc": (20, 63),
        "rvol": 20,
        "adx": 14,
        "zone_lookback": 126,
        "obv_lookback": 63,
    },
    "long_12m": {
        "required_groups": ("sma", "roc", "macd", "atr", "adx"),
        "target_days": 252,
        "history_rows": 1000,
        "fundamentals_lookback_months": 13,
        "primary_timeframe": "1d",
        "sma": (50, 200),
        "ema": (50, 100),
        "macd": (24, 52, 18),
        "rsi": 28,
        "bollinger": (50, 2),
        "atr": 21,
        "vwma": 50,
        "mfi": 28,
        "roc": (63, 252),
        "rvol": 63,
        "adx": 21,
        "zone_lookback": 252,
        "obv_lookback": 252,
    },
}


def parse_analysis_horizon(value: str | None) -> AnalysisHorizon:
    if not isinstance(value, str) or value not in HORIZON_CONFIG:
        raise ValueError(f"Choose an analysis horizon: {', '.join(HORIZON_CONFIG)}")
    return value


def horizon_context(horizon: AnalysisHorizon) -> str:
    target_days = HORIZON_CONFIG[horizon]["target_days"]
    return f"Analysis horizon: {HORIZON_LABELS[horizon]['en']} (target: {target_days} trading days)."


def horizon_label(horizon: AnalysisHorizon, language: str) -> str:
    language = language.strip().casefold()
    return HORIZON_LABELS[horizon]["tr" if language in {"tr", "turkish", "türkçe"} else "en"]
