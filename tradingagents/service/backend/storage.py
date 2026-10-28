"""Private Supabase Storage. Object paths are derived, never supplied by clients."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

import httpx

from ..errors import AnalysisServiceError
from ..models import AnalysisResult
from .contracts import BackendSettings

MAX_REPORT_BYTES = 2 * 1024 * 1024


class ReportStorage:
    def __init__(self, settings: BackendSettings, client: httpx.Client):
        self.settings, self.client = settings, client

    @staticmethod
    def path(user_id: UUID, job_id: UUID, token: UUID):
        return f"{user_id}/{job_id}/{token}.json"

    def _url(self, path, *, authenticated=False):
        # Validate even internally persisted paths; no SSRF/path traversal.
        parts = path.split("/")
        try:
            if len(parts) != 3 or not parts[2].endswith(".json"):
                raise ValueError()
            for part in (parts[0], parts[1], parts[2][:-5]):
                if str(UUID(part)) != part:
                    raise ValueError()
        except ValueError:
            raise AnalysisServiceError("INVALID_REPORT_PATH") from None
        route = "authenticated/" if authenticated else ""
        return (
            self.settings.supabase_url.rstrip("/")
            + "/storage/v1/object/"
            + route
            + "analysis-reports/"
            + path
        )

    def _headers(self):
        key = self.settings.supabase_service_key.get_secret_value()
        return {"apikey": key, "Authorization": "Bearer " + key}

    def put(self, user_id, job_id, token, result: AnalysisResult):
        path = self.path(user_id, job_id, token)
        data = result.model_dump_json().encode()
        if len(data) > MAX_REPORT_BYTES:
            raise AnalysisServiceError("REPORT_TOO_LARGE")
        try:
            response = self.client.post(
                self._url(path),
                headers={
                    **self._headers(),
                    "Content-Type": "application/json",
                    "x-upsert": "false",
                },
                content=data,
                timeout=30,
                follow_redirects=False,
            )
            if response.status_code not in (200, 201):
                raise ValueError()
        except Exception:
            raise AnalysisServiceError("REPORT_STORAGE_UNAVAILABLE", retryable=True) from None
        return path, hashlib.sha256(data).hexdigest()

    def get(self, user_id, job_id, path, digest):
        if not path or not path.startswith(f"{user_id}/{job_id}/"):
            raise AnalysisServiceError("REPORT_NOT_READY")
        try:
            with self.client.stream(
                "GET",
                self._url(path, authenticated=True),
                headers=self._headers(),
                timeout=30,
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise ValueError()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_REPORT_BYTES:
                        raise ValueError()
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError()
            result = AnalysisResult.model_validate(json.loads(data))
            if result.analysis_id != job_id:
                raise ValueError()
            return result
        except Exception:
            raise AnalysisServiceError("REPORT_STORAGE_UNAVAILABLE", retryable=True) from None

    def discard(self, path):
        try:
            response = self.client.delete(
                self._url(path), headers=self._headers(), timeout=15, follow_redirects=False
            )
            return response.status_code in (200, 204, 404)
        except Exception:
            return False
