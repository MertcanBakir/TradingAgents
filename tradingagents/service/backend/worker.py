"""Process-isolated worker with lease fencing and a hard wall-clock deadline."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from ..engine import AnalysisEngine
from ..errors import AnalysisServiceError
from ..memory import MemoryDecision
from ..models import AnalysisRequest, AnalysisResult, ServiceSettings
from .repository import SupabaseMemoryRepository
from .security import SecretVault


class DeferredMemory:
    """Read real lessons; defer the new decision to the result transaction."""

    def __init__(self, repository):
        self.repository, self.decision = repository, None

    def get_context(self, **kwargs):
        return self.repository.get_context(**kwargs)

    def save_decision(self, decision):
        self.decision = decision
        return True


def _execute_child(repository, job_id: UUID, token: UUID, output: Path):
    """Private child protocol, never an HTTP endpoint. One graph invocation."""
    from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV

    with repository.transaction() as conn:
        job = conn.execute(
            "SELECT * FROM analysis_backend.jobs WHERE id=%s AND lease_token=%s AND status='running' AND lease_until>clock_timestamp() AND hard_deadline>clock_timestamp()",
            (job_id, token),
        ).fetchone()
    if not job:
        raise AnalysisServiceError("WORKER_LEASE_LOST")
    repository.settings.require_owner(job["user_id"])
    personal = job.get("auth_type", "api_key") == "personal_chatgpt"
    if personal:
        from .personal import configure_auth

        auth = configure_auth(repository.settings)
        provider = "chatgpt"
        key = None
    else:
        connection = repository.connection(job["user_id"], job["connection_id"])
        provider = connection["provider"]
        key = SecretVault(repository.settings).decrypt(
            connection["secret_ciphertext"],
            user_id=job["user_id"],
            connection_id=connection["id"],
            provider=provider,
        )
    if personal:
        try:
            allowed = {model_id for _name, model_id in auth.list_models()}
        except auth.ChatGPTAuthError:
            raise AnalysisServiceError("CHATGPT_LOGIN_REQUIRED") from None
        except Exception:
            raise AnalysisServiceError("CHATGPT_TEMPORARILY_UNAVAILABLE") from None
    else:
        if provider not in repository.settings.allowed_models:
            raise AnalysisServiceError("PROVIDER_NOT_AVAILABLE")
        allowed = set(repository.settings.allowed_models[provider])
    for tier in ("quick", "deep"):
        choice = job["model_settings"][tier]
        if choice["provider"] != provider or choice["model"] not in allowed:
            raise AnalysisServiceError("MODEL_NOT_ALLOWED")
    # This process belongs to one analysis. No shared env mutation in API threads.
    for name in list(os.environ):
        if (
            name in set(PROVIDER_API_KEY_ENV.values())
            or name.endswith("_BASE_URL")
            or name
            in {
                "GOOGLE_APPLICATION_CREDENTIALS",
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "LANGCHAIN_API_KEY",
                "LANGSMITH_API_KEY",
                "OPENAI_ORGANIZATION",
                "OPENAI_PROJECT",
            }
        ):
            os.environ.pop(name, None)
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"
    if not personal:
        os.environ[PROVIDER_API_KEY_ENV[provider]] = key
    else:
        # Resolve/refresh before graph construction. Never fall back to API keys.
        try:
            auth.access_token()
        except auth.ChatGPTUnreachable:
            raise AnalysisServiceError("CHATGPT_TEMPORARILY_UNAVAILABLE") from None
        except auth.ChatGPTAuthError:
            raise AnalysisServiceError("CHATGPT_LOGIN_REQUIRED") from None
    memory = DeferredMemory(SupabaseMemoryRepository(repository))
    engine = AnalysisEngine(
        settings=ServiceSettings.model_validate(job["model_settings"]), memory=memory
    )
    finished = threading.Event()

    def watchdog():
        # Parent SIGKILL must not leave an indefinitely billable child. The
        # child only reads the lease; it cannot extend its dead parent's lease.
        while not finished.wait(10):
            try:
                alive = repository.lease_alive(job["user_id"], job_id, token)
            except Exception:
                alive = False
            if not alive:
                if os.getpgrp() == os.getpid():
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                os._exit(75)

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        result = engine.run(
            AnalysisRequest.model_validate(job["request"]),
            user_id=job["user_id"],
            analysis_id=job_id,
        )
    finally:
        finished.set()
    data = {
        "result": result.model_dump(mode="json"),
        "decision": memory.decision.model_dump(mode="json") if memory.decision else None,
    }
    with output.open("x", encoding="utf-8") as handle:
        os.chmod(output, 0o600)
        json.dump(data, handle, ensure_ascii=False)


def run_child(repository, job_id: UUID, token: UUID, output: Path):
    try:
        from .personal import execution_guard

        with execution_guard(repository.settings):
            return _execute_child(repository, job_id, token, output)
    except Exception as exc:
        allowed = {
            "PROVIDER_QUOTA_ERROR",
            "PROVIDER_TEMPORARY_ERROR",
            "PROVIDER_ACCESS_ERROR",
            "CONNECTION_NOT_FOUND",
            "CONNECTION_SECRET_UNAVAILABLE",
            "MODEL_NOT_ALLOWED",
            "WORKER_LEASE_LOST",
            "PROVIDER_NOT_AVAILABLE",
            "CHATGPT_LOGIN_REQUIRED",
            "CHATGPT_STORAGE_UNSAFE",
            "CHATGPT_TEMPORARILY_UNAVAILABLE",
            "ACCESS_DENIED",
            "PERSONAL_WORKER_BUSY",
        }
        code = (
            exc.code
            if isinstance(exc, AnalysisServiceError) and exc.code in allowed
            else "ANALYSIS_EXECUTION_FAILED"
        )
        with output.open("x", encoding="utf-8") as handle:
            os.chmod(output, 0o600)
            json.dump({"error_code": code}, handle)
        raise AnalysisServiceError(code) from None


class LocalProcessDispatcher:
    """Development dispatcher. AWS will implement the same start(job_id) port.

    Explicit opt-in only. Not suitable as a Lambda subprocess launcher.
    """

    def start(self, analysis_id):
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tradingagents.service.backend.runtime",
                "worker",
                str(analysis_id),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def _stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


class Worker:
    def __init__(self, repository, storage, *, popen=subprocess.Popen, clock=time.monotonic):
        self.repository, self.storage, self.popen, self.clock = repository, storage, popen, clock

    def run(self, job_id: UUID):
        job = self.repository.claim(job_id)
        user, token = job["user_id"], job["lease_token"]
        path = None
        process = None
        try:
            with TemporaryDirectory(prefix="lucrari-worker-") as directory:
                output = Path(directory) / "result.json"
                process = self.popen(
                    [
                        sys.executable,
                        "-m",
                        "tradingagents.service.backend.runtime",
                        "child",
                        str(job_id),
                        str(token),
                        str(output),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                deadline = self.clock() + self.repository.settings.hard_timeout_seconds - 10
                while process.poll() is None:
                    if self.clock() >= deadline:
                        raise AnalysisServiceError("ANALYSIS_TIMEOUT")
                    if not self.repository.heartbeat(user, job_id, token):
                        raise AnalysisServiceError("WORKER_LEASE_LOST")
                    with suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=min(20, self.repository.settings.lease_seconds / 3))
                if not output.is_file() or output.stat().st_size > 2 * 1024 * 1024:
                    raise AnalysisServiceError("ANALYSIS_EXECUTION_FAILED")
                envelope = json.loads(output.read_text())
                if process.returncode != 0:
                    code = envelope.get("error_code")
                    if code not in {
                        "PROVIDER_QUOTA_ERROR",
                        "PROVIDER_TEMPORARY_ERROR",
                        "PROVIDER_ACCESS_ERROR",
                        "CONNECTION_NOT_FOUND",
                        "CONNECTION_SECRET_UNAVAILABLE",
                        "MODEL_NOT_ALLOWED",
                        "WORKER_LEASE_LOST",
                        "PROVIDER_NOT_AVAILABLE",
                        "CHATGPT_LOGIN_REQUIRED",
                        "CHATGPT_STORAGE_UNSAFE",
                        "CHATGPT_TEMPORARILY_UNAVAILABLE",
                        "ACCESS_DENIED",
                        "PERSONAL_WORKER_BUSY",
                    }:
                        code = "ANALYSIS_EXECUTION_FAILED"
                    raise AnalysisServiceError(code)
                result = AnalysisResult.model_validate(envelope["result"])
                decision = (
                    MemoryDecision.model_validate(envelope["decision"])
                    if envelope["decision"]
                    else None
                )
                if (result.quality == "complete") != (decision is not None):
                    raise AnalysisServiceError("RESULT_CONTRACT_MISMATCH")
                if result.analysis_id != job_id or result.ticker != job["request"]["ticker"]:
                    raise AnalysisServiceError("RESULT_CONTRACT_MISMATCH")
                if decision and (
                    decision.user_id != user
                    or decision.analysis_id != job_id
                    or result.quality != "complete"
                ):
                    raise AnalysisServiceError("MEMORY_SCOPE_MISMATCH")
                if not self.repository.heartbeat(user, job_id, token, "persisting"):
                    raise AnalysisServiceError("WORKER_LEASE_LOST")
                path, digest = self.storage.put(user, job_id, token, result)
                self.repository.finish(
                    user,
                    job_id,
                    token,
                    report_path=path,
                    digest=digest,
                    decision=decision,
                    status="success" if result.quality == "complete" else "partial",
                )
                return result
        except Exception as exc:
            if process is not None:
                with suppress(Exception):
                    _stop(process)
            if path:
                # Commit acknowledgement can be lost even after DB success.
                # Never delete an upload already referenced by that commit.
                with suppress(Exception):
                    if not self.repository.report_is_referenced(user, job_id, path):
                        self.storage.discard(path)
            code = (
                exc.code if isinstance(exc, AnalysisServiceError) else "ANALYSIS_EXECUTION_FAILED"
            )
            with suppress(Exception):
                self.repository.finish(
                    user,
                    job_id,
                    token,
                    status="timed_out" if code == "ANALYSIS_TIMEOUT" else "failed",
                    code=code,
                )
            raise AnalysisServiceError(code) from None
