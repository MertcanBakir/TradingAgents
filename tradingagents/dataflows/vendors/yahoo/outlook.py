"""Dated observations of Yahoo's live analyst estimates, earnings calendar and price targets."""

import json
import logging
import math
from datetime import date, timedelta
from pathlib import Path

import yfinance as yf

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.symbols import normalize_symbol, safe_ticker_component
from tradingagents.dataflows.vendors.yahoo.common import yf_retry
from tradingagents.dataflows.vendors.yahoo.fundamentals import _MAIN_UNIT

logger = logging.getLogger(__name__)


def _date(value):
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        return None
    return value


def _change(current, previous):
    result = {"change": round(current - previous, 8)}
    if previous > 0:
        result["change_pct"] = round((current - previous) / previous * 100, 2)
    return result


def _read_snapshot(folder, day):
    try:
        data = json.loads((folder / f"{day}.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("observed_on") == day else None
    except (OSError, ValueError):
        return None


def _observation(ticker, as_of_date, name, fetch):
    symbol = safe_ticker_component(normalize_symbol(ticker))
    today = get_current_date()
    day = date.fromisoformat(as_of_date or today).isoformat()
    folder = Path(get_config()["data_cache_dir"]) / "yahoo_outlook" / symbol / name
    if day < today:
        return _read_snapshot(folder, day) or {
            "source": "Yahoo Finance", "unavailable": f"No archived observation for {day}."
        }

    try:
        data = fetch(symbol, day)
    except VendorUnavailableError:
        cached = _read_snapshot(folder, today)
        if cached:
            return cached
        raise
    if not data:
        return _read_snapshot(folder, today) or {
            "source": "Yahoo Finance", "unavailable": "No usable data returned."
        }
    observation = {"source": "Yahoo Finance", "observed_on": today, **data}
    try:
        folder.mkdir(parents=True, exist_ok=True)
        replace_file(
            folder / f"{today}.json",
            lambda path: Path(path).write_text(json.dumps(observation, allow_nan=False), encoding="utf-8"),
        )
    except OSError:
        logger.warning("Could not archive Yahoo %s for %s", name, symbol)
    return observation


def _estimates(symbol, _day):
    def fetch():
        ticker = yf.Ticker(symbol)
        frames = {
            "eps": ticker.get_earnings_estimate(),
            "revenue": ticker.get_revenue_estimate(),
            "eps_trend": ticker.get_eps_trend(),
            "eps_revisions": ticker.get_eps_revisions(),
        }
        # The public frames omit the fiscal end date. Read it from the same
        # response already fetched by yfinance, if this version exposes it.
        trend = getattr(getattr(ticker, "_analysis", None), "_earnings_trend", None) or []
        end_dates = {row["period"]: _date(row.get("endDate")) for row in trend if "period" in row}
        return frames, end_dates

    frames, end_dates = yf_retry(fetch) or ({}, {})
    periods = []
    for period in ("0q", "+1q", "0y", "+1y"):
        row = {"period": period, "period_end": end_dates.get(period)}
        for section, frame in frames.items():
            if frame is None or period not in frame.index:
                continue
            fields = frame.loc[period].to_dict()
            clean = {
                key: value if key in {"currency", "methodology"} and isinstance(value, str) else _number(value)
                for key, value in fields.items()
            }
            if any(value is not None for value in clean.values()):
                row[section] = clean
        if len(row) > 2:
            eps_trend = row.get("eps_trend", {})
            current = eps_trend.get("current")
            if current is not None:
                row["eps_consensus_changes"] = {
                    f"{days}d": _change(current, previous)
                    for days in (7, 30) if (previous := eps_trend.get(f"{days}daysAgo")) is not None
                }
            periods.append(row)
    return {"periods": periods} if periods else {}


def get_estimate_revisions(ticker, as_of_date=None):
    data = _observation(ticker, as_of_date, "estimates", _estimates)
    if not data.get("periods"):
        return data

    symbol = safe_ticker_component(normalize_symbol(ticker))
    folder = Path(get_config()["data_cache_dir"]) / "yahoo_outlook" / symbol / "estimates"
    day = date.fromisoformat(data["observed_on"])
    for window in (7, 30):
        previous_dates = [(day - timedelta(days=window + lag)).isoformat() for lag in range(4)]
        observations = [_read_snapshot(folder, previous_day) for previous_day in previous_dates]
        earlier_periods = [
            (observation["observed_on"], earlier)
            for observation in observations if observation
            for earlier in observation.get("periods", [])
        ]
        for row in data["periods"]:
            revenue = row.get("revenue", {})
            currency, average = revenue.get("currency"), revenue.get("avg")
            if not row["period_end"] or not currency or average is None:
                continue
            for previous_day, earlier in earlier_periods:
                old_revenue = earlier.get("revenue", {})
                if (earlier["period_end"] == row["period_end"] and earlier["period"][-1] == row["period"][-1]
                        and old_revenue.get("currency") == currency and old_revenue.get("avg") is not None):
                    old_avg = old_revenue["avg"]
                    row.setdefault("revenue_revisions", {})[f"{window}d"] = {
                        "previous_observation": previous_day, "previous_average": old_avg,
                        **_change(average, old_avg),
                    }
                    break
    if not any(row.get("revenue_revisions") for row in data["periods"]):
        data["revenue_revision_note"] = "Comparable historical revenue estimates are not yet available."
    return data


def get_upcoming_earnings(ticker, as_of_date=None):
    def fetch(symbol, day):
        calendar = yf_retry(lambda: yf.Ticker(symbol).get_calendar()) or {}
        dates = sorted({value for item in (calendar.get("Earnings Date") or [])
                        if (value := _date(item)) and value >= day})
        return {"dates": dates, "status": "estimated"} if dates else {}

    return _observation(ticker, as_of_date, "earnings_calendar", fetch)


def get_analyst_price_targets(ticker, as_of_date=None):
    def fetch(symbol, _day):
        ticker = yf.Ticker(symbol)
        raw = yf_retry(ticker.get_analyst_price_targets) or {}
        targets = {key: _number(raw.get(key)) for key in ("current", "low", "high", "mean", "median")}
        if not any(targets[key] is not None for key in ("low", "high", "mean", "median")):
            return {}
        try:
            currency = (yf_retry(ticker.get_info) or {}).get("currency")
        except VendorUnavailableError:
            currency = None
        targets["quote_currency"] = currency
        targets["target_currency"] = currency if currency not in _MAIN_UNIT else None
        current, median = targets["current"], targets["median"]
        if targets["target_currency"] and current and current > 0 and median is not None:
            targets["median_vs_reference_pct"] = round((median / current - 1) * 100, 2)
        return targets

    return _observation(ticker, as_of_date, "price_targets", fetch)
