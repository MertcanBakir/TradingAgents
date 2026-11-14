from types import SimpleNamespace

import pandas as pd
import pytest

from tradingagents.agents.tools import get_valuation_benchmarks
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.vendors import fmp
from tradingagents.dataflows.vendors.yahoo import outlook, valuation

pytestmark = pytest.mark.unit
DAY = "2026-11-13"


@pytest.fixture(autouse=True)
def as_of_today(monkeypatch):
    monkeypatch.setattr(outlook, "get_current_date", lambda: DAY)
    monkeypatch.setattr(fmp, "is_historical", lambda day: day < DAY)


def test_fmp_uses_own_profile_and_exact_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("FMP_API_KEY", "test-key")
    calls = []

    def request(url, *, params, headers, **kwargs):
        assert "test-key" not in url and "apikey" not in params
        assert headers == {"apikey": "test-key"}
        endpoint = url.rsplit("/", 1)[-1]
        calls.append((endpoint, params))
        rows = {
            "profile": [{"symbol": "NVDA", "industry": "Semiconductors", "exchange": "NASDAQ"}],
            "industry-pe-snapshot": [
                {"date": DAY, "industry": "Semiconductors", "exchange": "NYSE", "pe": 10},
                {"date": DAY, "industry": "Semiconductors", "exchange": "NASDAQ", "pe": 24},
            ],
        }[endpoint]
        return SimpleNamespace(status_code=200, json=lambda: rows)

    monkeypatch.setattr(fmp.requests, "get", request)
    monkeypatch.setattr(valuation.yf, "Ticker", lambda _: SimpleNamespace(
        get_valuation_measures=lambda **_: pd.DataFrame(),
        get_info=lambda: pytest.fail("Yahoo peer fallback was called"),
    ))
    with run_config({"data_cache_dir": str(tmp_path)}):
        report = get_valuation_benchmarks.func("NVDA", trade_date=DAY)
    assert report["industry"]["aggregate_pe"] == 24
    assert calls == [
        ("profile", {"symbol": "NVDA"}),
        ("industry-pe-snapshot", {"date": DAY, "industry": "Semiconductors", "exchange": "NASDAQ"}),
    ]
    assert "unavailable" in report["company_history"]


@pytest.mark.parametrize("failure", ["no_key", "forbidden", "empty_snapshot"])
def test_yahoo_fallback_keeps_peer_and_company_history_separate(monkeypatch, tmp_path, failure):
    monkeypatch.setenv("FMP_API_KEY", "" if failure == "no_key" else "test-key")

    def request(url, **kwargs):
        assert failure != "no_key"
        if failure == "forbidden":
            return SimpleNamespace(status_code=403)
        rows = ([{"symbol": "NVDA", "industry": "Semiconductors", "exchange": "NASDAQ"}]
                if url.endswith("/profile") else [])
        return SimpleNamespace(status_code=200, json=lambda: rows)

    monkeypatch.setattr(fmp.requests, "get", request)
    prices = {"NVDA": 30, "A": 20, "B": 28, "C": -3, "D": 32, "E": 40}

    def ticker(symbol):
        return SimpleNamespace(
            get_info=lambda: {"industryKey": "other" if symbol == "D" else "semiconductors",
                              "market": "ca_market" if symbol == "E" else "us_market",
                              "trailingPE": prices[symbol]},
            get_valuation_measures=lambda **_: pd.DataFrame(
                [[30, 24, 30, -1, 999]], index=["Trailing P/E"],
                columns=["Current", "9/30/2026", "6/30/2026", "3/31/2026", "12/31/2026"],
            ),
        )

    monkeypatch.setattr(valuation.yf, "Ticker", ticker)
    monkeypatch.setattr(valuation.yf, "Industry", lambda *a, **k: SimpleNamespace(
        top_companies=pd.DataFrame(index=["NVDA", "A", "B", "B", "C", "D", "E"]),
    ))
    with run_config({"data_cache_dir": str(tmp_path)}):
        result = get_valuation_benchmarks.func("NVDA", trade_date=DAY)
        past = get_valuation_benchmarks.func("NVDA", trade_date="2026-11-12")
    assert result["industry"]["peers"] == ["A", "B"]
    assert result["industry"]["median_trailing_pe"] == 24
    assert "company_trailing_pe" not in result["industry"]
    assert result["company_history"]["observation_count"] == 2
    assert result["company_history"]["mean_trailing_pe"] == 27
    assert "company_trailing_pe" not in result["company_history"]
    assert "NO_DATA_AVAILABLE" in past["industry"]
    assert "unavailable" in past["company_history"]
