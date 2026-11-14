from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from tradingagents.agents import tools
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.outlook import compare_consensus_to_guidance
from tradingagents.dataflows.vendors import sec_edgar, sec_outlook
from tradingagents.dataflows.vendors.yahoo import outlook

pytestmark = pytest.mark.unit


def test_empty_yahoo_responses_are_reported_as_unavailable(tmp_path, monkeypatch):
    day = "2026-09-15"
    ticker = SimpleNamespace(
        get_revenue_estimate=pd.DataFrame,
        get_earnings_estimate=pd.DataFrame,
        get_eps_trend=pd.DataFrame,
        get_eps_revisions=pd.DataFrame,
        get_analyst_price_targets=dict,
        get_calendar=dict,
    )
    monkeypatch.setattr(outlook, "get_current_date", lambda: day)
    monkeypatch.setattr(outlook.yf, "Ticker", lambda _: ticker)

    with run_config({"data_cache_dir": str(tmp_path)}):
        for fetch in (outlook.get_estimate_revisions, outlook.get_upcoming_earnings,
                      outlook.get_analyst_price_targets):
            assert fetch("NVDA", day) == {
                "source": "Yahoo Finance", "unavailable": "No usable data returned.",
            }
        assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("failed_source", ["get_upcoming_earnings", "get_company_guidance"])
def test_earnings_tool_keeps_the_available_source_when_the_other_fails(monkeypatch, failed_source):
    calendar = {"status": "estimated", "dates": ["2026-11-18"]}
    company = {"guidance": [{"metric": "revenue", "low": 100, "high": 110}]}
    sources = {"get_upcoming_earnings": calendar, "get_company_guidance": company}

    def route(method, _ticker, _date):
        if method == failed_source:
            raise VendorUnavailableError("provider timed out")
        return sources[method]

    monkeypatch.setattr(tools, "route_to_vendor", route)
    result = tools.get_upcoming_earnings_and_guidance.func("NVDA", trade_date="2026-09-15")
    if failed_source == "get_upcoming_earnings":
        assert "unavailable" in result["calendar"]
        assert result["company"] == company
    else:
        assert result["calendar"] == calendar
        assert "unavailable" in result["company"]


def test_yahoo_archives_observations_and_compares_revenue_across_relative_period_labels(tmp_path, monkeypatch):
    day = "2026-09-08"
    label = "+1q"
    end_date = "2026-10-25"
    revenue = 100
    calls = []

    def frame(**fields):
        return pd.DataFrame([fields], index=[label])

    def ticker(symbol):
        calls.append(symbol)
        return SimpleNamespace(
            _analysis=SimpleNamespace(_earnings_trend=[{"period": label, "endDate": end_date}]),
            get_revenue_estimate=lambda: frame(avg=revenue, currency="USD"),
            get_earnings_estimate=lambda: frame(avg=2.64),
            get_eps_trend=lambda: frame(current=2.64, **{"30daysAgo": 2.50}),
            get_eps_revisions=lambda: frame(upLast30days=12),
            get_analyst_price_targets=lambda: {"current": 125, "median": 140, "mean": 145, "high": 175, "low": 100},
            get_calendar=lambda: {"Earnings Date": [date(2026, 11, 20), date(2026, 11, 18), date(2026, 8, 26)]},
            get_info=lambda: {"currency": "USD"},
        )

    monkeypatch.setattr(outlook, "get_current_date", lambda: day)
    monkeypatch.setattr(outlook.yf, "Ticker", ticker)
    with run_config({"data_cache_dir": str(tmp_path)}):
        previous = outlook.get_estimate_revisions("NVDA", day)
        assert "revenue_revision_note" in previous
        assert outlook.get_analyst_price_targets("NVDA", day)["median_vs_reference_pct"] == 12
        assert outlook.get_upcoming_earnings("NVDA", day)["dates"] == ["2026-11-18", "2026-11-20"]
        day, label, revenue = "2026-09-15", "0q", 103
        row = outlook.get_estimate_revisions("NVDA", day)["periods"][0]
        assert row["revenue_revisions"]["7d"] == {
            "previous_observation": "2026-09-08", "previous_average": 100,
            "change": 3, "change_pct": 3,
        }
        assert row["eps_consensus_changes"]["30d"] == {"change": 0.14, "change_pct": 5.6}
        count = len(calls)
        assert outlook.get_estimate_revisions("NVDA", "2026-09-08") == previous
        assert outlook.get_analyst_price_targets("NVDA", "2026-09-08")["median"] == 140
        assert "unavailable" in outlook.get_upcoming_earnings("NVDA", "2026-09-07")
        assert len(calls) == count


def test_sec_guidance_reads_dated_exhibits_and_matches_verified_estimate_period(monkeypatch):
    accession = "0001045810-26-000073"
    schedule = "0001045810-26-000080"
    future = "0001045810-26-000090"
    payload = {"filings": {"recent": {
        "form": ["8-K"] * 3, "filingDate": ["2026-11-19", "2026-09-03", "2026-08-26"],
        "accessionNumber": [future, schedule, accession], "items": ["2.02", "7.01", "2.02"],
    }}}
    release = """
        <h1>Financial Results for Second Quarter Fiscal 2027</h1>
        <p>Revenue was reported for the second quarter ended July 26, 2026.</p>
        <div><b>Outlook</b></div>
        <div>Outlook for the third quarter of fiscal 2027 is as follows:</div>
        <div>Revenue is expected to be <span>$</span><span>108.0 billion</span>, plus or minus 2%.</div>
    """
    urls = []

    def request(url):
        urls.append(url)
        assert future.replace("-", "") not in url
        if url.endswith("-index.htm"):
            return SimpleNamespace(text="""<table><tr>
                <td>2</td><td>Press Release</td><td><a href="exhibit.htm">exhibit.htm</a></td><td>EX-99.1</td>
                </tr></table>""")
        if schedule.replace("-", "") in url:
            return SimpleNamespace(text="<p>Company to announce financial results on November 18, 2026</p>")
        return SimpleNamespace(text=release)

    def cached(_url, name):
        if name.startswith("submissions_"):
            return payload
        return {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [{"filed": "2026-08-26"}]}}}}}

    monkeypatch.setattr(sec_edgar, "cik_for", lambda _: "0001045810")
    monkeypatch.setattr(sec_edgar, "_cached_json", cached)
    monkeypatch.setattr(sec_edgar, "_request", request)
    company = sec_outlook.get_company_guidance("NVDA", "2026-09-15")

    assert len(urls) == 4
    assert company["confirmed_earnings"]["dates"] == ["2026-11-18"]
    guidance, = company["guidance"]
    assert guidance["period_ends"] == ["2026-10-25", "2026-11-01"]
    assert guidance["fiscal_period"] == "FY2027-Q3"
    assert (guidance["currency"], guidance["low"], guidance["high"]) == ("USD", 105840000000, 110160000000)

    estimates = {"periods": [{
        "period": "0q", "period_end": "2026-10-25", "revenue": {"avg": 110500000000, "currency": "USD"},
    }]}
    comparison, = compare_consensus_to_guidance(estimates, {"company": company})
    assert comparison["position"] == "above"
    assert comparison["difference_vs_range"] == 340000000
    estimates["periods"][0]["period_end"] = "2026-10-31"
    estimates["periods"][0]["revenue"]["avg"] = 109043188870
    comparison, = compare_consensus_to_guidance(estimates, {"company": company})
    assert comparison["position"] == "within"
    assert comparison["difference_vs_midpoint"] == 1043188870
    estimates["periods"][0]["period_end"] = "2026-11-30"
    assert compare_consensus_to_guidance(estimates, {"company": company}) == []
    estimates["periods"][0]["period_end"] = "2026-10-31"
    estimates["periods"][0]["revenue"]["currency"] = "CAD"
    assert compare_consensus_to_guidance(estimates, {"company": company}) == []


def test_eps_comparison_requires_same_methodology_and_period():
    guidance = {"metric": "eps", "currency": "USD", "basis": "gaap", "low": 2.0, "high": 2.5,
                "fiscal_period": "FY2027-Q3", "period_ends": ["2026-10-25"], "period_type": "quarter"}
    estimate = {"period": "0q", "period_end": "2026-10-25",
                "eps": {"avg": 2.25, "currency": "USD", "methodology": "normalized"}}
    company = {"company": {"guidance": [guidance]}}
    estimates = {"periods": [estimate]}
    assert compare_consensus_to_guidance(estimates, company) == []
    estimate["eps"]["methodology"] = "gaap"
    estimate["period"] = "0y"
    assert compare_consensus_to_guidance(estimates, company) == []
    estimate["period"] = "0q"
    comparison, = compare_consensus_to_guidance(estimates, company)
    assert comparison["position"] == "within"
