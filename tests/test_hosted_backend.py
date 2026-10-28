"""Offline HTTP/security/worker tests; no real provider or Supabase requests."""

from __future__ import annotations

import base64
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tradingagents.service.backend.api import create_app
from tradingagents.service.backend.contracts import BackendSettings
from tradingagents.service.backend.evaluation import evaluation_sessions, make_outcome
from tradingagents.service.backend.providers import ProviderProbe, endpoint
from tradingagents.service.backend.security import SecretVault, SupabaseAuthenticator
from tradingagents.service.backend.storage import ReportStorage
from tradingagents.service.backend.worker import Worker
from tradingagents.service.errors import AnalysisServiceError
from tradingagents.service.memory import MemoryDecision
from tradingagents.service.models import AnalysisRequest, ServiceSettings
from tradingagents.service.normalize import normalize_result


def settings(**overrides):
    return BackendSettings(
        **(
            {
                "database_url": "postgresql://localhost/test",
                "supabase_url": "https://test-project.supabase.co",
                "supabase_publishable_key": "public-test-key",
                "supabase_service_key": "server-only-test-key",
                "encryption_keys": {"v1": base64.b64encode(b"k" * 32).decode()},
                "active_encryption_key": "v1",
                "allowed_models": {"openai": ("gpt-5.6-sol",)},
            }
            | overrides
        )
    )


@pytest.fixture
def http_api():
    repo, auth, vault, probe, storage, dispatcher = (MagicMock() for _ in range(6))
    repo.settings = settings()
    user, job_id = uuid4(), uuid4()
    auth.authenticate.return_value = user
    row = {
        "id": job_id,
        "user_id": user,
        "status": "reserved",
        "stage": "accepted",
        "created_at": datetime.now(UTC),
        "started_at": None,
        "finished_at": None,
        "error_code": None,
        "request": {"ticker": "NVDA", "analysis_date": "2026-10-17"},
    }
    repo.reserve.return_value = (row, True)
    app = create_app(
        repository=repo,
        authenticator=auth,
        vault=vault,
        probe=probe,
        storage=storage,
        dispatcher=dispatcher,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield SimpleNamespace(
            client=client,
            repo=repo,
            auth=auth,
            vault=vault,
            probe=probe,
            storage=storage,
            dispatcher=dispatcher,
            user=user,
            row=row,
            app=app,
        )


def headers():
    return {"Authorization": "Bearer user-token", "Idempotency-Key": str(uuid4())}


def start_body():
    return {
        "request": {"ticker": "NVDA", "analysis_date": "2026-10-17"},
        "connection_id": str(uuid4()),
        "quick_model": "gpt-5.6-sol",
        "deep_model": "gpt-5.6-sol",
    }


def test_auth_missing_denies_every_private_route(http_api):
    a = http_api
    assert a.client.get("/v1/providers").status_code == 401
    a.auth.authenticate.assert_not_called()
    assert a.client.get("/healthz").status_code == 200


def test_start_uses_only_authenticated_owner_and_safe_contract(http_api):
    a = http_api
    result = a.client.post("/v1/analyses", headers=headers(), json=start_body())
    assert result.status_code == 202, result.text
    assert a.repo.reserve.call_args.args[0] == a.user
    assert "user_id" not in result.json()
    assert "connection_id" not in result.json()
    a.dispatcher.start.assert_called_once_with(a.row["id"])
    assert result.headers["cache-control"] == "no-store"
    assert "requestBody" in a.app.openapi()["paths"]["/v1/analyses"]["post"]


def test_idempotent_replay_never_dispatches_twice(http_api):
    a = http_api
    a.repo.reserve.return_value = (a.row, False)
    response = a.client.post("/v1/analyses", headers=headers(), json=start_body())
    assert response.json()["replayed"] is True
    a.dispatcher.start.assert_not_called()


@pytest.mark.parametrize(
    "code,status",
    [("CAPACITY_FULL", 429), ("IDEMPOTENCY_CONFLICT", 409), ("DAILY_ANALYSIS_LIMIT", 429)],
)
def test_admission_errors_do_not_launch(http_api, code, status):
    a = http_api
    a.repo.reserve.side_effect = AnalysisServiceError(code)
    assert a.client.post("/v1/analyses", headers=headers(), json=start_body()).status_code == status
    a.dispatcher.start.assert_not_called()


def test_dispatch_failure_is_recorded_without_secret(http_api):
    a = http_api
    a.dispatcher.start.side_effect = RuntimeError("server-password-SECRET")
    response = a.client.post("/v1/analyses", headers=headers(), json=start_body())
    assert response.status_code == 503
    assert "SECRET" not in response.text
    a.repo.dispatch_failed.assert_called_once_with(a.user, a.row["id"])


def test_connection_stores_ciphertext_never_returns_secret(http_api):
    a = http_api
    a.vault.encrypt.return_value = "v1.ciphertext"
    response = a.client.post(
        "/v1/connections",
        headers=headers(),
        json={"provider": "openai", "api_key": "sk-private-test-secret"},
    )
    assert response.status_code == 201, response.text
    a.probe.verify.assert_called_once_with("openai", "sk-private-test-secret")
    assert a.repo.add_connection.call_args.args[-1] == "v1.ciphertext"
    assert "private" not in response.text and "ciphertext" not in response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"provider": "openai", "api_key": "private-invalid key"},
        {"provider": "chatgpt", "api_key": "private-test-secret"},
        {"provider": "openai", "api_key": "private-test-secret", "user_id": str(uuid4())},
        {"provider": "openai", "api_key": "private-test-secret", "base_url": "http://127.0.0.1"},
    ],
)
def test_invalid_secret_request_never_echoed(http_api, payload):
    response = http_api.client.post("/v1/connections", headers=headers(), json=payload)
    assert response.status_code == 422
    assert "private" not in response.text
    http_api.probe.verify.assert_not_called()


def test_body_limit_and_invalid_pagination(http_api):
    a = http_api
    assert (
        a.client.post("/v1/connections", headers=headers(), content="x" * 17000).status_code == 413
    )
    assert a.client.get("/v1/analyses?limit=101", headers=headers()).status_code == 422
    assert a.client.get("/v1/analyses?offset=-1", headers=headers()).status_code == 422


def test_provider_options_explicitly_explain_hosted_oauth_boundary(http_api):
    response = http_api.client.get("/v1/providers", headers=headers())
    assert response.status_code == 200
    assert response.json()["unavailable"]["chatgpt"] == "HOSTED_OAUTH_APPROVAL_REQUIRED"


def test_owner_scoped_status_and_not_ready_report(http_api):
    a = http_api
    a.repo.job.return_value = a.row
    assert a.client.get(f"/v1/analyses/{a.row['id']}", headers=headers()).status_code == 200
    a.repo.job.assert_called_with(a.user, a.row["id"])
    assert a.client.get(f"/v1/analyses/{a.row['id']}/report", headers=headers()).status_code == 409
    a.storage.get.assert_not_called()


def test_vault_aad_binds_user_provider_connection_and_key_rotation():
    s = settings()
    vault = SecretVault(s)
    user, connection = uuid4(), uuid4()
    cipher = vault.encrypt("private-key", user_id=user, connection_id=connection, provider="openai")
    assert "private-key" not in cipher
    assert (
        vault.decrypt(cipher, user_id=user, connection_id=connection, provider="openai")
        == "private-key"
    )
    for other in ({"user_id": uuid4()}, {"connection_id": uuid4()}, {"provider": "google"}):
        scope = {"user_id": user, "connection_id": connection, "provider": "openai"} | other
        with pytest.raises(AnalysisServiceError, match="CONNECTION_SECRET_UNAVAILABLE"):
            vault.decrypt(cipher, **scope)
    rotated = SecretVault(
        settings(
            encryption_keys={**s.encryption_keys, "v2": base64.b64encode(b"x" * 32).decode()},
            active_encryption_key="v2",
        )
    )
    assert (
        rotated.decrypt(cipher, user_id=user, connection_id=connection, provider="openai")
        == "private-key"
    )
    assert rotated.encrypt(
        "private-key", user_id=user, connection_id=connection, provider="openai"
    ).startswith("v2.")


@pytest.mark.parametrize(
    "change",
    [
        {"supabase_url": "http://remote.example"},
        {"supabase_url": "https://user:pass@example.com"},
        {"allowed_models": {"chatgpt": ("gpt-5.6-sol",)}},
        {"active_encryption_key": "missing"},
    ],
)
def test_trusted_config_fails_closed(change):
    with pytest.raises(ValidationError):
        settings(**change)


def test_auth_is_verified_by_fixed_supabase_server_not_decoded_claims():
    user = uuid4()

    def reply(request):
        assert str(request.url) == "https://test-project.supabase.co/auth/v1/user"
        assert request.headers["authorization"] == "Bearer valid-token"
        assert request.headers["apikey"] == "public-test-key"
        return httpx.Response(
            200, json={"id": str(user), "role": "authenticated", "is_anonymous": False}
        )

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        assert SupabaseAuthenticator(settings(), client).authenticate("valid-token") == user


@pytest.mark.parametrize(
    "status,payload,code",
    [
        (401, {}, "AUTHENTICATION_REQUIRED"),
        (403, {}, "AUTHENTICATION_REQUIRED"),
        (500, {}, "AUTH_SERVICE_UNAVAILABLE"),
        (302, {}, "AUTH_SERVICE_UNAVAILABLE"),
        (200, {"id": str(uuid4()), "role": "service_role"}, "AUTHENTICATION_REQUIRED"),
        (
            200,
            {"id": str(uuid4()), "role": "authenticated", "is_anonymous": True},
            "AUTHENTICATION_REQUIRED",
        ),
    ],
)
def test_auth_fail_closed(status, payload, code):
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(status, json=payload))
        ) as client,
        pytest.raises(AnalysisServiceError, match=code),
    ):
        SupabaseAuthenticator(settings(), client).authenticate("bad-token")


@pytest.mark.parametrize("provider", ["openai", "google", "anthropic", "xai", "deepseek"])
def test_provider_probe_no_completion_or_custom_url(provider):
    def reply(request):
        assert request.method == "GET" and request.url.path.endswith("/models")
        assert "private-key" not in str(request.url)
        return httpx.Response(200, json={"data": []})

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        ProviderProbe(client).verify(provider, "private-key")
    with pytest.raises(AnalysisServiceError):
        endpoint("openai_compatible")


def decision():
    return MemoryDecision(
        analysis_id=uuid4(),
        user_id=uuid4(),
        ticker="NVDA",
        analysis_date=date(2026, 1, 1),
        rating="Overweight",
        decision="Evidence",
        created_at=datetime(2026, 1, 1, 12, tzinfo=UTC),
        evaluation_trading_days=5,
        benchmark="SPY",
    )


def test_calendar_excludes_holiday_weekend_and_incomplete_close():
    d = decision()
    assert evaluation_sessions(d, datetime(2026, 1, 9, 20, tzinfo=UTC)) == []
    sessions = evaluation_sessions(d, datetime(2026, 1, 12, 23, tzinfo=UTC))
    assert len(sessions) == 6
    assert sessions[0] == date(2026, 1, 2) and sessions[-1] == date(2026, 1, 9)
    assert all(day.weekday() < 5 for day in sessions)


def test_outcome_is_factual_no_missing_price_fallback():
    d = decision()
    sessions = evaluation_sessions(d, datetime(2026, 1, 12, 23, tzinfo=UTC))
    prices = dict.fromkeys(sessions, 100.0)
    prices[sessions[-1]] = 110
    result = make_outcome(
        d, sessions, prices, dict.fromkeys(sessions, 100.0), datetime(2026, 1, 12, 23, tzinfo=UTC)
    )
    assert result.raw_return == pytest.approx(0.1)
    assert result.benchmark_return == 0
    assert "not proof" in result.reflection
    prices.pop(sessions[1])
    with pytest.raises(AnalysisServiceError, match="EVALUATION_PRICES_MISSING"):
        make_outcome(d, sessions, prices, prices, datetime(2026, 1, 12, 23, tzinfo=UTC))


def test_storage_refuses_foreign_owner_and_traversal():
    client = MagicMock()
    storage = ReportStorage(settings(), client)
    with pytest.raises(AnalysisServiceError, match="REPORT_NOT_READY"):
        storage.get(uuid4(), uuid4(), "foreign/path", None)
    with pytest.raises(AnalysisServiceError, match="INVALID_REPORT_PATH"):
        storage._url("../other/file.json")
    client.get.assert_not_called()


def test_worker_claim_rejection_never_runs_graph():
    repo = MagicMock()
    repo.claim.side_effect = AnalysisServiceError("WORKER_CLAIM_REJECTED")
    popen = MagicMock()
    with pytest.raises(AnalysisServiceError, match="WORKER_CLAIM_REJECTED"):
        Worker(repo, MagicMock(), popen=popen).run(uuid4())
    popen.assert_not_called()


def worker_result(job_id):
    from tests.test_analysis_service import state

    now = datetime.now(UTC)
    return normalize_result(
        state=state.__wrapped__(),
        request=AnalysisRequest(ticker="NVDA", analysis_date="2026-10-17"),
        settings=ServiceSettings(
            quick={"provider": "openai", "model": "gpt-5.6-sol"},
            deep={"provider": "openai", "model": "gpt-5.6-sol"},
        ),
        analysis_id=job_id,
        started_at=now,
        finished_at=now,
        duration_seconds=1,
    )


@pytest.mark.parametrize("failure", [None, "storage", "lease", "child", "timeout"])
def test_worker_success_error_boundaries_and_no_graph_retry(failure, monkeypatch):
    import json

    from tradingagents.service.backend import worker as module

    repo, storage = MagicMock(), MagicMock()
    repo.settings = settings()
    job_id, user, token = uuid4(), uuid4(), uuid4()
    repo.claim.return_value = {
        "id": job_id,
        "user_id": user,
        "lease_token": token,
        "request": {"ticker": "NVDA"},
    }
    repo.heartbeat.return_value = failure != "lease"
    storage.put.return_value = ("owned-path", "digest")
    if failure == "storage":
        storage.put.side_effect = AnalysisServiceError("REPORT_STORAGE_UNAVAILABLE")
    launches = []

    class Process:
        returncode = 1 if failure == "child" else 0

        def poll(self):
            return None if failure in ("timeout", "lease") else self.returncode

        def wait(self, timeout=None):
            return self.returncode

    result = worker_result(job_id).model_copy(update={"memory_saved": True})
    d = MemoryDecision(
        analysis_id=job_id,
        user_id=user,
        ticker="NVDA",
        analysis_date=result.analysis_date,
        rating=result.final_rating,
        decision="Evidence",
        created_at=result.started_at,
        evaluation_trading_days=5,
        benchmark="SPY",
    )

    def popen(args, **kwargs):
        launches.append(args)
        envelope = {"result": result.model_dump(mode="json"), "decision": d.model_dump(mode="json")}
        if failure == "child":
            envelope = {"error_code": "PROVIDER_QUOTA_ERROR"}
        Path(args[-1]).write_text(json.dumps(envelope))
        return Process()

    stopped = MagicMock()
    monkeypatch.setattr(module, "_stop", stopped)
    ticks = iter([0, 999999])
    worker = Worker(repo, storage, popen=popen, clock=lambda: next(ticks))
    if failure:
        with pytest.raises(AnalysisServiceError):
            worker.run(job_id)
        assert repo.finish.call_args.kwargs["status"] in ("failed", "timed_out")
        stopped.assert_called_once()
    else:
        assert worker.run(job_id).analysis_id == job_id
        assert repo.finish.call_args.kwargs["status"] == "success"
        assert repo.finish.call_args.kwargs["decision"].user_id == user
    assert len(launches) == 1
    assert not any("private-key" in str(arg) for arg in launches[0])


def test_storage_upload_download_digest_and_owner_checks():
    job_id, user, token = uuid4(), uuid4(), uuid4()
    result = worker_result(job_id)
    saved = {}

    def reply(request):
        assert request.headers["authorization"] == "Bearer server-only-test-key"
        if request.method == "POST":
            assert request.headers["x-upsert"] == "false"
            saved["body"] = request.content
            return httpx.Response(200, json={})
        assert "/object/authenticated/analysis-reports/" in request.url.path
        return httpx.Response(200, content=saved["body"])

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        storage = ReportStorage(settings(), client)
        path, digest = storage.put(user, job_id, token, result)
        assert storage.get(user, job_id, path, digest) == result
        with pytest.raises(AnalysisServiceError):
            storage.get(user, job_id, path, "bad-digest")
        with pytest.raises(AnalysisServiceError, match="REPORT_NOT_READY"):
            storage.get(uuid4(), job_id, path, digest)


def test_raw_backend_exception_not_leaked_to_response_or_logs(http_api, caplog):
    a = http_api
    a.repo.connections.side_effect = RuntimeError("DB_PASSWORD_secret_123456")
    response = a.client.get("/v1/connections", headers=headers())
    assert response.status_code == 503
    assert "DB_PASSWORD" not in response.text + caplog.text


def test_orphan_cleanup_defaults_readonly_and_never_deletes_referenced_report():
    from tradingagents.service.backend.maintenance import cleanup_orphans

    repo, storage = MagicMock(), MagicMock()
    repo.orphan_reports.return_value = [{"name": "referenced"}, {"name": "orphan"}]
    conn = repo.transaction.return_value.__enter__.return_value
    conn.execute.return_value.fetchone.side_effect = [{"id": uuid4()}, None]
    assert cleanup_orphans(repo, storage)["orphan_candidates"] == 1
    storage.discard.assert_not_called()
    conn.execute.return_value.fetchone.side_effect = [{"id": uuid4()}, None]
    storage.discard.return_value = True
    assert cleanup_orphans(repo, storage, apply=True)["deleted"] == 1
    storage.discard.assert_called_once_with("orphan")


def test_remote_database_requires_verified_tls():
    with pytest.raises(ValidationError, match="DATABASE_VERIFIED_TLS_REQUIRED"):
        settings(database_url="postgresql://operator:password@remote.example/db?sslmode=require")
    assert settings(
        database_url="postgresql://operator:password@remote.example/db?sslmode=verify-full"
    )


def test_chunked_body_limit_without_content_length(http_api):
    response = http_api.client.post(
        "/v1/connections", headers=headers(), content=iter([b"x" * 9000, b"y" * 9000])
    )
    assert response.status_code == 413
    http_api.probe.verify.assert_not_called()


def test_uncertain_finalization_does_not_delete_committed_report(monkeypatch):
    import json

    from tradingagents.service.backend import worker as module

    repo, storage = MagicMock(), MagicMock()
    repo.settings = settings()
    job, user, token = uuid4(), uuid4(), uuid4()
    repo.claim.return_value = {
        "id": job,
        "user_id": user,
        "lease_token": token,
        "request": {"ticker": "NVDA"},
    }
    repo.heartbeat.return_value = True
    repo.report_is_referenced.return_value = True
    repo.finish.side_effect = RuntimeError("lost commit acknowledgement")
    storage.put.return_value = ("committed-report", "digest")
    result = worker_result(job).model_copy(update={"memory_saved": True})
    d = MemoryDecision(
        analysis_id=job,
        user_id=user,
        ticker="NVDA",
        analysis_date=result.analysis_date,
        rating=result.final_rating,
        decision="Evidence",
        created_at=result.started_at,
        evaluation_trading_days=5,
        benchmark="SPY",
    )

    def popen(args, **kwargs):
        Path(args[-1]).write_text(
            json.dumps(
                {"result": result.model_dump(mode="json"), "decision": d.model_dump(mode="json")}
            )
        )
        return type("Process", (), {"returncode": 0, "poll": lambda self: 0})()

    monkeypatch.setattr(module, "_stop", MagicMock())
    with pytest.raises(AnalysisServiceError):
        Worker(repo, storage, popen=popen).run(job)
    repo.report_is_referenced.assert_called_once_with(user, job, "committed-report")
    storage.discard.assert_not_called()


def test_orphan_cleanup_refuses_foreign_or_malformed_names():
    from tradingagents.service.backend.maintenance import cleanup_orphans

    repo = MagicMock()
    repo.orphan_reports.return_value = [{"name": "../../foreign"}, {"name": "manual-file.json"}]
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: pytest.fail("must not delete"))
    ) as client:
        result = cleanup_orphans(repo, ReportStorage(settings(), client), apply=True)
    assert result["orphan_candidates"] == 0 and result["deleted"] == 0


def test_child_uses_only_owner_key_and_clears_inherited_provider_credentials(tmp_path, monkeypatch):
    from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
    from tradingagents.service.backend import worker as module

    repo = MagicMock()
    repo.settings = settings()
    user, job_id, token, connection = uuid4(), uuid4(), uuid4(), uuid4()
    model_settings = {
        "quick": {"provider": "openai", "model": "gpt-5.6-sol"},
        "deep": {"provider": "openai", "model": "gpt-5.6-sol"},
    }
    row = {
        "id": job_id,
        "user_id": user,
        "connection_id": connection,
        "request": {"ticker": "NVDA", "analysis_date": "2026-10-17"},
        "model_settings": model_settings,
    }
    repo.transaction.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = row
    cipher = SecretVault(repo.settings).encrypt(
        "owner-only-private-key", user_id=user, connection_id=connection, provider="openai"
    )
    repo.connection.return_value = {
        "id": connection,
        "provider": "openai",
        "secret_ciphertext": cipher,
    }
    for name in PROVIDER_API_KEY_ENV.values():
        if name:
            monkeypatch.setenv(name, "inherited-other-user-key")
    for name in ("OPENAI_BASE_URL", "LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        monkeypatch.setenv(name, "inherited-unsafe-setting")

    class Engine:
        def __init__(self, *, settings, memory):
            self.memory = memory

        def run(self, request, *, user_id, analysis_id):
            import os

            assert os.environ["OPENAI_API_KEY"] == "owner-only-private-key"
            assert all(
                name not in os.environ
                for name in PROVIDER_API_KEY_ENV.values()
                if name and name != "OPENAI_API_KEY"
            )
            assert "OPENAI_BASE_URL" not in os.environ
            assert os.environ["LANGSMITH_TRACING"] == "false"
            result = worker_result(analysis_id)
            self.memory.save_decision(
                MemoryDecision(
                    analysis_id=analysis_id,
                    user_id=user_id,
                    ticker=request.ticker,
                    analysis_date=request.analysis_date,
                    rating="Overweight",
                    decision="Evidence",
                    created_at=result.started_at,
                    evaluation_trading_days=5,
                    benchmark="SPY",
                )
            )
            return result.model_copy(update={"memory_saved": True})

    monkeypatch.setattr(module, "AnalysisEngine", Engine)
    output = tmp_path / "child.json"
    module.run_child(repo, job_id, token, output)
    assert "private-key" not in output.read_text()
    assert output.stat().st_mode & 0o777 == 0o600
