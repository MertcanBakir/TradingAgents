"""Run the Market Analyst's indicator selection and report model calls."""

import json

from langchain_core.messages import HumanMessage, SystemMessage

from tradingagents.agent_stats import track_data
from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo.candles import latest_daily_candle
from tradingagents.dataflows.vendors.yahoo.snapshot import load_verified_market_rows
from tradingagents.horizons import HORIZON_CONFIG, horizon_context, parse_analysis_horizon
from tradingagents.technical_analysis.indicators import calculate_indicators, profile_indicators
from tradingagents.technical_analysis.reporting import (
    indicator_context,
    price_context,
    render_market_report,
    render_zones,
    unavailable_report,
)
from tradingagents.technical_analysis.zones import calculate_scipy_zones

MAX_GROUPS = 8


@track_data("market_price_history")
def _load_prices(symbol, date, *, history_rows, interval):
    return load_verified_market_rows(symbol, date, history_rows=history_rows, interval=interval)


def _selection(text, optional, required):
    selected = list(required)
    notes = []
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.partition("\n")[2][:-3].strip()
    try:
        items = json.loads(text)["groups"]
    except (ValueError, KeyError, TypeError):
        return selected, ["No readable optional indicator group selection."]
    if not isinstance(items, list):
        return selected, ["No readable optional indicator group selection."]
    invalid_count = 0
    overflow = False
    for item in items:
        if not isinstance(item, str) or item not in optional:
            invalid_count += 1
        elif item not in selected:
            if len(selected) < MAX_GROUPS:
                selected.append(item)
            else:
                overflow = True
    if invalid_count:
        notes.append(f"{invalid_count} requested indicator groups are outside the allowed list.")
    if overflow:
        notes.append("Additional indicator groups omitted because the limit is eight.")
    if not any(group not in required for group in selected):
        notes.append("No optional indicator groups selected.")
    return selected, notes


def create_market_analyst(llm):
    def market_analyst_node(state):
        date = state["trade_date"]
        horizon = parse_analysis_horizon(state.get("analysis_horizon"))
        profile = HORIZON_CONFIG[horizon]
        history_rows = profile["history_rows"]
        target_days = profile["target_days"]
        language = get_config().get("output_language", "English")
        try:
            rows = _load_prices(
                state["company_of_interest"], date,
                history_rows=history_rows, interval=profile["primary_timeframe"],
            )
        except NoMarketDataError:
            report = unavailable_report("no_market_data", language)
            return {"messages": [], "market_report": report}
        except VendorUnavailableError:
            report = unavailable_report("provider_unavailable", language)
            return {"messages": [], "market_report": report}

        if rows is None or rows.empty:
            report = unavailable_report("no_market_data", language)
            return {"messages": [], "market_report": report}
        candle = latest_daily_candle(rows)
        latest_price = rows["Close"].iloc[-1]
        completed = rows if candle.closed is True else rows.iloc[:-1]
        if completed.empty:
            report = unavailable_report("no_completed_candle", language, candle, latest_price)
            return {"messages": [], "market_report": report}
        missing = {column: float("nan") for column in ("Open", "High", "Low", "Volume") if column not in completed}
        if missing:
            completed = completed.assign(**missing)

        context = f"{get_instrument_context_from_state(state)}\n{horizon_context(horizon)}"
        groups = profile_indicators(profile)
        required = profile["required_groups"]
        optional = {
            group: ", ".join(spec.display_label for spec in specs)
            for group, specs in groups.items() if group not in required
        }

        selection = llm.invoke([
            SystemMessage(content=(
                f"You are the Market Analyst. The required groups are {', '.join(required)}. "
                f"Select up to {MAX_GROUPS - len(required)} optional groups that complement them "
                "based on the supplied completed daily history. Return only a JSON object with a "
                "groups array containing exact optional group codes. Optional groups and components: "
                + "; ".join(
                    f"{group}: {description}" for group, description in optional.items()
                )
            )),
            HumanMessage(content=f"Analysis date: {date}\n{context}\n{price_context(completed, target_days)}"),
        ])
        selected, notes = _selection(selection.text, optional, required)
        selected_groups = {group: groups[group] for group in selected}
        calculated = calculate_indicators(completed, selected_groups, target_days)
        indicator_lines = indicator_context(completed, selected_groups, calculated)
        atr = calculated.get(groups["atr"][0].name)
        zones = calculate_scipy_zones(
            completed, profile["zone_lookback"], atr.values if atr is not None else None,
        )
        if len(completed) < history_rows:
            notes.append(f"Only {len(completed)} of {history_rows} requested daily bars are confirmed complete.")
        evidence = "\n".join([
            f"Calculations used {len(completed)} completed daily bars. "
            "The following price table and the indicator series are recent excerpts.",
            "Completed daily OHLCV only:", price_context(completed, 20),
            *indicator_lines, *(["SciPy price zones:", render_zones(zones)] if zones else []), *notes,
        ])
        indicator_headings = ", ".join(f"### {name}" for name in selected_groups) or "none"
        result = llm.invoke([
            SystemMessage(content=(
                "You are the Market Analyst. Write a concise technical report in Markdown. "
                "Use these sections in order: Price and Volume, Selected Indicators, "
                "Technical Outlook. Translate the section titles into the report language. "
                "Start Price and Volume directly with the observed price and volume changes. "
                "Use dates to identify the price or volume observations you discuss. "
                "Omit any introductory recap of the ticker, analysis date, horizon or candle metadata. "
                "The candle status and timestamp appear in a separate block. Use only completed daily bars. "
                "Under Selected Indicators, use one level-three heading per selected group "
                f"in this exact order: {indicator_headings}. Keep the group codes unchanged. "
                "For each group, give its latest values and significance or explicitly mark unavailable evidence. "
                "Interpret supplied ATR %, Price-to-MA % and MA Slope % alongside their raw indicators. "
                "Explain where the supplied indicators agree or conflict, citing the provided numerical comparisons. "
                "When supplied, interpret support and resistance as price zones. "
                "The dedicated zone section lists exact ranges, formation dates and total contacts. "
                "Discuss only the broader implication of the zones in the prose. Avoid repeating those records "
                "or presenting zones as price targets. "
                "State data limitations and avoid inventing contacts. "
                "Use completed daily prices and available indicators to assess observed moves. "
                "Keep Technical Outlook to the observed technical picture without scenario sections. "
                "Keep the report descriptive. Omit ratings, transaction proposals, buy/sell instructions, "
                "price targets and odds. "
                "Use only the supplied dated data for numerical claims and do not infer "
                "crossovers or historical price behavior without supporting rows. "
                "Do not include a summary table or repeat the indicator list in the outlook. "
                "Frame the outlook for the selected analysis horizon. "
                + get_language_instruction(preserve_decision_labels=False)
            )),
            HumanMessage(content=f"Analysis date: {date}\n{context}\n{evidence}"),
        ])
        report = render_market_report(
            result.text, candle, completed, latest_price, zones, language=language,
        )
        return {"messages": [result], "market_report": report}

    return market_analyst_node
