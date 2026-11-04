"""Measurement ownership, usage gaps and persistence without network calls."""

from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from tradingagents.agent_stats import AgentStats, AgentStatsHandler, render_agent_stats, track_data
from tradingagents.reporting import write_report_tree


def node(handler, name):
    run = uuid4()
    handler.on_chain_start({}, {}, run_id=run, metadata={"langgraph_node": name})
    return run


def test_usage_deduplication_parallel_ownership_and_fallback():
    h = AgentStatsHandler()
    market, news = node(h, "Market Analyst"), node(h, "News Analyst")
    a, b = uuid4(), uuid4()
    h.on_chat_model_start({}, [], run_id=a, parent_run_id=market)
    h.on_chat_model_start({}, [], run_id=a, parent_run_id=market)
    h.on_chat_model_start({}, [], run_id=b, parent_run_id=news)
    h.on_llm_end(
        LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content="x",
                            usage_metadata={
                                "input_tokens": 120,
                                "output_tokens": 20,
                                "total_tokens": 140,
                            },
                        )
                    )
                ]
            ]
        ),
        run_id=a,
    )
    h.on_llm_end(
        LLMResult(
            generations=[],
            llm_output={"token_usage": {"prompt_tokens": 80, "completion_tokens": 10}},
        ),
        run_id=b,
    )
    h.on_chain_end({}, run_id=market)
    h.on_chain_end({}, run_id=news)
    rows = {r["agent"]: r for r in h.snapshot()}
    assert rows["Market Analyst"]["llm_calls"] == 1
    assert rows["Market Analyst"]["tokens_in"] == 120
    assert rows["News Analyst"]["tokens_in"] == 80
    assert rows["News Analyst"]["tokens_out"] == 10
    assert all(r["usage_missing"] == 0 for r in rows.values())


def test_errors_repeated_runs_missing_usage_and_handled_tool_error():
    h = AgentStatsHandler()
    for failed in (False, True):
        owner, call, tool = node(h, "Bull Researcher"), uuid4(), uuid4()
        h.on_chat_model_start({}, [], run_id=call, parent_run_id=owner)
        h.on_tool_start({"name": "get_news"}, "secret", run_id=tool, parent_run_id=owner)
        h.on_tool_end(ToolMessage(content="secret", tool_call_id="x", status="error"), run_id=tool)
        if failed:
            h.on_llm_error(RuntimeError("secret"), run_id=call)
            h.on_chain_error(RuntimeError("secret"), run_id=owner)
        else:
            h.on_llm_end(LLMResult(generations=[]), run_id=call)
            h.on_chain_end({}, run_id=owner)
    (row,) = h.snapshot()
    assert row["status"] == "error"
    assert row["llm_calls"] == row["usage_missing"] == 2
    assert row["tools"] == {"get_news": 2}
    assert len(row["runs"]) == 2
    assert "secret" not in str(row)
    assert "+ ?" in render_agent_stats([row])
    AgentStats.model_validate(row)


def test_data_decorator_is_transparent_without_graph_context():
    @track_data("test")
    def read(value):
        return value

    assert read(7) == 7


def test_stats_are_inside_final_decision_in_markdown_and_html(tmp_path):
    h = AgentStatsHandler()
    owner = node(h, "Market Analyst")
    h.on_chain_end({}, run_id=owner)
    state = {"final_trade_decision": "DECISION", "agent_stats": h.snapshot()}
    path = write_report_tree(state, "TEST", tmp_path)
    markdown = path.read_text()
    html = (tmp_path / "complete_report.html").read_text()
    assert markdown.index("DECISION") < markdown.index("Agent Execution Statistics")
    assert html.index("DECISION") < html.rindex("Agent Execution Statistics")
    assert "Market Analyst" in markdown and "Market Analyst" in html
    decision = (tmp_path / "5_portfolio" / "decision.md").read_text()
    assert decision.startswith("DECISION\n\n#### Agent Execution Statistics")
    write_report_tree({"final_trade_decision": "DECISION"}, "TEST", tmp_path)
    assert (tmp_path / "5_portfolio" / "decision.md").read_text() == "DECISION"


def test_direct_data_access_uses_current_nested_graph_owner():
    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict

    class State(TypedDict):
        value: int

    @track_data("reddit")
    def fetch():
        return 7

    def sentiment(state):
        return {"value": fetch()}

    graph = StateGraph(State)
    graph.add_node("Sentiment Analyst", sentiment)
    graph.add_edge(START, "Sentiment Analyst")
    graph.add_edge("Sentiment Analyst", END)
    h = AgentStatsHandler()
    assert graph.compile().invoke({"value": 0}, config={"callbacks": [h]}) == {"value": 7}
    row, = h.snapshot()
    assert row["data_accesses"] == {"reddit": 1}
    assert row["tools"] == {}


def test_terminal_full_report_ends_with_agent_statistics(monkeypatch):
    from io import StringIO

    from rich.console import Console

    from cli import display

    output = StringIO()
    monkeypatch.setattr(display, "console", Console(file=output, width=160, color_system=None))
    h = AgentStatsHandler()
    owner = node(h, "Market Analyst")
    h.on_chain_end({}, run_id=owner)
    display.display_complete_report(
        {"final_trade_decision": "FINAL_DECISION", "agent_stats": h.snapshot()}, language="tr"
    )
    text = output.getvalue()
    assert text.index("FINAL_DECISION") < text.index("Agent Çalışma İstatistikleri")
    assert "Market Analyst" in text
