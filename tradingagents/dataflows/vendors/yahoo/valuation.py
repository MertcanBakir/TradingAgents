"""Yahoo's company P/E history and a sample of same-industry peers."""

from contextlib import suppress
from datetime import datetime
from statistics import mean, median

import yfinance as yf

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo.common import yf_retry
from tradingagents.dataflows.vendors.yahoo.outlook import _number, _observation

_MAX_PEERS = 12
_US_EXCHANGES = {"NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "BTS", "NASDAQ", "NYSE"}


def _positive(value):
    value = _number(value)
    return value if value is not None and value > 0 else None


def _is_us_listing(profile):
    return profile.get("market") == "us_market" or profile.get("exchange") in _US_EXCHANGES


def compare_pe_benchmarks(fundamentals, benchmarks):
    """Use the report's Yahoo trailing P/E as the numerator for both comparisons."""
    label = "PE Ratio (TTM): "
    current = None
    if isinstance(fundamentals, str):
        for line in fundamentals.splitlines():
            if line.startswith(label):
                with suppress(ValueError):
                    current = _positive(float(line[len(label):]))
                break

    result = dict(benchmarks)
    for section, reference, difference in (
        ("industry", "median_trailing_pe", "company_vs_peer_median_pct"),
        ("company_history", "mean_trailing_pe", "company_vs_history_mean_pct"),
    ):
        data = result.get(section)
        if not isinstance(data, dict):
            continue
        # Older cached results included a second current value and its comparison.
        result[section] = {key: value for key, value in data.items()
                           if key not in {"company_trailing_pe", difference}}
        baseline = _positive(data.get(reference))
        if current is not None and baseline is not None:
            result[section][difference] = round((current / baseline - 1) * 100, 2)
    return result


def get_industry_pe(ticker, as_of_date=None):
    def fetch(symbol, day):
        profile = yf_retry(lambda: yf.Ticker(symbol).get_info()) or {}
        industry = profile.get("industryKey")
        if not industry or not _is_us_listing(profile):
            return {}
        companies = yf_retry(lambda: yf.Industry(industry, region="US").top_companies)
        if companies is None:
            return {}
        peers = [peer for peer in companies.index.unique()
                 if isinstance(peer, str) and peer.upper() != symbol][:_MAX_PEERS]
        values = {}
        for peer in peers:
            try:
                info = yf_retry(lambda peer=peer: yf.Ticker(peer).get_info()) or {}
            except VendorUnavailableError:
                continue
            if (info.get("industryKey") == industry and _is_us_listing(info)
                    and (value := _positive(info.get("trailingPE"))) is not None):
                values[peer] = value
        if len(values) < 2:
            return {}
        return {
            "date": day, "industry": profile.get("industry") or industry, "market": "US",
            "sample": "Yahoo Finance selected peers", "peers": list(values),
            "peer_count": len(values), "candidates_checked": len(peers),
            "mean_trailing_pe": round(mean(values.values()), 2),
            "median_trailing_pe": round(median(values.values()), 2),
        }

    result = _observation(ticker, as_of_date, "industry_pe", fetch)
    if "unavailable" in result:
        raise NoMarketDataError(ticker, detail=result["unavailable"])
    return result


def get_company_pe_history(ticker, as_of_date=None):
    def fetch(symbol, day):
        frame = yf_retry(lambda: yf.Ticker(symbol).get_valuation_measures(freq="quarterly", periods=20))
        if frame is None or "Trailing P/E" not in frame.index:
            return {}
        series = frame.loc["Trailing P/E"]
        observations = {}
        for label, value in series.items():
            try:
                period_end = datetime.strptime(label, "%m/%d/%Y").date().isoformat()
            except (TypeError, ValueError):
                continue
            if period_end <= day and (multiple := _positive(value)) is not None:
                observations[period_end] = multiple
        if len(observations) < 2:
            return {}
        return {
            "frequency": "quarterly", "observation_count": len(observations),
            "first_period_end": min(observations), "last_period_end": max(observations),
            "mean_trailing_pe": round(mean(observations.values()), 2),
            "median_trailing_pe": round(median(observations.values()), 2),
        }

    return _observation(ticker, as_of_date, "company_pe_history", fetch)
