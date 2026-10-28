"""Personal owner and OAuth wiring acceptance; no real login/model/network."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.test_hosted_backend import settings, worker_result
from tradingagents.service.backend.api import create_app
from tradingagents.service.backend.contracts import StartAnalysis
from tradingagents.service.backend.personal import (
    DatabaseDispatcher,
    configure_auth,
    execution_guard,
    manage_session,
)
from tradingagents.service.backend.security import SupabaseAuthenticator
from tradingagents.service.errors import AnalysisServiceError

OWNER = UUID("00000000-0000-4000-8000-000000000001")


@pytest.fixture(autouse=True)
def isolate_auth_module(monkeypatch):
    from tradingagents.llm_clients import chatgpt_auth

    monkeypatch.setattr(chatgpt_auth, "AUTH_FILE", chatgpt_auth.AUTH_FILE)


def personal_settings(tmp_path, **kwargs):
    folder = tmp_path.resolve() / "chatgpt"
    folder.mkdir(mode=0o700, exist_ok=True)
    return settings(
        **(
            {
                "personal_user_id": OWNER,
                "personal_chatgpt_enabled": True,
                "personal_chatgpt_auth_file": folder / "chatgpt-auth.json",
                "allowed_models": {"chatgpt": ("gpt-5.6-sol",)},
            }
            | kwargs
        )
    )


def session_record():
    return {
        "host_id": f"urn:uuid:{uuid4()}",
        "account": {
            "client_id": "test-issued-client",
            "subject": "test-subject",
            "email": "private@example.test",
        },
        "tokens": {
            "access_token": "PRIVATE_ACCESS",
            "refresh_token": "PRIVATE_REFRESH",
            "expires_at": time.time() + 3600,
            "scopes": ["chatgpt.tokens.use.direct"],
        },
    }


def write_record(path, data):
    path.write_text(json.dumps(data))
    path.chmod(0o600)


@pytest.mark.parametrize("anonymous", [True, False])
def test_only_verified_owner_is_accepted(tmp_path, anonymous):
    s = personal_settings(tmp_path)
    response = {"id": str(OWNER), "role": "authenticated", "is_anonymous": anonymous}

    def handler(request):
        assert request.headers["authorization"] == "Bearer real-session"
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        auth = SupabaseAuthenticator(s, client)
        assert auth.authenticate("real-session") == OWNER
        response["id"] = str(uuid4())
        with pytest.raises(AnalysisServiceError, match="ACCESS_DENIED"):
            auth.authenticate("real-session")


def test_rejected_supabase_token_cannot_use_supplied_owner(tmp_path):
    with (
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401))) as client,
        pytest.raises(AnalysisServiceError, match="AUTHENTICATION_REQUIRED"),
    ):
        SupabaseAuthenticator(personal_settings(tmp_path), client).authenticate(str(OWNER))


@pytest.mark.parametrize(
    "change",
    [
        {"personal_user_id": None},
        {"personal_chatgpt_auth_file": None},
        {"personal_chatgpt_auth_file": "relative.json"},
        {"personal_chatgpt_enabled": False},
    ],
)
def test_personal_configuration_fails_closed(tmp_path, change):
    with pytest.raises(ValidationError):
        personal_settings(tmp_path, **change)


def test_start_contract_no_oauth_secret_or_foreign_owner():
    body = {
        "request": {"ticker": "NVDA", "analysis_date": "2026-01-01"},
        "quick_model": "gpt-5.6-sol",
        "deep_model": "gpt-5.6-sol",
        "auth_type": "personal_chatgpt",
    }
    assert StartAnalysis(**body).connection_id is None
    for change in (
        {"connection_id": str(uuid4())},
        {"user_id": str(OWNER)},
        {"access_token": "SECRET"},
        {"auth_type": "api_key"},
    ):
        with pytest.raises(ValidationError):
            StartAnalysis(**(body | change))


def test_admin_import_preserves_host_and_does_not_overwrite_session(tmp_path):
    s = personal_settings(tmp_path)
    assert manage_session(s, "init")["initialized"]
    host = json.loads(s.personal_chatgpt_auth_file.read_text())["host_id"]
    source = tmp_path.resolve() / "import"
    source.mkdir(mode=0o700)
    file = source / "import.json"
    record = session_record()
    write_record(file, record)
    assert manage_session(s, "import", file)["session_imported"]
    saved = json.loads(s.personal_chatgpt_auth_file.read_text())
    assert saved["host_id"] == host != record["host_id"]
    assert saved["tokens"] == record["tokens"]
    assert s.personal_chatgpt_auth_file.stat().st_mode & 0o777 == 0o600
    assert manage_session(s, "status") == {"configured": True, "signed_in": True}
    with pytest.raises(AnalysisServiceError, match="ALREADY_EXISTS"):
        manage_session(s, "import", file)


@pytest.mark.parametrize("case", ["permissions", "parent", "symlink", "malformed", "no_scope"])
def test_session_unsafe_or_invalid_storage_is_rejected(tmp_path, case):
    s = personal_settings(tmp_path)
    record = session_record()
    if case == "no_scope":
        record["tokens"]["scopes"] = []
    write_record(s.personal_chatgpt_auth_file, record)
    if case == "permissions":
        s.personal_chatgpt_auth_file.chmod(0o644)
    if case == "parent":
        s.personal_chatgpt_auth_file.parent.chmod(0o755)
    if case == "malformed":
        s.personal_chatgpt_auth_file.write_text("{}")
    if case == "symlink":
        real = s.personal_chatgpt_auth_file.with_name("real.json")
        s.personal_chatgpt_auth_file.rename(real)
        s.personal_chatgpt_auth_file.symlink_to(real)
    with pytest.raises(AnalysisServiceError):
        configure_auth(s)


def test_missing_session_status_is_not_claimed_as_connected(tmp_path):
    assert manage_session(personal_settings(tmp_path), "status")["signed_in"] is False


def test_api_owner_gate_before_db_even_with_alternative_authenticator(tmp_path):
    repo, auth = MagicMock(), MagicMock()
    repo.settings = personal_settings(tmp_path)
    repo.provider_models.return_value = [{"id": "gpt-session-model", "name": "GPT Session Model"}]
    auth.authenticate.return_value = uuid4()
    app = create_app(
        repository=repo,
        authenticator=auth,
        vault=MagicMock(),
        probe=MagicMock(),
        storage=MagicMock(),
        dispatcher=DatabaseDispatcher(),
    )
    with TestClient(app) as client:
        assert (
            client.get("/v1/providers", headers={"Authorization": "Bearer token"}).status_code
            == 403
        )
        repo.throttle.assert_not_called()
        auth.authenticate.return_value = OWNER
        body = client.get("/v1/providers", headers={"Authorization": "Bearer token"}).json()
        assert body["providers"][0]["auth_type"] == "personal_chatgpt"
        assert body["providers"][0]["models"] == ["gpt-session-model"]
        assert body["providers"][0]["model_options"] == [
            {"id": "gpt-session-model", "name": "GPT Session Model"}
        ]
        assert "chatgpt" not in body["unavailable"]


def test_execution_guard_rejects_overlapping_child(tmp_path):
    s = personal_settings(tmp_path)
    with (
        execution_guard(s),
        pytest.raises(AnalysisServiceError, match="PERSONAL_WORKER_BUSY"),
        execution_guard(s),
    ):
        pytest.fail("must not overlap")


def test_personal_child_reuses_upstream_callable_without_any_api_key(tmp_path, monkeypatch):
    from tradingagents.llm_clients import chatgpt_auth
    from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
    from tradingagents.service.backend import worker

    s = personal_settings(tmp_path)
    write_record(s.personal_chatgpt_auth_file, session_record())
    # Do not leave the upstream module redirected after this test.
    monkeypatch.setattr(chatgpt_auth, "AUTH_FILE", chatgpt_auth.AUTH_FILE)
    monkeypatch.setattr(chatgpt_auth, "list_models", lambda: [("GPT 5.6 Sol", "gpt-5.6-sol")])
    repo = MagicMock()
    repo.settings = s
    job, token = uuid4(), uuid4()
    row = {
        "user_id": OWNER,
        "auth_type": "personal_chatgpt",
        "connection_id": None,
        "request": {"ticker": "NVDA", "analysis_date": "2026-01-01"},
        "model_settings": {
            k: {"provider": "chatgpt", "model": "gpt-5.6-sol"} for k in ("quick", "deep")
        },
    }
    repo.transaction.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = row
    for key in PROVIDER_API_KEY_ENV.values():
        if key:
            monkeypatch.setenv(key, "INHERITED_PRIVATE_KEY")
    calls = []

    def run(request, **kwargs):
        import os

        from tradingagents.llm_clients.openai_client import _chatgpt_credential

        assert _chatgpt_credential()() == "PRIVATE_ACCESS"
        assert all(key not in os.environ for key in PROVIDER_API_KEY_ENV.values() if key)
        calls.append(kwargs)
        return worker_result(job)

    engine = MagicMock(side_effect=lambda **kwargs: SimpleNamespace(run=run))
    monkeypatch.setattr(worker, "AnalysisEngine", engine)
    output = tmp_path / "result.json"
    worker.run_child(repo, job, token, output)
    assert len(calls) == 1
    repo.connection.assert_not_called()
    assert "PRIVATE_" not in output.read_text()


def test_settings_file_and_invalid_permissions_fail_without_secret_echo(tmp_path, monkeypatch):
    from tradingagents.service.backend.runtime import settings_from_env

    path = tmp_path / "settings.json"
    s = personal_settings(tmp_path)
    # SecretStr dumps redact, so fixture keys must explicitly use original values.
    raw = s.model_dump(mode="json")
    raw.update(
        database_url="postgresql://localhost/test",
        supabase_publishable_key="test-key",
        supabase_service_key="test-secret",
        encryption_keys={"v1": "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="},
    )
    write_record(path, raw)
    monkeypatch.setenv("LUCRARI_BACKEND_SETTINGS_FILE", str(path))
    assert settings_from_env().personal_user_id == OWNER
    path.chmod(0o644)
    with pytest.raises(AnalysisServiceError, match="BACKEND_CONFIGURATION_INVALID"):
        settings_from_env()


def test_compose_does_not_mount_chatgpt_into_api_or_maintenance():
    import yaml

    root = Path(__file__).resolve().parents[1]
    services = yaml.safe_load((root / "compose.personal.yml").read_text())["services"]
    for name in ("api", "maintenance"):
        assert not any("/run/chatgpt" in v for v in services[name]["volumes"])
    assert any("/run/chatgpt" in v for v in services["worker"]["volumes"])
    assert "ports" not in services["api"]
    assert services["worker"]["read_only"] is True


def test_upstream_rotation_is_persisted_once_across_concurrent_callers(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from tradingagents.llm_clients import chatgpt_auth

    s = personal_settings(tmp_path)
    record = session_record()
    record["tokens"]["expires_at"] = 1
    write_record(s.personal_chatgpt_auth_file, record)
    auth = configure_auth(s)
    monkeypatch.setattr(chatgpt_auth, "_retry_after", 0)
    rotated = {
        **record,
        "tokens": {
            **record["tokens"],
            "access_token": "ROTATED_ACCESS",
            "refresh_token": "ROTATED_REFRESH",
            "expires_at": time.time() + 3600,
        },
    }
    refresh = MagicMock(return_value=rotated)
    monkeypatch.setattr(auth, "_refresh", refresh)
    barrier = Barrier(2)

    def access(_):
        barrier.wait()
        return auth.access_token()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(access, range(2))) == ["ROTATED_ACCESS", "ROTATED_ACCESS"]
    refresh.assert_called_once()
    assert (
        json.loads(s.personal_chatgpt_auth_file.read_text())["tokens"]["refresh_token"]
        == "ROTATED_REFRESH"
    )


def test_persistent_worker_discovers_committed_job_and_stops_safely(tmp_path, monkeypatch):
    import signal

    from tradingagents.service.backend import runtime, worker

    repo = MagicMock()
    repo.settings = personal_settings(tmp_path)
    job = uuid4()
    repo.next_reserved.return_value = job
    handlers = {}

    def register(signum, handler):
        old = handlers.get(signum)
        handlers[signum] = handler
        return old

    monkeypatch.setattr(signal, "signal", register)

    def run(job_id):
        assert job_id == job
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    instance = MagicMock()
    instance.run.side_effect = run
    constructor = MagicMock(return_value=instance)
    monkeypatch.setattr(worker, "Worker", constructor)
    auth = MagicMock()
    auth.AUTH_FILE = repo.settings.personal_chatgpt_auth_file
    auth.list_models.return_value = [("GPT Dynamic", "gpt-dynamic")]
    monkeypatch.setattr(runtime, "configure_auth", MagicMock(), raising=False)
    monkeypatch.setattr(
        "tradingagents.service.backend.personal.configure_auth", MagicMock(return_value=auth)
    )
    assert runtime.serve(repo) == 0
    instance.run.assert_called_once_with(job)
    repo.reap.assert_called_once()
    repo.replace_provider_models.assert_called_once_with(
        "chatgpt", [("GPT Dynamic", "gpt-dynamic")]
    )


def test_second_worker_daemon_cannot_start(tmp_path):
    from tradingagents.dataflows.files import locked
    from tradingagents.service.backend.runtime import serve

    repo = MagicMock()
    repo.settings = personal_settings(tmp_path)
    with (
        locked(repo.settings.personal_chatgpt_auth_file.parent / "worker-daemon"),
        pytest.raises(AnalysisServiceError, match="PERSONAL_WORKER_BUSY"),
    ):
        serve(repo)
    repo.next_reserved.assert_not_called()
