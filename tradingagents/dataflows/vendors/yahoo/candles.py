"""Provenance and completion status for a daily Yahoo Finance bar."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from numbers import Real
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

PROVENANCE_COLUMN = "_yahoo_daily_provenance"


def _timestamp(value):
    if value is None or pd.isna(value):
        return None
    try:
        stamp = pd.Timestamp(value, unit="s", tz="UTC") if isinstance(value, Real) else pd.Timestamp(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return stamp.tz_convert("UTC") if stamp.tzinfo else None


def record_daily_provenance(rows, observed_at, metadata):
    """Save only the source fields needed to classify the daily bar."""
    regular = metadata.get("currentTradingPeriod") or {}
    regular = regular.get("regular", {}) if isinstance(regular, Mapping) else {}
    regular = regular if isinstance(regular, Mapping) else {}
    raw = {
        "observed_at": observed_at,
        "market_time": metadata.get("regularMarketTime"),
        "session_start": regular.get("start"),
        "session_end": regular.get("end"),
    }
    provenance = {key: stamp.isoformat() for key, value in raw.items() if (stamp := _timestamp(value))}
    timezone = metadata.get("exchangeTimezoneName") or getattr(rows.index, "tz", None)
    if timezone:
        provenance["exchange_timezone"] = str(timezone)
    return provenance


def attach_daily_provenance(data, provenance):
    # Put the metadata in the same atomically replaced CSV as the rows.
    data[PROVENANCE_COLUMN] = ""
    data.iloc[0, data.columns.get_loc(PROVENANCE_COLUMN)] = json.dumps(provenance)


def _read_daily_provenance(data):
    if PROVENANCE_COLUMN not in data:
        return {}
    raw = data[PROVENANCE_COLUMN].dropna()
    if raw.empty:
        return {}
    try:
        provenance = json.loads(raw.iloc[0])
    except (ValueError, TypeError):
        return {}
    return provenance if isinstance(provenance, dict) else {}


def restore_daily_provenance(data):
    provenance = _read_daily_provenance(data)
    if PROVENANCE_COLUMN in data:
        data.pop(PROVENANCE_COLUMN)
    if provenance:
        data.attrs["yahoo_daily_provenance"] = provenance


def _exchange_zone(metadata):
    try:
        return ZoneInfo(metadata.get("exchange_timezone") or "")
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return None


def cache_needs_closing_refresh(data, as_of_date, now):
    """Refresh an intraday snapshot once its requested exchange session ends."""
    metadata = _read_daily_provenance(data)
    observed = _timestamp(metadata.get("observed_at"))
    start = _timestamp(metadata.get("session_start"))
    end = _timestamp(metadata.get("session_end"))
    zone = _exchange_zone(metadata)
    return (
        observed is not None and start is not None and end is not None and zone is not None
        and start < end and start.tz_convert(zone).date() == as_of_date
        and observed < end <= now
    )


@dataclass(frozen=True)
class DailyCandle:
    date: str | None = None
    closed: bool | None = None
    as_of_timestamp: str | None = None
    timezone: str | None = None
    timestamp_basis: str | None = None


def latest_daily_candle(rows) -> DailyCandle:
    """Treat same-session data as final only with evidence available at fetch time."""
    if rows is None or rows.empty:
        return DailyCandle()
    bar_date = pd.Timestamp(rows["Date"].iloc[-1]).date()
    metadata = rows.attrs.get("yahoo_daily_provenance", {})
    observed = _timestamp(metadata.get("observed_at"))
    market_time = _timestamp(metadata.get("market_time"))
    session_start = _timestamp(metadata.get("session_start"))
    session_end = _timestamp(metadata.get("session_end"))
    zone = _exchange_zone(metadata)

    closed = None
    same_date_quote = (
        zone is not None and observed is not None and market_time is not None
        and market_time <= observed and market_time.tz_convert(zone).date() == bar_date
    )
    if zone is not None and observed is not None:
        if (
            session_start is not None and session_end is not None and session_start < session_end
            and session_start.tz_convert(zone).date() == bar_date
        ):
            if session_start <= observed < session_end:
                closed = False
            elif (
                (same_date_quote and market_time >= session_end)
                or observed.tz_convert(zone).date() > session_end.tz_convert(zone).date()
            ):
                closed = True
        elif observed.tz_convert(zone).date() > bar_date:
            # An earlier daily bar comes from a later vendor snapshot. If a
            # session spans midnight, its matching start/end above takes precedence.
            closed = True

    stamp = market_time if same_date_quote else observed
    stamp_zone = zone or ZoneInfo("UTC")
    return DailyCandle(
        date=bar_date.isoformat(),
        closed=closed,
        as_of_timestamp=stamp.tz_convert(stamp_zone).isoformat() if stamp is not None else None,
        timezone=str(stamp_zone) if stamp is not None else None,
        timestamp_basis=("vendor regular-market timestamp" if same_date_quote else "data fetch time")
        if stamp is not None else None,
    )
