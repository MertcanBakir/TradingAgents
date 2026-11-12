"""Support and resistance zones derived from completed price candles."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date
from typing import Literal

import pandas as pd


@dataclass(frozen=True)
class ZoneEvidence:
    period: int
    side: Literal["support", "resistance"]
    formation_date: date
    bounds: tuple[float, float]
    contact_count: int = 0


def _zone_from_pivot(rows, period, side, pivot):
    formation_date = pd.Timestamp(rows.at[pivot.index, "Date"]).date()
    lower, upper = pivot.bounds
    contact_count = 0
    inside = True  # Formation starts an episode but is not a later contact.
    for index in range(pivot.index + 1, len(rows)):
        low, high = rows.at[index, "Low"], rows.at[index, "High"]
        if pd.isna(low) or pd.isna(high):
            continue  # A missing range cannot establish that price left the zone.
        overlaps = low <= upper and high >= lower
        if overlaps and not inside:
            contact_count += 1
        inside = overlaps
    return ZoneEvidence(period, side, formation_date, (lower, upper), contact_count)


def _zone_bounds(center, atr):
    width = 0.25 * atr
    return float(center - width), float(center + width)


@dataclass(frozen=True)
class _Pivot:
    index: int
    center: float
    atr: float
    strength: float
    side: Literal["support", "resistance"]

    @property
    def bounds(self) -> tuple[float, float]:
        return _zone_bounds(self.center, self.atr)


def _scipy_pivots(window, atr):
    from scipy.signal import find_peaks

    for column, direction, side in (("High", 1, "resistance"), ("Low", -1, "support")):
        prices = window[column]
        valid = prices.notna() & prices.abs().lt(math.inf)
        # Treat missing prices as gaps rather than comparing across them.
        for _, segment in prices[valid].groupby((~valid).cumsum()):
            if len(segment) < 3:
                continue
            pivot_atr = atr.loc[segment.index]
            usable_atr = pivot_atr.where(pivot_atr.gt(0) & pivot_atr.lt(math.inf), math.inf)
            peaks, properties = find_peaks(
                direction * segment.to_numpy(dtype=float),
                prominence=(0.5 * usable_atr.to_numpy(dtype=float), None),
                distance=3,
            )
            for peak, prominence in zip(peaks, properties["prominences"], strict=True):
                yield _Pivot(
                    int(segment.index[peak]), float(segment.iloc[peak]), float(usable_atr.iloc[peak]),
                    float(prominence / usable_atr.iloc[peak]), side,
                )


def calculate_scipy_zones(rows, period, atr, *, per_side=3, recent_per_side=3) -> list[ZoneEvidence]:
    """Find local pivots, consolidate overlapping ATR zones and report nearby candidates."""
    if rows is None or len(rows) < 3 or atr is None or pd.isna(rows["Close"].iloc[-1]):
        return []
    rows = rows.reset_index(drop=True)
    atr = atr.reset_index(drop=True)
    window = rows.tail(period)
    last_close = rows["Close"].iloc[-1]

    # Keep the most prominent anchor in each overlap without widening its ATR zone.
    anchors = []
    pivots = sorted(_scipy_pivots(window, atr), key=lambda pivot: (-pivot.strength, pivot.index))
    first_by_bounds = {}
    for pivot in sorted(pivots, key=lambda pivot: pivot.index):
        first_by_bounds.setdefault(pivot.bounds, pivot)
    for pivot in pivots:
        lower, upper = pivot.bounds
        if any(lower <= other.bounds[1] and other.bounds[0] <= upper for other in anchors):
            continue
        first = first_by_bounds[pivot.bounds]
        anchors.append(replace(pivot, index=first.index, side=first.side))

    candidates = {"support": [], "resistance": []}
    for pivot in anchors:
        lower, upper = pivot.bounds
        if last_close < lower:
            side, distance = "resistance", lower - last_close
        elif last_close > upper:
            side, distance = "support", last_close - upper
        else:
            side, distance = pivot.side, 0
        candidates[side].append((distance, pivot))

    def by_distance(entry):
        return (entry[0], -entry[1].strength, -entry[1].index)

    evidence = []
    for side, nearby in candidates.items():
        nearby.sort(key=by_distance)
        # Retain recently formed areas that would be hidden behind older nearby zones.
        recent = sorted(nearby[per_side:], key=lambda entry: -entry[1].index)[:recent_per_side]
        chosen = sorted(nearby[:per_side] + recent, key=by_distance)
        evidence.extend(
            _zone_from_pivot(rows, len(window), side, pivot)
            for _, pivot in chosen
        )
    return evidence
