"""Multi-user provider contracts and execution boundaries; fabricated credentials only."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.test_hosted_backend import settings
from tests.test_personal_backend import OWNER, personal_settings
from tradingagents.llm_clients import factory
from tradingagents.llm_clients.tier_credentials import credential_for, tier_credentials
from tradingagents.service.backend.api import create_app
from tradingagents.service.backend.contracts import APIProvider, ConnectionInput, StartAnalysis
from tradingagents.service.backend.providers import ProviderProbe, endpoint
from tradingagents.service.backend.security import SupabaseAuthenticator
from tradingagents.service.errors import AnalysisServiceError


def api_body(a=None, b=None):
    return {
        "request": {"ticker": "NVDA", "analysis_date": "2026-01-01"},
        "models": {
            "quick": {"connection_id": str(a or uuid4()), "model": "fast-model"},
            "deep": {"connection_id": str(b or uuid4()), "model": "deep-model"},
        },
    }


def test_new_contract_binds_each_tier_and_rejects_untrusted_configuration():
    body = api_body()
    assert StartAnalysis(**body).models.quick.model == "fast-model"
    for field, value in (
        ("connection_id", str(uuid4())),
        ("user_id", str(uuid4())),
        ("api_key", "PRIVATE"),
        ("quick_model", "unexpected"),
        ("base_url", "http://localhost"),
    ):
        with pytest.raises(ValidationError):
            StartAnalysis(**{**body, field: value})
    for tier in ("quick", "deep"):
        bad = api_body()
        bad["models"][tier]["provider"] = "chatgpt"
        with pytest.raises(ValidationError):
            StartAnalysis(**bad)
    assert (
        ConnectionInput(provider="openai", api_key="PRIVATE_TEST_KEY", name="My connection").name
        == "My connection"
    )
    with pytest.raises(ValidationError):
        ConnectionInput(provider="openai", api_key="PRIVATE_TEST_KEY", name="bad\nname")


def test_allowlist_is_closed_and_personal_owner_is_separate(tmp_path):
    invited, outsider = uuid4(), uuid4()
    s = personal_settings(
        tmp_path,
        allowed_user_ids=(invited,),
        allowed_models={"openai": ("fast-model",), "chatgpt": ("session-model",)},
    )
    s.require_owner(invited)
    s.require_owner(OWNER)
    with pytest.raises(AnalysisServiceError, match="ACCESS_DENIED"):
        s.require_owner(outsider)
    with pytest.raises(AnalysisServiceError, match="ACCESS_DENIED"):
        s.require_personal_owner(invited)
    with pytest.raises(AnalysisServiceError, match="ACCESS_DENIED"):
        settings().require_owner(invited)


@pytest.mark.parametrize("anonymous", [False, True])
def test_invited_supabase_account_must_be_verified_and_nonanonymous(tmp_path, anonymous):
    invited = uuid4()
    s = personal_settings(tmp_path, allowed_user_ids=(invited,))
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"id": str(invited), "role": "authenticated", "is_anonymous": anonymous}
            )
        )
    ) as client:
        if anonymous:
            with pytest.raises(AnalysisServiceError, match="AUTHENTICATION_REQUIRED"):
                SupabaseAuthenticator(s, client).authenticate("test")
        else:
            assert SupabaseAuthenticator(s, client).authenticate("test") == invited


def test_nonowner_sees_api_providers_but_never_owner_session_catalog(tmp_path):
    invited = uuid4()
    repo, auth = MagicMock(), MagicMock()
    repo.settings = personal_settings(
        tmp_path,
        allowed_user_ids=(invited,),
        allowed_models={
            "chatgpt": ("session-model",),
            "openai": ("api-model",),
            "anthropic": ("claude-model",),
        },
    )
    auth.authenticate.return_value = invited
    app = create_app(
        repository=repo,
        authenticator=auth,
        vault=MagicMock(),
        probe=MagicMock(),
        storage=MagicMock(),
        dispatcher=MagicMock(),
    )
    with TestClient(app) as client:
        result = client.get("/v1/providers", headers={"Authorization": "Bearer test"})
        assert result.status_code == 200
        assert {p["provider"] for p in result.json()["providers"]} == {"openai", "anthropic"}
        repo.provider_models.assert_not_called()
        body = api_body()
        body.pop("models")
        body.update(
            auth_type="personal_chatgpt", quick_model="session-model", deep_model="session-model"
        )
        # Defense-in-depth must reject before repository mocks/DB dispatch.
        result = client.post(
            "/v1/analyses",
            headers={"Authorization": "Bearer test", "Idempotency-Key": str(uuid4())},
            json=body,
        )
        assert result.status_code == 403
        repo.reserve.assert_not_called()


def test_same_provider_different_keys_are_injected_per_tier_without_env(monkeypatch):
    calls = []
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        factory, "create_llm_client", lambda **kwargs: calls.append(kwargs) or SimpleNamespace()
    )
    config = {"llm_provider": "openai", "quick_think_llm": "fast", "deep_think_llm": "deep"}
    with tier_credentials({("quick", "openai"): "KEY_A", ("deep", "openai"): "KEY_B"}):
        factory.create_tier_client(config, "quick")
        factory.create_tier_client(config, "deep")
    assert [c["api_key"] for c in calls] == ["KEY_A", "KEY_B"]
    assert credential_for("quick", "openai") is None
    assert "KEY_A" not in str(config) and "KEY_B" not in str(config)
    with (
        tier_credentials({("quick", "openai"): "KEY_A"}),
        pytest.raises(ValueError, match="Missing trusted"),
    ):
        factory.create_tier_client(config, "deep")


def test_tier_credentials_are_reset_after_exception():
    with pytest.raises(RuntimeError), tier_credentials({("quick", "openai"): "PRIVATE"}):
        assert credential_for("quick", "openai") == "PRIVATE"
        raise RuntimeError("failed")
    assert credential_for("quick", "openai") is None


def test_explicit_openai_key_does_not_require_or_use_another_users_env(monkeypatch):
    from tradingagents.llm_clients import openai_client

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from dataclasses import replace

    monkeypatch.setitem(
        openai_client.OPENAI_COMPATIBLE_PROVIDERS,
        "openai",
        replace(
            openai_client.OPENAI_COMPATIBLE_PROVIDERS["openai"], chat_class=lambda **kwargs: kwargs
        ),
    )
    result = openai_client.OpenAIClient("gpt-5.6-sol", api_key="EXPLICIT_KEY").get_llm()
    assert result["api_key"] == "EXPLICIT_KEY"
    monkeypatch.setenv("OPENAI_API_KEY", "WRONG_INHERITED_KEY")
    assert (
        openai_client.OpenAIClient("gpt-5.6-sol", api_key="EXPLICIT_KEY").get_llm()["api_key"]
        == "EXPLICIT_KEY"
    )


def test_all_simple_api_providers_use_fixed_endpoints_and_never_paid_verification():
    from typing import get_args

    for provider in get_args(APIProvider):
        seen = []
        with httpx.Client(
            transport=httpx.MockTransport(
                lambda request, seen=seen: seen.append(request) or httpx.Response(200, json={})
            )
        ) as client:
            ProviderProbe(client).verify(provider, "SYNTHETIC_PRIVATE_KEY")
        assert len(seen) == 1 and seen[0].method == "GET"
        assert str(seen[0].url) == endpoint(provider).rstrip("/") + "/models"
        assert "SYNTHETIC_PRIVATE_KEY" not in str(seen[0].url)


def test_cd_cannot_deploy_v2_before_explicit_transition_gate():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / ".github/workflows/cd.yml").read_text()
    assert "vars.PRODUCTION_DEPLOY_ENABLED == 'true' && vars.BACKEND_SCHEMA_VERSION == '2'" in text


def test_api_only_daemon_does_not_need_personal_session(monkeypatch):
    import signal

    from tradingagents.service.backend import runtime, worker

    invited = uuid4()
    repo = MagicMock()
    repo.settings = settings(allowed_user_ids=(invited,))
    repo.next_reserved.return_value = uuid4()
    handlers = {}

    def register(signum, handler):
        return handlers.setdefault(signum, handler)

    monkeypatch.setattr(signal, "signal", register)
    instance = MagicMock()
    instance.run.side_effect = lambda _: handlers[signal.SIGTERM](signal.SIGTERM, None)
    monkeypatch.setattr(worker, "Worker", lambda *args: instance)
    assert runtime.serve(repo) == 0
    instance.run.assert_called_once()
    repo.replace_provider_models.assert_not_called()
