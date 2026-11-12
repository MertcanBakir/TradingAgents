"""Technical indicator definitions, calculations and result validation."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

import pandas as pd
from stockstats import wrap

from tradingagents.dataflows.vendors.yahoo.snapshot import _fmt

logger = logging.getLogger(__name__)


def _wilder_average(values, period):
    """Seed with a full-window mean and restart after missing input."""
    average = pd.Series(index=values.index, dtype=float)
    for _, segment in values.dropna().groupby(values.isna().cumsum()):
        if len(segment) < period:
            continue
        seeded = segment.iloc[period - 1:].astype(float).copy()
        seeded.iloc[0] = segment.iloc[:period].mean()
        average.loc[seeded.index] = seeded.ewm(alpha=1 / period, adjust=False).mean()
    return average


def _adx(rows, period):
    high, low, close = rows["High"], rows["Low"], rows["Close"]
    up, down = high.diff(), -low.diff()
    valid = up.notna() & down.notna() & close.notna() & close.shift().notna()
    plus = _wilder_average(up.where((up > down) & (up > 0), 0).where(valid), period)
    minus = _wilder_average(down.where((down > up) & (down > 0), 0).where(valid), period)
    # True range cancels in DX = 100 * abs(+DI - -DI) / (+DI + -DI).
    total = plus + minus
    dx = (100 * (plus - minus).abs() / total.where(total.ne(0))).mask(total.eq(0), 0)
    return _wilder_average(dx, period)


def _valid_price_range(rows):
    prices = rows[["High", "Low", "Close"]]
    return prices.notna().all(axis=1) & prices.abs().lt(float("inf")).all(axis=1) & rows["High"].ge(rows["Low"])


def _atr(rows, period):
    high, low, close = rows["High"], rows["Low"], rows["Close"]
    previous = close.shift()
    previous.iloc[0] = close.iloc[0]
    true_range = pd.concat([high - low, (high - previous).abs(), (low - previous).abs()], axis=1)
    ranges = true_range.max(axis=1, skipna=False).where(
        _valid_price_range(rows) & previous.abs().lt(float("inf"))
    )
    # Match the existing smoothed average on complete data and restart it
    # after a gap, instead of counting a missing range as a quiet session.
    return ranges.groupby(ranges.isna().cumsum()).transform(
        lambda segment: segment.ewm(alpha=1 / period, adjust=True, min_periods=period).mean()
    ).where(ranges.notna())


def _valid_price_volume_window(rows, period, *, extra_price_rows=0):
    valid_ranges = _valid_price_range(rows).rolling(period + extra_price_rows).min().eq(1)
    return valid_ranges & _positive_volume_window(rows, period)


def _roc(rows, period):
    close = rows["Close"]
    previous = close.shift(period)
    return 100 * (close / previous.where(previous.ne(0)) - 1)


def _valid_volume(rows):
    volume = rows["Volume"]
    return volume.where(volume.ge(0) & volume.lt(float("inf")))


def _positive_volume_window(rows, period):
    return _valid_volume(rows).rolling(period).sum().gt(0)


def _rvol(rows, period):
    volume = _valid_volume(rows)
    average = volume.shift().rolling(period).mean()
    return volume / average.where(average.gt(0))


def _signed_volume(rows):
    change = rows["Close"].diff()
    direction = change.gt(0).astype(int) - change.lt(0).astype(int)
    return (direction * _valid_volume(rows)).where(change.notna())


def _obv(rows):
    signed_volume = _signed_volume(rows)
    if pd.notna(rows["Close"].iloc[0]) and pd.notna(_valid_volume(rows).iloc[0]):
        signed_volume.iloc[0] = 0
    return signed_volume.cumsum(skipna=False)


def _obv_change(rows, period):
    # A complete recent window can be measured even if older volume is missing.
    return _signed_volume(rows).rolling(period).sum()


def _bollinger_band(rows, period, multiplier):
    window = rows["Close"].rolling(period)
    return window.mean() + multiplier * window.std()


@dataclass(frozen=True)
class IndicatorSpec:
    name: str
    minimum_rows: int = 1
    calculate: Callable[[pd.DataFrame], pd.Series] | None = None
    label: str | None = None
    scale: float = 1.0
    valid_rows: Callable[[pd.DataFrame], pd.Series] | None = None
    derived_metrics: Callable[[str, pd.Series, float, int], list[str]] | None = None

    @property
    def display_label(self) -> str:
        return f"{self.name} ({self.label})" if self.label else self.name


def profile_indicators(profile) -> dict[str, list[IndicatorSpec]]:
    """Build the indicator definitions for one horizon. Stockstats handles default calculations."""
    groups = {
        family: [
            IndicatorSpec(f"close_{period}_{family}", period, derived_metrics=_moving_average_metrics)
            for period in profile[family]
        ]
        for family in ("sma", "ema")
    }

    fast, slow, signal = profile["macd"]
    groups["macd"] = [
        IndicatorSpec(f"{component}_{fast},{slow},{signal}", warmup)
        for component, warmup in (("macd", slow), ("macds", slow + signal - 1), ("macdh", slow + signal - 1))
    ]
    rsi, atr, vwma, mfi = (profile[group] for group in ("rsi", "atr", "vwma", "mfi"))
    groups["rsi"] = [IndicatorSpec(f"rsi_{rsi}", rsi + 1)]
    groups["atr"] = [IndicatorSpec(
        f"atr_{atr}", atr, calculate=partial(_atr, period=atr), derived_metrics=_atr_metrics,
    )]
    groups["vwma"] = [IndicatorSpec(
        f"vwma_{vwma}", vwma, valid_rows=partial(_valid_price_volume_window, period=vwma),
        derived_metrics=_moving_average_metrics,
    )]
    groups["mfi"] = [IndicatorSpec(
        f"mfi_{mfi}", mfi + 1, label="0–100 scale", scale=100,
        valid_rows=partial(_valid_price_volume_window, period=mfi, extra_price_rows=1),
    )]

    period, deviations = profile["bollinger"]
    groups["bollinger"] = [
        IndicatorSpec(f"bollinger_{band}_{period}_{deviations}",
                      calculate=partial(_bollinger_band, period=period, multiplier=multiplier))
        for band, multiplier in (("middle", 0), ("upper", deviations), ("lower", -deviations))
    ]
    groups["roc"] = [
        IndicatorSpec(f"roc_{period}", calculate=partial(_roc, period=period),
                      label=f"% close change over {period} bars")
        for period in profile["roc"]
    ]
    for group, calculator, label in (
        ("rvol", _rvol, "current volume divided by prior average"),
        ("adx", _adx, "Wilder trend strength, 0–100, no direction"),
    ):
        period = profile[group]
        groups[group] = [IndicatorSpec(f"{group}_{period}", calculate=partial(calculator, period=period), label=label)]
    period = profile["obv_lookback"]
    groups["obv"] = [
        IndicatorSpec("obv", calculate=_obv, label="cumulative signed volume"),
        IndicatorSpec(f"obv_change_{period}", calculate=partial(_obv_change, period=period),
                      label=f"net change over {period} bars"),
    ]
    return groups


def _percentage_change(current, reference):
    if pd.isna(current) or pd.isna(reference) or reference <= 0:
        return None
    return 100 * (current / reference - 1)


def _atr_metrics(name, values, close, _slope_bars):
    atr_pct = 100 * values.iloc[-1] / close if pd.notna(close) and close > 0 else None
    return [f"{name} ATR %={_fmt(atr_pct)}"]


def _moving_average_metrics(name, values, close, slope_bars):
    latest = values.iloc[-1]
    reference = values.shift(slope_bars).iloc[-1]
    return [
        f"{name} Price-to-MA %={_fmt(_percentage_change(close, latest))}",
        f"{name} MA Slope % ({slope_bars} bars)={_fmt(_percentage_change(latest, reference))}",
    ]


def trend_comparisons(groups, calculated, dates):
    evidence = []
    for family in ("sma", "ema"):
        if (pair := groups.get(family)) and all(spec.name in calculated for spec in pair):
            fast, slow = (spec.name for spec in pair)
            gap = _percentage_change(calculated[fast].iloc[-1], calculated[slow].iloc[-1])
            if gap is not None:
                evidence.append(f"{fast} vs {slow} ({_fmt(dates.iloc[-1])}) %={_fmt(gap)}")

    if (adx_group := groups.get("adx")) and (adx := calculated.get(adx_group[0].name)) is not None:
        previous = adx.shift(5).iloc[-1]
        latest = adx.iloc[-1]
        if pd.notna(previous) and pd.notna(latest):
            evidence.append(
                f"{adx_group[0].name} strength change over 5 bars: "
                f"{_fmt(dates.iloc[-6])}={_fmt(previous)}, {_fmt(dates.iloc[-1])}={_fmt(latest)}, "
                f"change={_fmt(latest - previous)} points"
            )

    if (macd_group := groups.get("macd")) and (histogram := calculated.get(macd_group[2].name)) is not None:
        recent = histogram.tail(3)
        if len(recent) == 3 and recent.notna().all():
            magnitude = recent.abs()
            first, second, third = magnitude
            if first < second < third:
                trend = "expanding"
            elif first > second > third:
                trend = "contracting"
            else:
                trend = "not steadily expanding or contracting"
            samples = ", ".join(f"{_fmt(date)}={_fmt(value)}" for date, value in zip(dates.tail(3), recent, strict=True))
            evidence.append(f"{macd_group[2].name} last 3 bars: {samples}; absolute magnitude {trend}")
    return evidence


@dataclass(frozen=True)
class IndicatorResult:
    values: pd.Series
    metrics: list[str]


def calculate_indicators(rows, groups, slope_bars) -> dict[str, IndicatorResult]:
    # Carry each candle's date through both calculation paths so a shifted or
    # reordered result can be rejected instead of relabelled by position.
    rows = rows.set_index("Date", drop=False)
    missing = {column: float("nan") for column in ("High", "Low", "Volume") if column not in rows}
    if missing:
        rows = rows.assign(**missing)
    # Stockstats uses cumulative sums internally. Neutral placeholders prevent
    # gaps poisoning later windows. Specs mask affected windows using raw rows.
    valid_ranges = _valid_price_range(rows)
    stock_rows = rows.assign(
        Volume=_valid_volume(rows).fillna(0),
        High=rows["High"].where(valid_ranges, rows["Close"]),
        Low=rows["Low"].where(valid_ranges, rows["Close"]),
    )
    data = wrap(stock_rows)
    close = rows["Close"].iloc[-1]
    calculated = {}
    for specs in groups.values():
        for spec in specs:
            try:
                values = spec.calculate(rows) if spec.calculate else data[spec.name]
                if not isinstance(values, pd.Series) or not values.index.equals(rows.index):
                    raise ValueError("Indicator output must contain one value per candle in date order")
                values = (values.astype(float) * spec.scale).replace([float("inf"), float("-inf")], float("nan"))
                if spec.valid_rows:
                    values = values.where(spec.valid_rows(rows))
                values.iloc[:spec.minimum_rows - 1] = float("nan")
                metrics = spec.derived_metrics(spec.name, values, close, slope_bars) if spec.derived_metrics else []
            except (KeyError, ValueError, TypeError, ArithmeticError):
                logger.warning("Could not calculate market indicator %s", spec.name, exc_info=True)
                continue
            calculated[spec.name] = IndicatorResult(values, metrics)
    return calculated
