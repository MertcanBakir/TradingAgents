from datetime import date
from unittest.mock import Mock

import pandas as pd
import pytest
from langchain_core.messages import AIMessage

from tradingagents.agents.analysts import market_analyst as market
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo.candles import latest_daily_candle
from tradingagents.horizons import HORIZON_CONFIG
from tradingagents.technical_analysis import indicators, reporting
from tradingagents.technical_analysis.reporting import render_zones
from tradingagents.technical_analysis.zones import (
    ZoneEvidence,
    calculate_scipy_zones,
)


def _closed_rows(rows):
    rows.attrs["yahoo_daily_provenance"] = {
        "observed_at": "2026-11-04T13:00:00+00:00",
        "exchange_timezone": "America/New_York",
    }
    return rows


@pytest.mark.parametrize("selection", [
    '{"groups":["rsi","mfi","bollinger","invalid"]}',
    '{"groups":["invalid"]}',
    'not JSON',
])
def test_market_uses_one_price_load_and_two_calls_without_default_indicators(monkeypatch, selection):
    monkeypatch.setattr(market, "get_config", lambda: {"output_language": "English"})
    rows = pd.DataFrame({
        "Date": pd.bdate_range(end="2026-11-03", periods=20),
        "Open": 100.0, "High": 102.0, "Low": 99.0,
        "Close": range(101, 121), "Volume": 1000,
    })
    load = Mock(return_value=_closed_rows(rows))
    monkeypatch.setattr(market, "_load_prices", load)
    llm = Mock()
    llm.invoke.side_effect = [AIMessage(content=selection), AIMessage(content="Technical report")]

    result = market.create_market_analyst(llm)({
        "company_of_interest": "NVDA", "trade_date": "2026-11-03", "asset_type": "stock", "analysis_horizon": "medium_3m",
    })

    assert result["messages"] == [AIMessage(content="Technical report")]
    assert "Technical report" in result["market_report"]
    assert "**Candle status:** Closed" in result["market_report"]
    assert "**Last observed price:**" not in result["market_report"]
    assert "\n\nTechnical report\n\n### SciPy support and resistance zones" in result["market_report"]
    assert "### Support and resistance zones" not in result["market_report"]
    load.assert_called_once_with("NVDA", "2026-11-03", history_rows=350, interval="1d")
    assert llm.invoke.call_count == 2
    llm.bind_tools.assert_not_called()
    first, second = (call.args[0][-1].content for call in llm.invoke.call_args_list)
    assert "Date,Open,High,Low,Close,Volume" in first
    assert "atr_14 ATR %=" in second
    assert "close_20_ema Price-to-MA %=" in second
    assert "adx_14 (" in second
    instructions = llm.invoke.call_args_list[1].args[0][0].content
    assert "Do not include a summary table" in instructions
    if '"rsi"' in selection:
        assert "### sma, ### ema, ### macd, ### adx, ### rvol, ### atr, ### rsi, ### mfi" in instructions
    if '"rsi"' in selection:
        assert "### bollinger" not in instructions
        assert "limit is eight" in second
        for component in ("macd_12,26,9", "macds_12,26,9", "macdh_12,26,9"):
            assert any(line.startswith(f"{component}: ") for line in second.splitlines())
        assert "rsi_14:" in second and "close_200_sma:" in second
        assert "mfi_14 (0–100 scale):" in second
        assert "2026-11-03=100" in next(line for line in second.splitlines() if line.startswith("mfi_14 "))
        assert "2026-11-03=N/A" in second
    else:
        assert "rsi_14:" not in second and "mfi_14 " not in second
    assert ("outside the allowed list" if "invalid" in selection else "No readable") in second


def test_market_reports_new_groups_from_daily_rows(monkeypatch):
    monkeypatch.setattr(market, "get_config", lambda: {"output_language": "English"})
    close = list(range(100, 169)) + [165]
    high = [value + 1 for value in close]
    high[50] = 185
    high[55] = 190
    rows = pd.DataFrame({
        "Date": pd.bdate_range(end="2026-11-03", periods=70),
        "Open": close,
        "High": high,
        "Low": [value - 1 for value in close[:-1]] + [90],
        "Close": close,
        "Volume": [100] * 69 + [200],
    })
    monkeypatch.setattr(market, "_load_prices", Mock(return_value=_closed_rows(rows)))
    llm = Mock()
    llm.invoke.side_effect = [
        AIMessage(content='{"groups":["roc","obv"]}'),
        AIMessage(content="Technical report"),
    ]
    result = market.create_market_analyst(llm)({
        "company_of_interest": "NVDA", "trade_date": "2026-11-03", "asset_type": "stock", "analysis_horizon": "short_1m",
    })
    selection_prompt = llm.invoke.call_args_list[0].args[0][0].content
    assert "rolling_high_low:" not in selection_prompt
    evidence = llm.invoke.call_args_list[1].args[0][-1].content
    lines = {line.split()[0].rstrip(":"): line for line in evidence.splitlines() if "2026-11-03=" in line}
    assert lines["roc_5"].endswith("2026-11-03=0.609756")
    assert lines["rvol_20"].endswith("2026-11-03=2.00")
    assert "SciPy price zones:" in evidence
    assert "- **Resistance (63 bar):**" in evidence
    assert "### SciPy support and resistance zones" in result["market_report"]
    assert "### Support and resistance zones" not in result["market_report"]
    assert "First formed" in result["market_report"]
    assert "prior_high_" not in evidence and "prior_low_" not in evidence
    assert lines["obv"].endswith("2026-11-03=6600.00")


def test_flat_prices_and_zero_volume_do_not_produce_infinite_indicators():
    rows = pd.DataFrame({"High": [100] * 28, "Low": 100, "Close": 100, "Volume": 0})

    adx = indicators._adx(rows, 14)
    assert adx.iloc[:-1].isna().all()
    assert adx.iloc[-1] == 0
    assert indicators._rvol(rows, 20).isna().all()


@pytest.mark.parametrize(
    ("horizon", "history_rows", "target_days", "macd", "roc", "obv", "required", "optional"),
    [
        ("short_1m", 180, 21, "8,21,5", "5", 20, ("ema", "rsi", "macd", "atr", "rvol"),
         {"sma", "bollinger", "roc", "vwma", "mfi", "adx", "obv"}),
        ("medium_3m", 350, 63, "12,26,9", "20", 63, ("sma", "ema", "macd", "adx", "rvol", "atr"),
         {"rsi", "bollinger", "roc", "vwma", "mfi", "obv"}),
        ("long_12m", 1000, 252, "24,52,18", "63", 252, ("sma", "roc", "macd", "atr", "adx"),
         {"ema", "rsi", "bollinger", "vwma", "mfi", "rvol", "obv"}),
    ],
)
def test_selected_horizon_controls_yahoo_and_calculations(
    monkeypatch, horizon, history_rows, target_days, macd, roc, obv, required, optional,
):
    close = pd.Series(range(100, 100 + history_rows), dtype=float)
    rows = pd.DataFrame({
        "Date": pd.bdate_range(end="2026-11-03", periods=history_rows),
        "Open": close, "High": close + 2, "Low": close - 2, "Close": close, "Volume": 100,
    })
    load = Mock(return_value=_closed_rows(rows))
    monkeypatch.setattr(market, "_load_prices", load)
    llm = Mock()
    llm.invoke.side_effect = [
        AIMessage(content='{"groups":["roc","obv","bollinger","rsi"]}'),
        AIMessage(content="Technical report"),
    ]

    market.create_market_analyst(llm)({
        "company_of_interest": "NVDA", "trade_date": "2026-11-03",
        "asset_type": "stock", "analysis_horizon": horizon,
    })

    load.assert_called_once_with("NVDA", "2026-11-03", history_rows=history_rows, interval="1d")
    assert llm.invoke.call_count == 2
    selection_data = llm.invoke.call_args_list[0].args[0][-1].content
    price_rows = selection_data.split("Date,Open,High,Low,Close,Volume\n", 1)[1].splitlines()
    assert [row.split(",", 1)[0] for row in price_rows] == rows["Date"].tail(target_days).dt.strftime("%Y-%m-%d").tolist()
    selection_prompt = llm.invoke.call_args_list[0].args[0][0].content
    assert f"Select up to {8 - len(required)} optional groups" in selection_prompt
    menu = selection_prompt.split("Optional groups and components: ", 1)[1]
    assert {entry.split(":", 1)[0] for entry in menu.split("; ")} == optional
    report_prompt = llm.invoke.call_args_list[1].args[0][0].content
    assert "3-month run" not in report_prompt
    assert "Explain where the supplied indicators agree or conflict" in report_prompt
    assert ", ".join(f"### {group}" for group in required) in report_prompt
    evidence = llm.invoke.call_args_list[1].args[0][-1].content
    assert "rolling_high_" not in evidence and "rolling_low_" not in evidence
    assert f"macdh_{macd} last 3 bars:" in evidence
    if horizon != "short_1m":
        assert "close_50_sma vs close_200_sma" in evidence
        assert "strength change over 5 bars:" in evidence
    assert "RVOL=" not in evidence
    if horizon == "long_12m":
        assert "roc_252 (" in evidence and "adx_21 (" in evidence
        assert "rvol_63 (" not in evidence and "close_50_ema:" not in evidence
    for component in ("macd", "macds", "macdh"):
        assert f"\n{component}_{macd}: " in evidence
    assert f"\nroc_{roc} (" in evidence
    line = next(line for line in evidence.splitlines() if line.startswith(f"obv_change_{obv} "))
    assert line.endswith(f"2026-11-03={obv * 100}.00")
    assert "could not be calculated" not in evidence
    assert "N/A" not in line
    assert rows["Date"].iloc[0].strftime("%Y-%m-%d") not in evidence


@pytest.mark.parametrize("horizon", [{}, {"analysis_horizon": "unknown"}])
def test_market_refuses_missing_or_invalid_horizon_before_loading_data(monkeypatch, horizon):
    load = Mock()
    monkeypatch.setattr(market, "_load_prices", load)
    llm = Mock()
    run = market.create_market_analyst(llm)

    with pytest.raises(ValueError, match="Choose an analysis horizon"):
        run({"company_of_interest": "NVDA", "trade_date": "2026-11-03", **horizon})

    load.assert_not_called()
    llm.invoke.assert_not_called()



@pytest.mark.parametrize(
    ("observed_at", "market_time", "expected"),
    [
        ("2026-11-27T17:15:00Z", "2026-11-27T17:12:00Z", False),
        ("2026-11-27T18:02:00Z", "2026-11-27T17:59:00Z", None),
        ("2026-11-27T18:02:00Z", "2026-11-27T18:00:00Z", True),
    ],
)
def test_daily_candle_uses_source_session_end_and_source_observation_time(observed_at, market_time, expected):
    rows = pd.DataFrame({"Date": [pd.Timestamp("2026-11-27")]})
    rows.attrs["yahoo_daily_provenance"] = {
        "observed_at": observed_at,
        "market_time": market_time,
        "exchange_timezone": "America/New_York",
        "session_start": "2026-11-27T14:30:00Z",
        "session_end": "2026-11-27T18:00:00Z",
    }
    candle = latest_daily_candle(rows)
    assert candle.closed is expected
    assert candle.as_of_timestamp.endswith("-05:00")
    assert candle.timezone == "America/New_York"


def test_partial_bar_is_disclosed_but_excluded_from_all_model_calculations(monkeypatch):
    monkeypatch.setattr(market, "get_config", lambda: {"output_language": "Turkish"})
    rows = pd.DataFrame({
        "Date": pd.bdate_range(end="2026-11-27", periods=215),
        "Open": 100.0, "High": [101.0] * 214 + [999.0],
        "Low": 99.0, "Close": [100.0] * 214 + [999.0], "Volume": [100] * 214 + [123456789],
    })
    rows.attrs["yahoo_daily_provenance"] = {
        "observed_at": "2026-11-27T16:30:00Z",
        "market_time": "2026-11-27T16:28:00Z",
        "session_start": "2026-11-27T14:30:00Z",
        "session_end": "2026-11-27T18:00:00Z",
        "exchange_timezone": "America/New_York",
    }
    monkeypatch.setattr(market, "_load_prices", Mock(return_value=rows))
    llm = Mock()
    recap = (
        "**NVDA — analiz tarihi: 2026-11-27; değerlendirme ufku: 3 ay, yaklaşık 63 işlem günü.** "
        "Son tamamlanmış günlük bar **2026-11-26** tarihlidir; bar kapanmıştır. "
        "Veri zaman damgası: 2026-11-27T11:28:00-05:00, America/New_York."
    )
    llm.invoke.side_effect = [
        AIMessage(content='{"groups":[]}'),
        AIMessage(content=f"## Technical Outlook\n\n{recap}\n\nEvidence\n\n**Rating**: Bearish\n\nFINAL TRANSACTION PROPOSAL: SELL."),
    ]
    result = market.create_market_analyst(llm)({
        "company_of_interest": "NVDA", "trade_date": "2026-11-27", "analysis_horizon": "medium_3m",
    })

    for call in llm.invoke.call_args_list:
        model_data = call.args[0][-1].content
        assert "999.00" not in model_data and "123456789" not in model_data
    report = result["market_report"]
    assert report.startswith("## Technical Outlook\n\n**Günlük mum bilgisi**")
    assert "- **Mum durumu:** Henüz kapanmadı" in report
    assert "- **Veri zamanı:** 2026-11-27 11:28:00-05:00 (America/New_York, piyasa veri zamanı)" in report
    assert "- **Son gözlenen fiyat:** 999.00" in report
    assert "123456789" not in report
    assert "Rating" not in report and "FINAL TRANSACTION PROPOSAL" not in report
    assert "değerlendirme ufku" not in report
    assert "Evidence" in report
    prompt = llm.invoke.call_args_list[-1].args[0][0].content
    assert "Omit ratings, transaction proposals" in prompt
    assert "FINAL TRANSACTION PROPOSAL" not in prompt
    assert "Omit any introductory recap" in prompt
    evidence = llm.invoke.call_args_list[-1].args[0][-1].content
    assert "close_50_ema:" in evidence
    assert "adx_14 " in evidence
    assert "2026-11-26,100.00" in evidence
    assert "as_of_timestamp" not in evidence
    assert "close_20_ema Price-to-MA %=0.00" in evidence
    assert "close_20_ema MA Slope %" in evidence
    assert "atr_14 ATR %=2.00" in evidence
    assert "### Destek ve direnç bölgeleri" not in report
    assert "### SciPy destek ve direnç bölgeleri" in report
    assert "999.00" not in report.split("### SciPy destek ve direnç bölgeleri", 1)[1]


def test_zone_report_shows_one_summary_line_per_zone_in_both_languages():
    zones = [
        ZoneEvidence(252, "support", date(2026, 11, 1), (88.0, 92.0), contact_count=2),
        ZoneEvidence(252, "resistance", date(2026, 11, 3), (108.0, 112.0)),
    ]

    assert render_zones(zones).splitlines() == [
        "- **Support (252 bar):** 88.00–92.00. First formed 2026-11-01. Subsequent contacts: 2.",
        "- **Resistance (252 bar):** 108.00–112.00. First formed 2026-11-03. No subsequent contact.",
    ]
    assert render_zones(zones, language="Turkish").splitlines() == [
        "- **Destek (252 bar):** 88.00–92.00. İlk oluşum 2026-11-01. Sonraki temas sayısı 2.",
        "- **Direnç (252 bar):** 108.00–112.00. İlk oluşum 2026-11-03. Sonraki temas görülmedi.",
    ]


def test_derived_ma_metrics_use_the_configured_reference_and_price():
    values = pd.Series([90.0, 100.0, 108.0, 110.0])
    assert indicators._moving_average_metrics("close_20_ema", values, close=121.0, slope_bars=2) == [
        "close_20_ema Price-to-MA %=10.00",
        "close_20_ema MA Slope % (2 bars)=10.00",
    ]
    assert "N/A" in indicators._moving_average_metrics("close_20_ema", values, close=121.0, slope_bars=4)[1]


def test_trend_comparisons_use_available_values_and_histogram_magnitude():
    dates = pd.date_range("2026-11-01", periods=6)
    groups = {
        group: [indicators.IndicatorSpec(name) for name in names]
        for group, names in {
            "ema": ("close_20_ema", "close_50_ema"),
            "adx": ("adx_14",),
            "macd": ("macd_12,26,9", "macds_12,26,9", "macdh_12,26,9"),
        }.items()
    }
    calculated = {
        "close_20_ema": pd.Series([110.0] * 6),
        "close_50_ema": pd.Series([100.0] * 6),
        "adx_14": pd.Series([27.0, 26.0, 25.0, 24.0, 23.0, 22.0]),
        "macdh_12,26,9": pd.Series([-8.0, -7.0, -6.0, -3.0, -2.0, -1.0]),
    }
    assert indicators.trend_comparisons(groups, calculated, pd.Series(dates)) == [
        "close_20_ema vs close_50_ema (2026-11-06) %=10.00",
        "adx_14 strength change over 5 bars: 2026-11-01=27.00, 2026-11-06=22.00, change=-5.00 points",
        "macdh_12,26,9 last 3 bars: 2026-11-04=-3.00, 2026-11-05=-2.00, 2026-11-06=-1.00; absolute magnitude contracting",
    ]

    calculated["adx_14"].iloc[0] = float("nan")
    calculated["macdh_12,26,9"].iloc[-2] = float("nan")
    assert len(indicators.trend_comparisons(groups, calculated, pd.Series(dates))) == 1



@pytest.mark.parametrize(
    ("error", "explanation"),
    [
        (NoMarketDataError("NVDA", detail="no rows"), "returned no usable daily prices"),
        (VendorUnavailableError("upstream request failed"), "Yahoo Finance was unavailable"),
    ],
)
def test_market_reports_distinct_data_errors_without_calling_models(monkeypatch, error, explanation):
    monkeypatch.setattr(market, "get_config", lambda: {"output_language": "English"})
    monkeypatch.setattr(market, "_load_prices", Mock(side_effect=error))
    llm = Mock()

    result = market.create_market_analyst(llm)({
        "company_of_interest": "NVDA", "trade_date": "2026-11-03", "analysis_horizon": "medium_3m",
    })

    assert explanation in result["market_report"]
    assert not result["messages"]
    llm.invoke.assert_not_called()


def test_market_discloses_unconfirmed_candle_without_calling_models(monkeypatch):
    monkeypatch.setattr(market, "get_config", lambda: {"output_language": "Turkish"})
    rows = pd.DataFrame({"Date": [pd.Timestamp("2026-11-03")], "Close": [100.0]})
    monkeypatch.setattr(market, "_load_prices", Mock(return_value=rows))
    llm = Mock()

    result = market.create_market_analyst(llm)({
        "company_of_interest": "NVDA", "trade_date": "2026-11-03", "analysis_horizon": "medium_3m",
    })

    assert "Hesaplamaya uygun tamamlanmış mum yok." in result["market_report"]
    assert "**Mum durumu:** Kapanışı doğrulanamadı" in result["market_report"]
    assert "**Son gözlenen fiyat:** 100.00" in result["market_report"]
    llm.invoke.assert_not_called()


def test_indicator_calculation_failure_logs_name_and_keeps_remaining_results(caplog):
    rows = pd.DataFrame({"Date": [pd.Timestamp("2026-11-03")], "Close": [100.0]})
    failing = indicators.IndicatorSpec("broken", calculate=Mock(side_effect=ValueError("invalid indicator data")))
    healthy = indicators.IndicatorSpec("healthy", calculate=lambda prices: prices["Close"])

    evidence, calculated = _calculate_and_render(rows, {"test": [failing, healthy]}, 21)

    assert "Could not calculate market indicator broken" in caplog.text
    assert evidence == ["broken: could not be calculated.", "healthy: 2026-11-03=100.00"]
    assert calculated["healthy"].iloc[-1] == 100.0



@pytest.mark.parametrize(
    "volume",
    [
        pytest.param([float("nan")] * 60, id="all-missing"),
        pytest.param([0.0] * 60, id="all-zero"),
        pytest.param([100.0] * 58 + [float("nan"), 100.0], id="missing-in-window"),
        pytest.param([100.0] * 59 + [float("nan")], id="missing-current"),
        pytest.param([100.0] * 10, id="insufficient-history"),
    ],
)
def test_volume_indicators_report_na_for_invalid_windows(volume):
    rows = _volume_rows(volume)
    evidence, calculated = _volume_context(rows)

    for name in ("mfi_14", "vwma_20", "rvol_20"):
        assert pd.isna(calculated[name].iloc[-1])
        line = next(line for line in evidence if line.startswith(f"{name}: ") or line.startswith(f"{name} ("))
        assert line.endswith("2026-11-03=N/A")
    assert "vwma_20 Price-to-MA %=N/A" in evidence


def test_zero_current_volume_is_valid_with_a_positive_volume_reference():
    rows = _volume_rows([100.0] * 59 + [0.0])
    _, calculated = _volume_context(rows)

    assert calculated["rvol_20"].iloc[-1] == 0
    assert calculated["mfi_14"].iloc[-1] == 100
    assert calculated["vwma_20"].iloc[-1] > 0


def test_older_volume_gap_does_not_create_a_valid_cumulative_obv():
    rows = _volume_rows([100.0] * 4 + [float("nan")] + [100.0] * 55)
    _, calculated = _volume_context(rows)

    assert pd.isna(calculated["obv"].iloc[-1])
    assert calculated["obv_change_20"].iloc[-1] == 2000
    assert calculated["mfi_14"].iloc[-1] == 100
    assert calculated["vwma_20"].iloc[-1] > 0


def _volume_rows(volume):
    close = pd.Series(range(100, 100 + len(volume)), dtype=float)
    return pd.DataFrame({
        "Date": pd.bdate_range(end="2026-11-03", periods=len(volume)),
        "Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": volume,
    })


def _volume_context(rows):
    groups = indicators.profile_indicators(HORIZON_CONFIG["short_1m"])
    return _calculate_and_render(rows, {group: groups[group] for group in ("mfi", "vwma", "rvol", "obv")}, 21)


@pytest.mark.parametrize(
    "calculate",
    [
        pytest.param(lambda rows: rows["Close"].iloc[:-1], id="shorter-series"),
        pytest.param(lambda rows: rows["Close"].iloc[::-1], id="reversed-dates"),
        pytest.param(lambda rows: rows["Close"].reset_index(drop=True), id="missing-dates"),
    ],
)
def test_misaligned_indicator_series_is_rejected_without_stopping_other_calculations(calculate, caplog):
    rows = _volume_rows([100.0] * 12)
    groups = {"test": [
        indicators.IndicatorSpec("misaligned", calculate=calculate),
        indicators.IndicatorSpec("healthy", calculate=lambda prices: prices["Close"]),
    ]}

    evidence, calculated = _calculate_and_render(rows, groups, 21)

    assert "Could not calculate market indicator misaligned" in caplog.text
    assert "Indicator output must contain one value per candle in date order" in caplog.text
    assert "misaligned: could not be calculated." in evidence
    assert "misaligned" not in calculated
    assert calculated["healthy"].loc[pd.Timestamp("2026-11-03")] == 111.0



def test_market_handles_a_missing_volume_column_without_losing_price_indicators(monkeypatch):
    rows = _closed_rows(_volume_rows([100.0] * 60).drop(columns="Volume"))
    monkeypatch.setattr(market, "_load_prices", Mock(return_value=rows))
    llm = Mock()
    llm.invoke.side_effect = [AIMessage(content='{"groups":["mfi","vwma"]}'), AIMessage(content="Technical report")]

    result = market.create_market_analyst(llm)({
        "company_of_interest": "NVDA", "trade_date": "2026-11-03", "analysis_horizon": "short_1m",
    })

    assert "Technical report" in result["market_report"]
    evidence = llm.invoke.call_args_list[-1].args[0][-1].content
    for name in ("mfi_14", "vwma_20", "rvol_20"):
        line = next(line for line in evidence.splitlines() if line.startswith(f"{name}: ") or line.startswith(f"{name} ("))
        assert line.endswith("2026-11-03=N/A")
    assert "2026-11-03=100.00" in next(line for line in evidence.splitlines() if line.startswith("rsi_14: "))


def _calculate_and_render(rows, groups, slope_bars):
    results = indicators.calculate_indicators(rows, groups, slope_bars)
    evidence = reporting.indicator_context(rows, groups, results)
    return evidence, {name: result.values for name, result in results.items()}



def test_scipy_finds_intermediate_resistance_and_ranks_it_ahead_of_the_window_high():
    close = pd.Series([95, 99, 104, 98, 100, 106, 115, 110, 105, 98, 104, 100, 96, 93, 95])
    rows = pd.DataFrame({
        "Date": pd.date_range("2026-10-01", periods=len(close)),
        "High": close + 1, "Low": close - 1, "Close": close,
    })
    atr = pd.Series(8.0, index=rows.index)

    candidates = calculate_scipy_zones(rows, 14, atr)
    resistance = [zone for zone in candidates if zone.side == "resistance"]

    assert [zone.bounds for zone in resistance] == [(103.0, 107.0), (114.0, 118.0)]
    assert resistance[0].formation_date == date(2026, 10, 3)
    assert resistance[0].contact_count == 3
    nearest = calculate_scipy_zones(rows, 14, atr, per_side=1, recent_per_side=0)
    assert sum(zone.side == "resistance" for zone in nearest) == 1
    with_recent = calculate_scipy_zones(rows, 14, atr, per_side=1, recent_per_side=1)
    assert [zone.bounds for zone in with_recent if zone.side == "resistance"] == [(103.0, 107.0), (114.0, 118.0)]


def test_scipy_does_not_confirm_an_endpoint_or_cross_a_missing_price():
    rows = pd.DataFrame({
        "Date": pd.date_range("2026-10-01", periods=5),
        "High": [101, 111, float("nan"), 105, 120],
        "Low": [99, 100, 101, 102, 103],
        "Close": [100, 104, 102, 104, 110],
    })

    assert calculate_scipy_zones(rows, 5, pd.Series(4.0, index=rows.index)) == []



@pytest.mark.parametrize("missing", ["High", "Low", "both", "columns"])
def test_missing_daily_price_ranges_leave_only_dependent_indicators_unavailable(missing):
    rows = _volume_rows([100.0] * 60)
    if missing == "columns":
        rows = rows.drop(columns=["High", "Low"])
    elif missing == "both":
        rows[["High", "Low"]] = float("nan")
    else:
        rows[missing] = float("nan")
    groups = indicators.profile_indicators(HORIZON_CONFIG["short_1m"])
    selected = {name: groups[name] for name in ("atr", "mfi", "vwma", "sma")}

    evidence, calculated = _calculate_and_render(rows, selected, 21)

    for name in ("atr_14", "mfi_14", "vwma_20"):
        assert pd.isna(calculated[name].iloc[-1])
        line = next(line for line in evidence if line.startswith(f"{name}: ") or line.startswith(f"{name} ("))
        assert line.endswith("2026-11-03=N/A")
    assert calculated["close_20_sma"].iloc[-1] > 0


def test_price_range_indicators_resume_only_after_complete_windows():
    rows = _volume_rows([100.0] * 60)
    rows.loc[30, "High"] = float("nan")
    groups = indicators.profile_indicators(HORIZON_CONFIG["short_1m"])
    selected = {name: groups[name] for name in ("atr", "mfi", "vwma")}

    _, calculated = _calculate_and_render(rows, selected, 21)

    for name, first_valid in (("atr_14", 44), ("mfi_14", 45), ("vwma_20", 50)):
        assert calculated[name].iloc[30:first_valid].isna().all()
        assert pd.notna(calculated[name].iloc[first_valid])
    assert calculated["atr_14"].iloc[-1] == pytest.approx(2.0)
    assert calculated["mfi_14"].iloc[-1] == 100.0
    assert calculated["vwma_20"].iloc[-1] == pytest.approx(149.5)


def test_atr_on_complete_prices_preserves_the_previous_calculation():
    from stockstats import wrap

    rows = _volume_rows([100.0] * 60)
    rows.loc[10:25, "High"] += 3
    rows.loc[40:50, "Low"] -= 2
    expected = wrap(rows.copy())["atr_14"].astype(float)
    expected.iloc[:13] = float("nan")

    actual = indicators._atr(rows, 14)

    pd.testing.assert_series_equal(actual, expected, check_names=False)
