"""Agents on the schema-only structured-output path must not invite tool calls (#1130).

`with_structured_output` binds exactly one tool (the schema). A prompt that
primes tool use makes models emit an unknown `web_search` call, which discards
the structured attempt and forces a free-text retry — an extra LLM round trip
and the loss of typed output.

These assert the constraint reaches the *rendered* prompt each agent actually
sends, not merely that the constant is referenced in the module.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

import tradingagents.agents.analysts.sentiment_analyst as sentiment
from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.structured import NO_EXTERNAL_TOOLS
from tradingagents.agents.trader.trader import create_trader


def _capturing_llm(captured: dict, result):
    """LLM whose structured binding records the prompt it was handed."""
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or result
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


def _prompt_text(prompt) -> str:
    """Flatten a captured prompt (str, message list, or objects) to text."""
    if isinstance(prompt, str):
        return prompt
    parts = []
    for m in prompt:
        parts.append(m.get("content", "") if isinstance(m, dict) else getattr(m, "content", ""))
    return "\n".join(str(p) for p in parts)


@pytest.mark.unit
def test_trader_prompt_states_constraint():
    from tradingagents.agents.schemas import TraderAction, TraderProposal

    captured = {}
    llm = _capturing_llm(captured, TraderProposal(action=TraderAction.BUY, reasoning="x"))
    create_trader(llm)({
        "company_of_interest": "NVDA",
        "investment_plan": "**Recommendation**: Buy",
        "market_report": "Current price $189.5; ATR 4.2.",
    })
    assert NO_EXTERNAL_TOOLS in _prompt_text(captured["prompt"])


@pytest.mark.unit
def test_research_manager_prompt_states_constraint():
    from tradingagents.agents.schemas import PortfolioRating, ResearchPlan

    captured = {}
    llm = _capturing_llm(
        captured,
        ResearchPlan(
            recommendation=PortfolioRating.BUY, rationale="x", strategic_actions="y"
        ),
    )
    create_research_manager(llm)({
        "company_of_interest": "NVDA",
        "investment_debate_state": {
            "history": "h", "bull_history": "b", "bear_history": "r",
            "current_response": "", "count": 1,
        },
    })
    assert NO_EXTERNAL_TOOLS in _prompt_text(captured["prompt"])


@pytest.mark.unit
@pytest.mark.parametrize("use_structured", [True, False])
def test_portfolio_manager_prompt_states_constraint(use_structured):
    from langchain_core.messages import AIMessage

    from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating
    from tradingagents.dataflows.config import run_config

    captured = {}
    llm = _capturing_llm(
        captured,
        PortfolioDecision(
            rating=PortfolioRating.HOLD,
            executive_summary="x",
            investment_thesis="y",
        ),
    )
    risk = {
        "history": "h", "aggressive_history": "a", "conservative_history": "c",
        "neutral_history": "n", "current_aggressive_response": "",
        "current_conservative_response": "", "current_neutral_response": "",
        "latest_speaker": "Neutral", "count": 1,
    }
    if not use_structured:
        llm.with_structured_output.return_value.invoke.side_effect = lambda prompt: (
            captured.__setitem__("prompt", prompt) or None
        )
        llm.invoke.return_value = AIMessage(content="**Rating**: Hold\n**Time Horizon**: 6 months")
    with run_config({"output_language": "Turkish"}):
        state = create_portfolio_manager(llm)({
            "company_of_interest": "NVDA",
            "analysis_horizon": "short_1m",
            "risk_debate_state": risk,
            "investment_plan": "plan",
            "trader_investment_plan": "trader plan",
        })
    prompt = _prompt_text(captured["prompt"])
    assert NO_EXTERNAL_TOOLS in prompt
    assert "Short (1 month)" in prompt
    assert state["final_trade_decision"].endswith("**Time Horizon**: Kısa (1 ay)")
    assert "6 months" not in state["final_trade_decision"]


@pytest.mark.unit
def test_sentiment_prompt_states_constraint(monkeypatch):
    from tradingagents.agents.schemas import SentimentBand, SentimentReport

    # Pre-fetched sources are stubbed so the prompt builds without network I/O.
    monkeypatch.setattr(sentiment, "fetch_stocktwits_messages", lambda *a, **k: "st")
    monkeypatch.setattr(sentiment, "fetch_reddit_posts", lambda *a, **k: "rd")
    monkeypatch.setattr(sentiment.get_news, "func", lambda *a, **k: "news", raising=False)

    captured = {}
    llm = _capturing_llm(captured, SentimentReport(
        overall_band=SentimentBand.BULLISH, overall_score=7.5,
        confidence="high", narrative="n",
    ))
    sentiment.create_sentiment_analyst(llm)({
        "company_of_interest": "NVDA", "trade_date": "2026-01-15",
        "asset_type": "stock", "messages": [],
    })
    text = _prompt_text(captured["prompt"])
    assert NO_EXTERNAL_TOOLS in text
    # This agent binds no tools, so tool-range wording must not reappear.
    assert "tool-call date ranges" not in text


@pytest.mark.unit
def test_fundamentals_prefetches_all_sources_once_and_writes_one_report(monkeypatch):
    from langchain_core.messages import AIMessage

    import tradingagents.agents.analysts.fundamentals_analyst as fundamentals
    from tradingagents.agents import tools
    from tradingagents.dataflows.config import run_config

    ticker, date = "NVDA", "2026-01-15"
    expected = [
        ("get_fundamentals", (ticker, date)),
        ("get_income_statement", (ticker, "quarterly", date)),
        ("get_balance_sheet", (ticker, "quarterly", date)),
        ("get_cashflow", (ticker, "quarterly", date)),
        ("get_insider_transactions", (ticker, date)),
        ("get_estimate_revisions", (ticker, date)),
        ("get_upcoming_earnings", (ticker, date)),
        ("get_company_guidance", (ticker, date)),
        ("get_analyst_price_targets", (ticker, date)),
        ("get_industry_pe", (ticker, date)),
        ("get_company_pe_history", (ticker, date)),
    ]
    calls = []

    def fetch(method, *args):
        calls.append((method, args))
        if method == "get_balance_sheet":
            raise RuntimeError("vendor failed")
        if method == "get_insider_transactions":
            return (
                "# Insider Transactions data for NVDA\n"
                "Start Date,Text,Transaction,Insider,Shares\n"
                "2025-01-15,Sale at price 100 per share.,,Boundary Seller,123\n"
                "2025-01-14,Sale at price 100 per share.,,Old Seller,456\n"
                "2026-01-15,Sale at price 100 per share.,,Latest Seller,789\n"
                "2026-01-16,Sale at price 100 per share.,,Future Seller,456\n"
                "2026-01-14,Stock Gift at price 0 per share.,,Donor,456\n"
                "2026-01-14,Purchase at price 100 per share.,,Buyer,456\n"
                "2026-01-14,Stock Award(Grant) at price 0 per share.,,Recipient,456\n"
            )
        if method == "get_fundamentals":
            return "PE Ratio (TTM): 21.01\nForward PE: 10.49"
        if method == "get_industry_pe":
            return {"median_trailing_pe": 46.53, "company_trailing_pe": 21.01,
                    "company_vs_peer_median_pct": -54.85}
        if method == "get_company_pe_history":
            return {"mean_trailing_pe": 46.77, "company_trailing_pe": 21.96,
                    "company_vs_history_mean_pct": -53.04}
        if method == "get_estimate_revisions":
            return {"periods": [{"period": "0q", "period_end": "2026-01-31",
                                 "revenue": {"avg": 103, "currency": "USD"}}]}
        if method == "get_company_guidance":
            return {"guidance": [{"fiscal_period": "FY2026-Q4", "period_type": "quarter",
                                  "period_ends": ["2026-01-31"], "metric": "revenue",
                                  "currency": "USD", "low": 98, "high": 102}]}
        return f"{method} first row\n{method} last row"

    def write_report(messages):
        assert calls == expected
        return AIMessage(content="### Şirket Özeti ve Değerleme\nRapor.")

    monkeypatch.setattr(tools, "route_to_vendor", fetch)
    llm = MagicMock()
    llm.invoke.side_effect = write_report
    with run_config({"output_language": "Turkish"}):
        state = fundamentals.create_fundamentals_analyst(llm)({
            "company_of_interest": ticker,
            "trade_date": date,
        })

    llm.invoke.assert_called_once()
    llm.bind_tools.assert_not_called()
    system, data = llm.invoke.call_args.args[0]
    assert NO_EXTERNAL_TOOLS in system.content
    assert "eight level-three headings" in system.content
    assert "Turkish" in system.content
    assert date in data.content and ticker in data.content
    for method, _ in expected:
        if method in {"get_fundamentals", "get_estimate_revisions", "get_company_guidance", "get_upcoming_earnings",
                      "get_industry_pe", "get_company_pe_history", "get_insider_transactions"}:
            continue
        body = ("Data could not be retrieved." if method == "get_balance_sheet"
                else f"{method} first row\n{method} last row")
        assert f"### {method}\n{body}" in data.content
    assert "### get_estimate_revisions\n" in data.content
    assert "### get_upcoming_earnings_and_guidance\n" in data.content
    assert "### get_valuation_benchmarks\n" in data.content
    assert "Insider Sales in the Last Year" in system.content
    assert "Reported insider sales, 2025-01-15 to 2026-01-15 (inclusive)" in data.content
    assert "Boundary Seller" in data.content and "Latest Seller" in data.content
    assert not any(name in data.content for name in ("Old Seller", "Future Seller", "Donor", "Buyer", "Recipient"))
    assert "PE Ratio (TTM): 21.01" in data.content
    assert '"company_vs_peer_median_pct": -54.85' in data.content
    assert '"company_vs_history_mean_pct": -55.08' in data.content
    assert "21.96" not in data.content
    assert '"position": "above", "difference_vs_range": 1' in data.content
    assert state["fundamentals_report"] == "### Şirket Özeti ve Değerleme\nRapor."


@pytest.mark.unit
@pytest.mark.parametrize(("source", "expected"), [
    (
        "# Insider Transactions data for NVDA\nStart Date,Text\n2026-01-14,Stock Gift at price 0\n",
        "No sales found in the provider's available records.",
    ),
    (
        '{"data":[{"transaction_date":"2026-01-14","acquisition_or_disposal":"D"}]}',
        "Sales could not be verified from the provider's available data.",
    ),
    ("DATA_UNAVAILABLE: provider unavailable", "DATA_UNAVAILABLE: provider unavailable"),
])
def test_fundamentals_insider_sales_distinguishes_empty_and_unavailable(source, expected):
    from tradingagents.agents.analysts.fundamentals_analyst import _insider_sales

    assert _insider_sales(source, "2026-01-15").endswith(expected)


@pytest.mark.unit
@pytest.mark.parametrize(("horizon", "keeps_year_ago"), [
    ("short_1m", False), ("medium_3m", True), ("long_12m", True),
])
def test_fundamentals_limits_sec_statement_history_by_horizon(monkeypatch, horizon, keeps_year_ago):
    from langchain_core.messages import AIMessage

    from tradingagents.agents import tools
    from tradingagents.agents.analysts.fundamentals_analyst import create_fundamentals_analyst

    header = "# SEC EDGAR facts filed on or before 2026-11-13, at the values filed then"
    statement = (
        f"# Statement, USD in millions\n{header}\n\nPeriod,Value\n"
        "2025-06-25,10\n2025-06-26,20\n2025-07-27 (6 months),30\n"
        "2025-12-25,40\n2025-12-26,50\n2026-07-26 (6 months),60\n"
    )
    methods = {"get_income_statement", "get_balance_sheet", "get_cashflow"}
    monkeypatch.setattr(tools, "route_to_vendor",
                        lambda method, *_: statement if method in methods else "Other data")
    llm = MagicMock()
    llm.invoke.return_value = AIMessage(content="Report.")
    create_fundamentals_analyst(llm)({
        "company_of_interest": "NVDA", "trade_date": "2026-11-13", "analysis_horizon": horizon,
    })

    llm.invoke.assert_called_once()
    _, evidence = llm.invoke.call_args.args[0]
    assert evidence.content.count(header) == 3
    assert evidence.content.count("2026-07-26 (6 months),60") == 3
    assert evidence.content.count("2025-12-26,50") == 3
    assert evidence.content.count("2025-07-27 (6 months),30") == (3 if keeps_year_ago else 0)
    assert evidence.content.count("2025-06-26,20") == (3 if keeps_year_ago else 0)
    assert "2025-06-25,10" not in evidence.content


@pytest.mark.unit
def test_news_analyst_keeps_its_tool_date_guidance():
    import tradingagents.agents.analysts.news_analyst as news

    assert "tool-call date ranges" in inspect.getsource(news)


@pytest.mark.unit
def test_constraint_text_is_unambiguous():
    assert "do not call external tools" in NO_EXTERNAL_TOOLS.lower()
    # No template braces: it is embedded in ChatPromptTemplate strings, where
    # braces would be parsed as input variables.
    assert "{" not in NO_EXTERNAL_TOOLS and "}" not in NO_EXTERNAL_TOOLS


@pytest.mark.unit
def test_the_sentiment_prompt_names_the_subreddits_searched(monkeypatch):
    from tradingagents.agents.schemas import SentimentBand, SentimentReport

    monkeypatch.setattr(sentiment, "fetch_stocktwits_messages", lambda *a, **k: "st")
    monkeypatch.setattr(sentiment, "fetch_reddit_posts", lambda *a, **k: "rd")
    monkeypatch.setattr(sentiment.get_news, "func", lambda *a, **k: "news", raising=False)
    captured = {}
    llm = _capturing_llm(captured, SentimentReport(
        overall_band=SentimentBand.NEUTRAL, overall_score=5.0, confidence="low", narrative="n",
    ))
    sentiment.create_sentiment_analyst(llm)({
        "company_of_interest": "BTC-USD", "trade_date": "2026-01-15",
        "asset_type": "crypto", "messages": [],
    })
    text = _prompt_text(captured["prompt"])
    assert "r/Bitcoin, r/CryptoCurrency, r/CryptoMarkets" in text
    assert "wallstreetbets" not in text
