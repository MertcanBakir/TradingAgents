"""Short-lived HTTP control plane; no graph or model execution in API handlers."""

from typing import Annotated, Protocol
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..errors import AnalysisServiceError
from .contracts import ConnectionInput, StartAnalysis
from .repository import PUBLIC_JOB_COLUMNS


class Dispatcher(Protocol):
    def start(self, analysis_id: UUID) -> None: ...


def public_job(row):
    return {
        **{key: row[key] for key in PUBLIC_JOB_COLUMNS},
        "ticker": row["request"]["ticker"],
        "analysis_date": row["request"]["analysis_date"],
    }


def create_app(*, repository, authenticator, vault, probe, storage, dispatcher: Dispatcher):
    app = FastAPI(title="Lucrari Analysis API", version="1", docs_url=None, redoc_url=None)
    bearer = HTTPBearer(auto_error=False)

    @app.middleware("http")
    async def safe_headers(request: Request, call_next):
        # Reject large bodies before FastAPI parses a secret-bearing JSON value.
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > 16384):
            return JSONResponse(
                {"error": {"code": "REQUEST_TOO_LARGE", "retryable": False}}, status_code=413
            )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                return JSONResponse(
                    {"error": {"code": "REQUEST_TOO_LARGE", "retryable": False}},
                    status_code=413,
                    headers={"Cache-Control": "no-store"},
                )
        request._body = bytes(body)
        try:
            response = await call_next(request)
        except Exception:
            # Do not let ServerErrorMiddleware rethrow raw DB/provider errors
            # into the ASGI server's traceback logger after sending a response.
            response = JSONResponse(
                {"error": {"code": "BACKEND_UNAVAILABLE", "retryable": True}}, status_code=503
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _exc):
        # FastAPI defaults include rejected input (which may be an API key).
        return JSONResponse(
            {"error": {"code": "INVALID_REQUEST", "retryable": False}}, status_code=422
        )

    @app.exception_handler(AnalysisServiceError)
    async def domain_error(_request, exc):
        codes = {
            "AUTHENTICATION_REQUIRED": 401,
            "ACCESS_DENIED": 403,
            "CONNECTION_NOT_FOUND": 404,
            "ANALYSIS_NOT_FOUND": 404,
            "REPORT_NOT_READY": 409,
            "IDEMPOTENCY_CONFLICT": 409,
            "CAPACITY_FULL": 429,
            "DAILY_ANALYSIS_LIMIT": 429,
            "RATE_LIMITED": 429,
            "CONNECTION_LIMIT": 409,
            "MODEL_NOT_ALLOWED": 422,
            "INSTRUMENT_NOT_SUPPORTED": 422,
            "PROVIDER_NOT_AVAILABLE": 422,
            "PROVIDER_ACCESS_ERROR": 422,
            "PROVIDER_VERIFICATION_UNSUPPORTED": 422,
        }
        return JSONResponse(
            {"error": {"code": exc.code, "retryable": exc.retryable}},
            status_code=codes.get(exc.code, 503),
        )

    @app.exception_handler(Exception)
    async def unexpected(_request, _exc):
        return JSONResponse(
            {"error": {"code": "BACKEND_UNAVAILABLE", "retryable": True}}, status_code=503
        )

    def identity(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if credentials is None:
            raise AnalysisServiceError("AUTHENTICATION_REQUIRED")
        user = authenticator.authenticate(credentials.credentials)
        repository.settings.require_owner(user)
        repository.throttle(user)
        return user

    User = Annotated[UUID, Depends(identity)]

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/v1/providers")
    def providers(user_id: User):
        providers = [
            {
                "provider": provider,
                "models": list(models),
                "model_options": [{"id": model, "name": model} for model in models],
                "auth_type": "api_key",
            }
            for provider, models in repository.settings.allowed_models.items()
            if provider != "chatgpt"
        ]
        chatgpt_models = []
        if repository.settings.personal_chatgpt_enabled:
            chatgpt_models = repository.provider_models("chatgpt")
            if chatgpt_models:
                providers.append(
                    {
                        "provider": "chatgpt",
                        "models": [item["id"] for item in chatgpt_models],
                        "model_options": chatgpt_models,
                        "auth_type": "personal_chatgpt",
                    }
                )
        return {
            "providers": providers,
            "unavailable": {
                **(
                    {"chatgpt": "MODEL_CATALOG_UNAVAILABLE"}
                    if repository.settings.personal_chatgpt_enabled and not chatgpt_models
                    else (
                        {}
                        if repository.settings.personal_chatgpt_enabled
                        else {"chatgpt": "HOSTED_OAUTH_APPROVAL_REQUIRED"}
                    )
                ),
                "ollama": "HOSTED_ENDPOINT_NOT_CONFIGURED",
                "openai_compatible": "CUSTOM_ENDPOINTS_DISABLED",
                "azure": "HOSTED_ENDPOINT_NOT_CONFIGURED",
                "bedrock": "PER_USER_IAM_NOT_CONFIGURED",
            },
        }

    @app.get("/v1/connections")
    def connections(user_id: User):
        return {"items": repository.connections(user_id)}

    @app.post("/v1/connections", status_code=201)
    def connect(payload: ConnectionInput, user_id: User):
        if payload.provider not in repository.settings.allowed_models:
            raise AnalysisServiceError("PROVIDER_NOT_AVAILABLE")
        repository.throttle(
            user_id, "connection", repository.settings.daily_connection_limit, 86400
        )
        key = payload.api_key.get_secret_value()
        probe.verify(payload.provider, key)
        connection_id = uuid4()
        ciphertext = vault.encrypt(
            key, user_id=user_id, connection_id=connection_id, provider=payload.provider
        )
        repository.add_connection(user_id, connection_id, payload.provider, ciphertext)
        return {"id": connection_id, "provider": payload.provider, "status": "active"}

    @app.post("/v1/connections/{connection_id}/verify")
    def verify(connection_id: UUID, user_id: User):
        repository.throttle(
            user_id, "connection", repository.settings.daily_connection_limit, 86400
        )
        row = repository.connection(user_id, connection_id)
        key = vault.decrypt(
            row["secret_ciphertext"],
            user_id=user_id,
            connection_id=connection_id,
            provider=row["provider"],
        )
        probe.verify(row["provider"], key)
        repository.mark_verified(user_id, connection_id)
        return {"id": connection_id, "valid": True}

    @app.delete("/v1/connections/{connection_id}")
    def disconnect(connection_id: UUID, user_id: User):
        repository.revoke_connection(user_id, connection_id)
        return {"revoked": True}

    @app.post("/v1/analyses", status_code=202)
    def start(
        payload: StartAnalysis,
        user_id: User,
        idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    ):
        row, created = repository.reserve(user_id, idempotency_key, payload)
        if created:
            try:
                dispatcher.start(row["id"])
            except Exception:
                repository.dispatch_failed(user_id, row["id"])
                raise AnalysisServiceError("DISPATCH_FAILED", retryable=True) from None
        return {**public_job(row), "replayed": not created}

    @app.get("/v1/analyses")
    def history(
        user_id: User,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    ):
        rows, total = repository.history(user_id, limit, offset)
        return {"items": [public_job(row) for row in rows], "total_count": total}

    @app.get("/v1/analyses/{analysis_id}")
    def status(analysis_id: UUID, user_id: User):
        return public_job(repository.job(user_id, analysis_id))

    @app.get("/v1/analyses/{analysis_id}/report")
    def report(analysis_id: UUID, user_id: User):
        row = repository.job(user_id, analysis_id)
        if row["status"] not in ("success", "partial"):
            raise AnalysisServiceError("REPORT_NOT_READY")
        return storage.get(user_id, analysis_id, row["report_path"], row["report_sha256"])

    return app
