import csv
import json
import logging
import re
from calendar import monthrange
from datetime import date
from io import StringIO

from langchain_core.messages import HumanMessage, SystemMessage

from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.structured import NO_EXTERNAL_TOOLS
from tradingagents.agents.tools import (
    get_analyst_price_targets,
    get_balance_sheet,
    get_cashflow,
    get_estimate_revisions,
    get_fundamentals,
    get_income_statement,
    get_insider_transactions,
    get_upcoming_earnings_and_guidance,
    get_valuation_benchmarks,
)
from tradingagents.dataflows.outlook import compare_consensus_to_guidance
from tradingagents.dataflows.vendors.yahoo.valuation import compare_pe_benchmarks
from tradingagents.horizons import HORIZON_CONFIG, parse_analysis_horizon

logger = logging.getLogger(__name__)
_SEC_PERIOD_ROW = re.compile(r"^(\d{4}-\d{2}-\d{2})(?: \(\d+ months\))?,")
_SALE = re.compile(r"^(?:sale|sell|sold)\b", re.IGNORECASE)
_TRANSACTION_FIELDS = ("transaction_type", "transaction_code", "transaction_description")


def _months_before(day, months):
    year, month_index = divmod(day.year * 12 + day.month - 1 - months, 12)
    month = month_index + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _insider_sales(data, trade_date):
    """Return explicitly identified sales within the analysis date's trailing year."""
    end = date.fromisoformat(trade_date)
    start = _months_before(end, 12)
    scope = f"Reported insider sales, {start} to {end} (inclusive)"

    def in_scope(row, date_field, type_fields):
        try:
            transaction_date = date.fromisoformat(row.get(date_field, ""))
        except (TypeError, ValueError):
            return False
        return start <= transaction_date <= end and any(
            _SALE.match(str(row.get(field) or "").strip()) for field in type_fields
        )

    if isinstance(data, str):
        if data.startswith(("DATA_UNAVAILABLE:", "NO_DATA_AVAILABLE:", "No insider transactions reported",
                            "Data could not be retrieved.", "No data returned.")) or (
            data.startswith("# Insider Transactions for ") and "transactions are withheld" in data
        ):
            return f"{scope}\n{data}"
        header, _, body = data.partition("\n")
        if header.startswith("# Insider Transactions data for "):
            reader = csv.DictReader(StringIO(body))
            fields = reader.fieldnames or []
            if "Start Date" in fields and {"Text", "Transaction"}.intersection(fields):
                sales = [row for row in reader if in_scope(row, "Start Date", ("Text", "Transaction"))]
                if not sales:
                    return f"{scope}\nNo sales found in the provider's available records."
                output = StringIO()
                fields = [field for field in fields if field and any(row.get(field) for row in sales)]
                writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(sales)
                return f"{scope}\n{output.getvalue()}"
        try:
            data = json.loads(data)
        except ValueError:
            data = None

    if isinstance(data, dict) and isinstance(rows := data.get("data"), list):
        sales = [row for row in rows if isinstance(row, dict) and in_scope(
            row, "transaction_date", _TRANSACTION_FIELDS,
        )]
        if sales:
            return f"{scope}\n{json.dumps(sales, ensure_ascii=False)}"
        if not rows or any(isinstance(row, dict) and any(
            row.get(field) for field in _TRANSACTION_FIELDS
        ) for row in rows):
            return f"{scope}\nNo sales found in the provider's available records."
    return f"{scope}\nSales could not be verified from the provider's available data."


def _limit_statement_history(data, months):
    """Keep recent SEC period rows and their original headers and units."""
    if not isinstance(data, str) or "\n# SEC EDGAR facts filed on or before " not in data:
        return data
    lines = data.splitlines(keepends=True)
    periods = {
        index: date.fromisoformat(match[1])
        for index, line in enumerate(lines)
        if (match := _SEC_PERIOD_ROW.match(line))
    }
    if not periods:
        return data
    latest = max(periods.values())
    cutoff = _months_before(latest, months)
    return "".join(
        line for index, line in enumerate(lines)
        if index not in periods or periods[index] >= cutoff
    )


def create_fundamentals_analyst(llm):
    def fundamentals_analyst_node(state):
        ticker = state["company_of_interest"]
        trade_date = state["trade_date"]
        horizon = parse_analysis_horizon(state.get("analysis_horizon", "medium_3m"))
        months = HORIZON_CONFIG[horizon]["fundamentals_lookback_months"]
        instrument_context = get_instrument_context_from_state(state)
        dated_args = {"ticker": ticker, "curr_date": trade_date, "trade_date": trade_date}
        sources = (
            (get_fundamentals, dated_args),
            (get_income_statement, dated_args),
            (get_balance_sheet, dated_args),
            (get_cashflow, dated_args),
            (get_insider_transactions, {"ticker": ticker, "trade_date": trade_date}),
            (get_estimate_revisions, dated_args),
            (get_upcoming_earnings_and_guidance, dated_args),
            (get_analyst_price_targets, dated_args),
            (get_valuation_benchmarks, dated_args),
        )
        data_by_source = {}
        for tool, args in sources:
            try:
                data = tool.func(**args)
            except Exception as exc:
                # The router can raise when every configured vendor fails.
                logger.warning("Fundamentals Analyst could not fetch %s (%s)", tool.name, type(exc).__name__)
                data = "Data could not be retrieved."
            data_by_source[tool.name] = data or "No data returned."

        for tool in (get_income_statement, get_balance_sheet, get_cashflow):
            data_by_source[tool.name] = _limit_statement_history(data_by_source[tool.name], months)
        insider_key = get_insider_transactions.name
        data_by_source[insider_key] = _insider_sales(data_by_source[insider_key], trade_date)

        outlook = data_by_source[get_upcoming_earnings_and_guidance.name]
        if isinstance(outlook, dict):
            outlook["consensus_comparison"] = compare_consensus_to_guidance(
                data_by_source[get_estimate_revisions.name], outlook,
            )
        benchmarks = data_by_source[get_valuation_benchmarks.name]
        if isinstance(benchmarks, dict):
            data_by_source[get_valuation_benchmarks.name] = compare_pe_benchmarks(
                data_by_source[get_fundamentals.name], benchmarks,
            )
        evidence = [
            f"### {name}\n{data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)}"
            for name, data in data_by_source.items()
        ]

        system_message = (
            "You are the Fundamentals Analyst. Write a concise Markdown report for the research team. "
            "Use exactly eight level-three headings in this order: Company Overview and Valuation, "
            "Income Statement, Balance Sheet, Cash Flow, Insider Sales in the Last Year, EPS and Revenue Revisions, "
            "Upcoming Earnings and Guidance, and Analyst Price Targets. "
            "Translate the headings into the report language. Use the corresponding source block "
            "for each section. Give key figures with their periods and units once. Use compact bullets "
            "or a small table for comparisons. Add a sentence after them only if it contributes a new "
            "comparison or implication. State a data gap briefly when it prevents a requested finding. "
            "Include get_valuation_benchmarks under Company Overview and Valuation. Briefly give the company "
            "historical trailing P/E mean and median with the date range and observation count. Identify the "
            "industry and exchange or market. Label FMP's aggregate as published and use only supplied percentage "
            "comparisons. Label a Yahoo sample as selected peers and state its count, mean and median. "
            "When either industry benchmark is available, report the supplied benchmark without discussing the other provider. "
            "Use the company overview's trailing P/E as the single current trailing P/E for both benchmarks. "
            "In Income Statement, compare the latest standalone quarter with the same quarter last year "
            "when both are supplied. Otherwise use the most recent comparable standalone quarter if supplied. "
            "When both comparisons are possible, include the sequential comparison only if it adds a distinct finding. "
            "In Balance Sheet, use "
            "the current ratio to summarize liquidity when supported. In Cash Flow, compare like reported "
            "periods, label year-to-date figures, and give free cash flow when supported. Use reported periods "
            "for cash flow and omit standalone quarters derived by subtracting cumulative totals. "
            "For insider sales, state the trailing one-year window ending on the analysis date and summarize "
            "the most material reported sales. If the provider returned no sales or coverage was unavailable, "
            "state that limitation in this section. "
            "For revisions, focus on the nearest estimate quarter and current fiscal year. Combine the 7- and "
            "30-day results when they match and use the actual observation dates for revenue revisions. "
            "For upcoming earnings, distinguish an estimated date or date range from a confirmed date. "
            "For company guidance, cite its filing date and source link. Use only the supplied "
            "consensus_comparison for comparisons with guidance, in one sentence without repeating the inputs. "
            "Do not expose internal field names or present inferred fiscal period candidates as dates stated "
            "by the company. If there is no comparable consensus, say so briefly in plain language. "
            "For Yahoo aggregate targets, give the median, mean, range and any supplied difference from the "
            "reference price in one line. Use the supplied currency and attribute the data to Yahoo. "
            "Omit the introduction, closing summary, summary table, and trade recommendations. "
            + NO_EXTERNAL_TOOLS
            + get_language_instruction(preserve_decision_labels=False)
        )
        source_data = "\n\n".join(evidence)
        result = llm.invoke([
            SystemMessage(content=system_message),
            HumanMessage(content=f"Analysis date: {trade_date}\n{instrument_context}\n\n{source_data}"),
        ])

        return {
            "messages": [result],
            "fundamentals_report": result.text,
        }

    return fundamentals_analyst_node
