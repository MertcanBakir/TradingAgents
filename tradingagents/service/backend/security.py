"""Supabase validates bearer sessions; AES-GCM binds secrets to their owner."""

from __future__ import annotations

import base64
import os
from uuid import UUID

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..errors import AnalysisServiceError
from .contracts import BackendSettings


class SecretVault:
    def __init__(self, settings: BackendSettings):
        self.keys = {
            k: AESGCM(base64.b64decode(v.get_secret_value()))
            for k, v in settings.encryption_keys.items()
        }
        self.active = settings.active_encryption_key

    @staticmethod
    def _aad(user_id: UUID, connection_id: UUID, provider: str) -> bytes:
        return f"lucrari:v1:{user_id}:{connection_id}:{provider}".encode()

    def encrypt(self, secret: str, *, user_id: UUID, connection_id: UUID, provider: str) -> str:
        nonce = os.urandom(12)
        cipher = self.keys[self.active].encrypt(
            nonce, secret.encode(), self._aad(user_id, connection_id, provider)
        )
        return self.active + "." + base64.b64encode(nonce + cipher).decode()

    def decrypt(self, envelope: str, *, user_id: UUID, connection_id: UUID, provider: str) -> str:
        try:
            key_id, payload = envelope.split(".", 1)
            raw = base64.b64decode(payload, validate=True)
            return (
                self.keys[key_id]
                .decrypt(raw[:12], raw[12:], self._aad(user_id, connection_id, provider))
                .decode()
            )
        except Exception:
            raise AnalysisServiceError("CONNECTION_SECRET_UNAVAILABLE") from None


class SupabaseAuthenticator:
    """Online getUser: Auth server verifies signature/expiry/issuer, not decode-only.

    No caching of tokens/user records. Does not accept refresh tokens or roles
    supplied in a request body. A service key is never used for authentication.
    """

    def __init__(self, settings: BackendSettings, client: httpx.Client):
        self.settings, self.client = settings, client

    def authenticate(self, token: str) -> UUID:
        if not token or len(token) > 8192 or any(c.isspace() for c in token):
            raise AnalysisServiceError("AUTHENTICATION_REQUIRED")
        try:
            response = self.client.get(
                self.settings.supabase_url.rstrip("/") + "/auth/v1/user",
                headers={
                    "apikey": self.settings.supabase_publishable_key.get_secret_value(),
                    "Authorization": "Bearer " + token,
                },
                timeout=10,
                follow_redirects=False,
            )
            if response.status_code in (401, 403):
                raise AnalysisServiceError("AUTHENTICATION_REQUIRED")
            if response.status_code != 200:
                raise AnalysisServiceError("AUTH_SERVICE_UNAVAILABLE", retryable=True)
            data = response.json()
            if data.get("role") != "authenticated":
                raise AnalysisServiceError("AUTHENTICATION_REQUIRED")
            user_id = UUID(data["id"])
            self.settings.require_owner(user_id)
            # Only the explicitly configured personal owner may use an
            # anonymous Auth session. Never trust a request-supplied UID.
            if data.get("is_anonymous", False) and self.settings.personal_user_id is None:
                raise AnalysisServiceError("AUTHENTICATION_REQUIRED")
            return user_id
        except AnalysisServiceError:
            raise
        except Exception:
            raise AnalysisServiceError("AUTH_SERVICE_UNAVAILABLE", retryable=True) from None
