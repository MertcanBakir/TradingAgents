"""Financial Modeling Prep's published industry P/E benchmark."""

import math
import os

import requests

from tradingagents.dataflows.date_window import get_current_date, is_historical
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)

_BASE = "https://financialmodelingprep.com/stable"


def _rows(endpoint, key, **params):
    try:
        response = requests.get(
            f"{_BASE}/{endpoint}", params=params, headers={"apikey": key},
            timeout=10, allow_redirects=False,
        )
        if response.status_code != 200:
            raise VendorUnavailableError(f"FMP {endpoint} returned HTTP {response.status_code}")
        rows = response.json()
    except (requests.RequestException, ValueError):
        raise VendorUnavailableError(f"FMP {endpoint} request failed") from None
    if not isinstance(rows, list):
        raise VendorUnavailableError(f"FMP {endpoint} returned an unexpected response")
    return rows


def get_industry_pe(ticker, as_of_date=None):
    day = as_of_date or get_current_date()
    if is_historical(day):
        # The profile supplies today's classification, which may have changed.
        raise NoMarketDataError(ticker, detail=f"FMP classification is not verified as of {day}")
    key = os.getenv("FMP_API_KEY", "").strip()
    if not key:
        raise VendorNotConfiguredError("FMP_API_KEY is not configured")
    symbol = ticker.strip().upper()
    profiles = _rows("profile", key, symbol=symbol)
    profile = next((row for row in profiles if isinstance(row, dict) and row.get("symbol") == symbol), {})
    industry, exchange = profile.get("industry"), profile.get("exchange")
    if not industry or not exchange:
        raise NoMarketDataError(ticker, detail="FMP industry or exchange is unavailable")

    rows = _rows("industry-pe-snapshot", key, date=day, industry=industry, exchange=exchange)
    for row in rows:
        if (not isinstance(row, dict) or row.get("date") != day or row.get("industry") != industry
                or row.get("exchange") != exchange):
            continue
        value = row.get("pe")
        if type(value) in (int, float) and math.isfinite(value) and value > 0:
            return {
                "source": "Financial Modeling Prep", "date": day, "industry": industry,
                "exchange": exchange, "aggregate_pe": value,
            }
    raise NoMarketDataError(ticker, detail=f"No usable FMP industry P/E for {industry}, {exchange} on {day}")
