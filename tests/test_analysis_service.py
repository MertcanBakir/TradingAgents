"""Phase-1 acceptance: fake graph boundaries, real SQLite, no provider calls."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from tradingagents.service.cli import main
from tradingagents.service.config import build_graph_config
from tradingagents.service.engine import AnalysisEngine
from tradingagents.service.errors import AnalysisServiceError, classify_error
from tradingagents.service.memory import (
    BoundMemory,
    MemoryDecision,
    MemoryOutcome,
    SQLiteMemoryRepository,
)
from tradingagents.service.models import AnalysisRequest, LLMChoice, ServiceSettings
from tradingagents.service.normalize import normalize_result

USER_A = UUID("11111111-1111-4111-8111-111111111111")
USER_B = UUID("22222222-2222-4222-8222-222222222222")


@pytest.fixture
def request_data():
    return {"ticker": "NVDA", "analysis_date": "2026-10-17"}


@pytest.fixture
def settings():
    return ServiceSettings(
        quick=LLMChoice(provider="chatgpt", model="gpt-5.6-sol"),
        deep=LLMChoice(provider="chatgpt", model="gpt-5.6-sol"),
    )


@pytest.fixture
def state():
    return {
        "company_of_interest": "NVDA",
        "trade_date": "2026-10-17",
        "market_report": "Technical evidence",
        "fundamentals_report": "Fundamental evidence",
        "news_report": "News evidence",
        "sentiment_report": "Sentiment evidence",
        "investment_debate_state": {
            "bull_history": "Bull evidence",
            "bear_history": "Bear evidence",
        },
        "investment_plan": "Research-manager rationale",
        "trader_investment_plan": "**Action**: Buy\n\n**Reasoning**: Measured entry.",
        "risk_debate_state": {
            "aggressive_history": "Aggressive evidence",
            "conservative_history": "Conservative evidence",
            "neutral_history": "Neutral evidence",
        },
        "final_trade_decision": "**Rating**: Overweight\n\n**Executive Summary**: Staged entry.\n\n**Investment Thesis**: Evidence, not certainty.",
        "final_rating": "Overweight",
    }


def decision(user=USER_A, ticker="NVDA", **changes):
    data = {
        "analysis_id": uuid4(),
        "user_id": user,
        "ticker": ticker,
        "analysis_date": date(2026, 1, 1),
        "rating": "Overweight",
        "decision": f"Evidence for {ticker}",
        "created_at": datetime(2026, 1, 1, 12, tzinfo=UTC),
        "evaluation_trading_days": 5,
        "benchmark": "SPY",
    }
    return MemoryDecision(**(data | changes))


def outcome(**changes):
    data = {
        "raw_return": 0.04,
        "benchmark_return": 0.01,
        "entry_date": date(2026, 1, 2),
        "exit_date": date(2026, 1, 9),
        "observed_trading_days": 5,
        "reflection": "LESSON_A: short windows do not prove long theses.",
        "resolved_at": datetime(2026, 1, 10, tzinfo=UTC),
    }
    return MemoryOutcome(**(data | changes))


def normalize(state, request_data, settings):
    now = datetime.now(UTC)
    return normalize_result(
        state=state,
        request=AnalysisRequest(**request_data),
        settings=settings,
        analysis_id=uuid4(),
        started_at=now,
        finished_at=now,
        duration_seconds=1.2,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"ticker": ""},
        {"ticker": "../../etc/passwd"},
        {"ticker": "A..B"},
        {"ticker": "NVDA;DROP"},
        {"analysis_date": "2026-02-30"},
        {"analysis_date": "2039-01-01"},
        {"analysis_date": "2026-01-01T00:00:00Z"},
        {"analysis_date": 0},
        {"analysts": []},
        {"analysts": ["market", "market"]},
        {"analysts": ["unknown"]},
        {"profile": "unlimited"},
        {"language": "unknown"},
        {"user_id": str(USER_B)},
        {"config": {"api_key": "secret"}},
        {"backend_url": "http://localhost"},
        {"memory": ["fake"]},
    ],
)
def test_request_fail_closed(request_data, change):
    with pytest.raises(ValidationError):
        AnalysisRequest(**(request_data | change))


@pytest.mark.parametrize("ticker", ["NVDA", "THYAO.IS", "BRK-B", "0700.HK"])
def test_ticker_contract(request_data, ticker):
    assert AnalysisRequest(**(request_data | {"ticker": ticker.lower()})).ticker == ticker


def test_server_config_is_bounded_and_deeply_isolated(
    request_data, settings, tmp_path, monkeypatch
):
    from tradingagents.default_config import DEFAULT_CONFIG

    monkeypatch.setitem(DEFAULT_CONFIG, "max_debate_rounds", 5)
    monkeypatch.setitem(DEFAULT_CONFIG, "memory_log_path", "/private/user-memory.md")
    monkeypatch.setitem(DEFAULT_CONFIG, "backend_url", "https://password:secret@example.com")
    before = deepcopy(DEFAULT_CONFIG)
    a = build_graph_config(AnalysisRequest(**request_data), settings, tmp_path / "a")
    b = build_graph_config(AnalysisRequest(**request_data), settings, tmp_path / "b")
    a["data_vendors"]["news_data"] = "changed"
    assert b["data_vendors"] != a["data_vendors"]
    assert before == DEFAULT_CONFIG
    assert a["max_debate_rounds"] == a["max_risk_discuss_rounds"] == 1
    assert a["backend_url"] is a["memory_log_path"] is None
    assert a["checkpoint_enabled"] is False
    assert a["results_dir"] != b["results_dir"]


def test_server_settings_reject_secret_endpoint_and_unbounded_retries(settings):
    for values in ({"llm_max_retries": 200}, {"api_key": "secret"}, {"max_tool_rounds": 999}):
        with pytest.raises(ValidationError):
            ServiceSettings(**(settings.model_dump() | values))
    with pytest.raises(ValidationError):
        LLMChoice(provider="chatgpt", model="https://attacker.example")


def test_twelve_cards_rating_action_and_summary_are_distinct(state, request_data, settings):
    result = normalize(state, request_data, settings)
    assert len(result.sections) == len({s.code for s in result.sections}) == 12
    assert result.final_rating == "Overweight" and result.trade_action == "Buy"
    assert result.quality == "complete"
    assert result.sections[0].content_markdown == "Staged entry."
    assert result.sections[8].parts["neutral"] == "Neutral evidence"
    assert result.sections[7].parts["research_manager"] == "Research-manager rationale"
    output = result.model_dump_json()
    assert "messages" not in output and "user_id" not in output and "backend_url" not in output


def test_missing_selected_analyst_is_partial_and_null(state, request_data, settings):
    state["news_report"] = ""
    result = normalize(state, request_data, settings)
    assert result.quality == "partial"
    assert "MISSING_SECTION_NEWS" in result.warning_codes
    assert next(s for s in result.sections if s.code == "news").content_markdown is None


def test_unselected_analyst_is_not_invented_or_required(state, request_data, settings):
    state["news_report"] = None
    request_data["analysts"] = ["market", "social", "fundamentals"]
    assert normalize(state, request_data, settings).quality == "complete"


def test_missing_summary_is_not_fabricated(state, request_data, settings):
    state["final_trade_decision"] = "**Rating**: Overweight\nNo structured summary."
    result = normalize(state, request_data, settings)
    assert result.sections[0].content_markdown is None and result.quality == "partial"


@pytest.mark.parametrize("rating", [None, "unexpected", "REVIEW", "Sell"])
def test_ambiguous_rating_never_silently_becomes_hold(state, request_data, settings, rating):
    state["final_rating"] = rating
    state["final_trade_decision"] = "No parseable rating or summary."
    if rating == "Sell":
        state["final_trade_decision"] = "**Rating**: Buy\n\n**Executive Summary**: Conflict."
    assert normalize(state, request_data, settings).final_rating == "REVIEW"


@pytest.mark.parametrize(
    "change,code",
    [
        ({"final_trade_decision": ""}, "MISSING_FINAL_DECISION"),
        ({"company_of_interest": "AAPL"}, "RESULT_IDENTITY_MISMATCH"),
        ({"trade_date": "2026-09-30"}, "RESULT_IDENTITY_MISMATCH"),
    ],
)
def test_invalid_graph_result_fails_closed(state, request_data, settings, change, code):
    with pytest.raises(AnalysisServiceError, match=code):
        normalize(state | change, request_data, settings)


def test_report_text_redacts_secrets(state, request_data, settings, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-private-test-credential-99999")
    state["news_report"] = (
        "sk-private-test-credential-99999 postgresql://bob:pw@example.com/db Bearer abc.123"
    )
    text = normalize(state, request_data, settings).model_dump_json()
    assert "private-test" not in text and "bob:pw" not in text and "abc.123" not in text


def test_memory_is_durable_user_scoped_and_pending_is_not_a_lesson(tmp_path):
    path = tmp_path / "memory.sqlite3"
    repo = SQLiteMemoryRepository(path)
    item = decision()
    assert repo.save_decision(item)
    assert not repo.save_decision(item)
    assert repo.get_context(user_id=USER_A, ticker="NVDA", as_of=datetime.now(UTC)) == ""
    assert not repo.resolve(user_id=USER_B, analysis_id=item.analysis_id, outcome=outcome())
    assert repo.resolve(user_id=USER_A, analysis_id=item.analysis_id, outcome=outcome())
    assert not repo.resolve(user_id=USER_A, analysis_id=item.analysis_id, outcome=outcome())
    reopened = SQLiteMemoryRepository(path)
    context = reopened.get_context(user_id=USER_A, ticker="NVDA", as_of=datetime.now(UTC))
    assert "LESSON_A" in context and "+3.00%" in context
    assert reopened.get_context(user_id=USER_B, ticker="NVDA", as_of=datetime.now(UTC)) == ""
    assert path.stat().st_mode & 0o777 == 0o600


def test_memory_point_in_time_and_bounded_same_cross_retrieval(tmp_path):
    repo = SQLiteMemoryRepository(tmp_path / "m.sqlite3")
    for ticker in ["NVDA"] * 8 + ["AAPL"] * 6:
        item = decision(ticker=ticker)
        repo.save_decision(item)
        repo.resolve(user_id=USER_A, analysis_id=item.analysis_id, outcome=outcome())
    assert (
        repo.get_context(user_id=USER_A, ticker="NVDA", as_of=datetime(2026, 1, 9, tzinfo=UTC))
        == ""
    )
    context = repo.get_context(
        user_id=USER_A, ticker="NVDA", as_of=datetime.now(UTC), max_chars=16000
    )
    assert context.count("NVDA |") == 5 and context.count("AAPL |") == 3
    assert (
        len(repo.get_context(user_id=USER_A, ticker="NVDA", as_of=datetime.now(UTC), max_chars=500))
        <= 500
    )


def test_memory_conflict_is_not_overwritten(tmp_path):
    repo = SQLiteMemoryRepository(tmp_path / "m.sqlite3")
    item = decision()
    repo.save_decision(item)
    with pytest.raises(AnalysisServiceError, match="MEMORY_IDEMPOTENCY_CONFLICT"):
        repo.save_decision(item.model_copy(update={"user_id": USER_B}))
    repo.resolve(user_id=USER_A, analysis_id=item.analysis_id, outcome=outcome())
    with pytest.raises(AnalysisServiceError, match="MEMORY_OUTCOME_CONFLICT"):
        repo.resolve(user_id=USER_A, analysis_id=item.analysis_id, outcome=outcome(raw_return=0.5))


@pytest.mark.parametrize(
    "changes", [{"observed_trading_days": 3}, {"entry_date": date(2025, 12, 31)}]
)
def test_memory_rejects_wrong_evaluation_window(tmp_path, changes):
    repo = SQLiteMemoryRepository(tmp_path / "m.sqlite3")
    item = decision()
    repo.save_decision(item)
    with pytest.raises(AnalysisServiceError, match="INVALID_MEMORY_EVALUATION_WINDOW"):
        repo.resolve(user_id=USER_A, analysis_id=item.analysis_id, outcome=outcome(**changes))


def test_parallel_memory_writes_and_resolution_are_idempotent(tmp_path):
    repo = SQLiteMemoryRepository(tmp_path / "m.sqlite3")
    item = decision()
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert sorted(pool.map(repo.save_decision, [item] * 3)) == [False, False, True]
        results = list(
            pool.map(
                lambda _: repo.resolve(
                    user_id=USER_A, analysis_id=item.analysis_id, outcome=outcome()
                ),
                range(3),
            )
        )
    assert sorted(results) == [False, False, True]
    with sqlite3.connect(repo.path) as conn:
        assert conn.execute("SELECT count(*) FROM analysis_memory").fetchone()[0] == 1


def test_bound_memory_cannot_change_ticker(tmp_path):
    bound = BoundMemory(
        SQLiteMemoryRepository(tmp_path / "m.sqlite3"), USER_A, "NVDA", datetime.now(UTC), 8000
    )
    with pytest.raises(AnalysisServiceError, match="MEMORY_SCOPE_MISMATCH"):
        bound.get_past_context("AAPL")


def test_headless_graph_reads_memory_without_settlement_or_disk_writes(tmp_path):
    from tradingagents.service.graph import HeadlessTradingGraph

    graph = HeadlessTradingGraph.__new__(HeadlessTradingGraph)
    repo = SQLiteMemoryRepository(tmp_path / "m.sqlite3")
    item = decision()
    repo.save_decision(item)
    repo.resolve(user_id=USER_A, analysis_id=item.analysis_id, outcome=outcome())
    graph.memory_log = BoundMemory(repo, USER_A, "NVDA", datetime.now(UTC), 8000)
    graph.settle_all_pending = lambda **_: pytest.fail("Analysis must not settle old decisions")
    assert "LESSON_A" in graph._memory_step({"company_of_interest": "NVDA"})["past_context"]
    assert graph.record_decision("NVDA", "2026-10-17", {}) is None


def test_three_parallel_runs_have_separate_paths_and_no_user_memory_leak(
    state, request_data, settings, tmp_path
):
    repo = SQLiteMemoryRepository(tmp_path / "m.sqlite3")
    item = decision()
    repo.save_decision(item)
    repo.resolve(user_id=USER_A, analysis_id=item.analysis_id, outcome=outcome())
    barrier = Barrier(3)
    paths, contexts = [], []

    def factory(**kwargs):
        config, memory = kwargs["config"], kwargs["memory"]
        paths.append(Path(config["results_dir"]).parent)

        class Graph:
            def propagate(self, ticker, trade_date):
                contexts.append((memory.user_id, memory.get_past_context(ticker)))
                barrier.wait(timeout=10)
                return deepcopy(state), "Overweight"

        return Graph()

    engine = AnalysisEngine(settings=settings, memory=repo, graph_factory=factory)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(engine.run, AnalysisRequest(**request_data), user_id=user)
            for user in (USER_A, USER_B, USER_B)
        ]
        results = [f.result() for f in futures]
    assert all(r.memory_saved and r.quality == "complete" for r in results)
    assert len(set(paths)) == 3 and all(not p.exists() for p in paths)
    assert all(("LESSON_A" in text) == (user == USER_A) for user, text in contexts)


def test_engine_calls_business_once_cleans_up_and_hides_exception(
    request_data, settings, tmp_path, caplog
):
    calls, paths = [], []

    def factory(**kwargs):
        paths.append(Path(kwargs["config"]["results_dir"]).parent)

        class Graph:
            def propagate(self, *args):
                calls.append(args)
                raise TypeError("postgresql://bob:SecretPassword@example.com/db")

        return Graph()

    engine = AnalysisEngine(
        settings=settings,
        memory=SQLiteMemoryRepository(tmp_path / "m.sqlite3"),
        graph_factory=factory,
    )
    with pytest.raises(AnalysisServiceError, match="ANALYSIS_EXECUTION_FAILED") as caught:
        engine.run(AnalysisRequest(**request_data), user_id=USER_A)
    assert len(calls) == 1 and all(not p.exists() for p in paths)
    assert "SecretPassword" not in caplog.text + str(caught.value)


def test_partial_results_do_not_enter_memory(state, request_data, settings, tmp_path):
    state["news_report"] = None

    class Graph:
        def propagate(self, *args):
            return state, "Overweight"

    repo = SQLiteMemoryRepository(tmp_path / "m.sqlite3")
    result = AnalysisEngine(settings=settings, memory=repo, graph_factory=lambda **_: Graph()).run(
        AnalysisRequest(**request_data), user_id=USER_A
    )
    assert result.quality == "partial" and not result.memory_saved
    with sqlite3.connect(repo.path) as conn:
        assert conn.execute("SELECT count(*) FROM analysis_memory").fetchone()[0] == 0


def test_billing_exhaustion_is_not_classified_as_retryable():
    exc = type("RateLimitError", (Exception,), {"code": "insufficient_quota"})("private")
    assert classify_error(exc).code == "PROVIDER_QUOTA_ERROR"
    assert classify_error(exc).retryable is False


def cli_files(tmp_path, request_data, settings):
    request_path, settings_path = tmp_path / "request.json", tmp_path / "settings.json"
    request_path.write_text(json.dumps(request_data))
    settings_path.write_text(settings.model_dump_json())
    return [
        "--request",
        str(request_path),
        "--settings",
        str(settings_path),
        "--user-id",
        str(USER_A),
    ]


def test_cli_defaults_to_validation_without_graph_or_memory(
    tmp_path, request_data, settings, monkeypatch, capsys
):
    monkeypatch.setattr(AnalysisEngine, "run", lambda *a, **k: pytest.fail("Must not call models"))
    assert main(cli_files(tmp_path, request_data, settings)) == 0
    assert json.loads(capsys.readouterr().out)["execution_started"] is False
    assert not list(tmp_path.glob("*.sqlite3"))


def test_cli_refuses_overwrite_before_execution(
    tmp_path, request_data, settings, monkeypatch, capsys
):
    output = tmp_path / "existing.json"
    output.write_text("original")
    monkeypatch.setattr(AnalysisEngine, "run", lambda *a, **k: pytest.fail("Must not call models"))
    args = cli_files(tmp_path, request_data, settings)
    assert main(args + ["--execute", "--output", str(output)]) == 1
    assert output.read_text() == "original"
    assert "OUTPUT_ALREADY_EXISTS" in capsys.readouterr().err


def test_cli_input_error_never_echoes_secret(tmp_path, request_data, settings, capsys):
    args = cli_files(tmp_path, request_data | {"api_key": "SECRET-MUST-NOT-LEAK"}, settings)
    assert main(args) == 1
    captured = capsys.readouterr()
    assert "SECRET-MUST-NOT-LEAK" not in captured.err + captured.out
    assert json.loads(captured.err)["error"]["code"] == "INVALID_INPUT"


def test_memory_save_failure_is_an_explicit_warning(state, request_data, settings):
    class BrokenMemory:
        def save_decision(self, *args):
            raise OSError("private database location")

    class Graph:
        def propagate(self, *args):
            return state, "Overweight"

    result = AnalysisEngine(
        settings=settings, memory=BrokenMemory(), graph_factory=lambda **_: Graph()
    ).run(AnalysisRequest(**request_data), user_id=USER_A)
    assert result.quality == "complete" and not result.memory_saved
    assert result.warning_codes == ("MEMORY_SAVE_FAILED",)


def test_memory_read_failure_is_safe_and_does_not_settle():
    from tradingagents.service.graph import HeadlessTradingGraph

    class BrokenMemory:
        def get_past_context(self, *args):
            raise OSError("postgresql://user:private@host/db")

    graph = HeadlessTradingGraph.__new__(HeadlessTradingGraph)
    graph.memory_log = BrokenMemory()
    result = graph._memory_step({"company_of_interest": "NVDA"})
    assert result == {"past_context": "", "memory_note": "MEMORY_READ_UNAVAILABLE"}
    assert graph.memory_read_failed


def test_cli_requires_explicit_memory_before_execution(
    tmp_path, request_data, settings, monkeypatch, capsys
):
    monkeypatch.setattr(AnalysisEngine, "run", lambda *a, **k: pytest.fail("Must not call models"))
    assert main(cli_files(tmp_path, request_data, settings) + ["--execute"]) == 1
    assert "MEMORY_DATABASE_REQUIRED" in capsys.readouterr().err


def test_trusted_identity_is_required(request_data, settings, tmp_path):
    engine = AnalysisEngine(
        settings=settings,
        memory=SQLiteMemoryRepository(tmp_path / "m.sqlite3"),
        graph_factory=lambda **_: pytest.fail("Must fail before construction"),
    )
    with pytest.raises(AnalysisServiceError, match="INVALID_TRUSTED_IDENTITY"):
        engine.run(AnalysisRequest(**request_data), user_id="unverified-user")


@pytest.mark.parametrize(
    "changes",
    [
        {"raw_return": float("nan")},
        {"benchmark_return": float("inf")},
        {"reflection": "   "},
        {"resolved_at": datetime(2039, 1, 1, tzinfo=UTC)},
        {"resolved_at": datetime(2026, 1, 10)},
        {"exit_date": date(2026, 1, 2)},
    ],
)
def test_malformed_outcomes_are_rejected(changes):
    with pytest.raises(ValidationError):
        outcome(**changes)


def test_cli_json_output_is_private(tmp_path, request_data, settings, capsys):
    output = tmp_path / "validated.json"
    assert main(cli_files(tmp_path, request_data, settings) + ["--output", str(output)]) == 0
    assert json.loads(output.read_text())["execution_started"] is False
    assert output.stat().st_mode & 0o777 == 0o600
    assert capsys.readouterr().out == ""


def test_cli_rejects_dangling_output_symlink_before_models(
    tmp_path, request_data, settings, monkeypatch, capsys
):
    output = tmp_path / "link.json"
    output.symlink_to(tmp_path / "absent.json")
    monkeypatch.setattr(AnalysisEngine, "run", lambda *a, **k: pytest.fail("Must not call models"))
    assert (
        main(cli_files(tmp_path, request_data, settings) + ["--execute", "--output", str(output)])
        == 1
    )
    assert "OUTPUT_ALREADY_EXISTS" in capsys.readouterr().err
