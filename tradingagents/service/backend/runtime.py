"""Explicit entry points. No default credentials, launches or import side effects."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from uuid import UUID

import httpx

from ..errors import AnalysisServiceError
from .contracts import BackendSettings
from .repository import Repository
from .storage import ReportStorage


def settings_from_env():
    try:
        settings_file = os.environ.get("LUCRARI_BACKEND_SETTINGS_FILE")
        if settings_file:
            import stat

            path = Path(settings_file)
            if not path.is_absolute() or path.is_symlink():
                raise ValueError("UNSAFE_SETTINGS_FILE")
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("UNSAFE_SETTINGS_FILE")
            raw = path.read_text()
        else:
            raw = os.environ["LUCRARI_BACKEND_SETTINGS"]
        return BackendSettings.model_validate_json(raw)
    except Exception:
        raise AnalysisServiceError("BACKEND_CONFIGURATION_INVALID") from None


def application():
    """uvicorn tradingagents.service.backend.runtime:application --factory"""
    from contextlib import asynccontextmanager

    from .api import create_app
    from .providers import ProviderProbe
    from .security import SecretVault, SupabaseAuthenticator
    from .worker import LocalProcessDispatcher

    settings = settings_from_env()
    dispatch_mode = os.environ.get("LUCRARI_DISPATCHER")
    if dispatch_mode not in {"local-process", "database"}:
        raise AnalysisServiceError("DISPATCHER_NOT_CONFIGURED")
    if dispatch_mode == "database":
        from .personal import DatabaseDispatcher

        if settings.personal_user_id is None:
            raise AnalysisServiceError("PERSONAL_OWNER_REQUIRED")
        dispatcher = DatabaseDispatcher()
    else:
        if settings.personal_chatgpt_enabled:
            raise AnalysisServiceError("PERSONAL_DATABASE_DISPATCH_REQUIRED")
        dispatcher = LocalProcessDispatcher()
    client = httpx.Client(timeout=15, trust_env=False)
    app = create_app(
        repository=Repository(settings),
        authenticator=SupabaseAuthenticator(settings, client),
        vault=SecretVault(settings),
        probe=ProviderProbe(client),
        storage=ReportStorage(settings, client),
        dispatcher=dispatcher,
    )

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            client.close()

    app.router.lifespan_context = lifespan
    return app


def serve(repository, *, maintenance=False):
    """Independent persistent service. DB outages never start speculative work."""
    import signal
    import threading
    import time
    from contextlib import nullcontext

    from .worker import Worker

    if repository.settings.personal_user_id is None:
        raise AnalysisServiceError("PERSONAL_OWNER_REQUIRED")
    stop = threading.Event()

    def interrupted(_signum, _frame):
        stop.set()
        raise AnalysisServiceError("WORKER_INTERRUPTED")

    previous = {s: signal.signal(s, interrupted) for s in (signal.SIGTERM, signal.SIGINT)}
    guard = nullcontext(True)
    try:
        if not maintenance:
            from tradingagents.dataflows.files import locked

            from .personal import configure_auth

            auth = configure_auth(repository.settings, require_session=False)
            guard = locked(auth.AUTH_FILE.parent / "worker-daemon", wait=False)
        with guard as held, httpx.Client(timeout=15, trust_env=False) as client:
            if not held:
                raise AnalysisServiceError("PERSONAL_WORKER_BUSY")
            evaluation_due = 0.0
            model_catalog_due = 0.0
            while not stop.is_set():
                try:
                    repository.reap()
                    if maintenance:
                        if time.monotonic() >= evaluation_due:
                            from .evaluation import EvaluationJob

                            print(json.dumps(EvaluationJob(repository).run()), flush=True)
                            evaluation_due = time.monotonic() + 3600
                    else:
                        if time.monotonic() >= model_catalog_due:
                            try:
                                from .personal import configure_auth

                                auth = configure_auth(repository.settings)
                                repository.replace_provider_models("chatgpt", auth.list_models())
                                model_catalog_due = time.monotonic() + 300
                            except Exception:
                                print(json.dumps({"event": "model_catalog_error"}), flush=True)
                                model_catalog_due = time.monotonic() + 60
                        job_id = repository.next_reserved()
                        if job_id is not None:
                            Worker(repository, ReportStorage(repository.settings, client)).run(
                                job_id
                            )
                except Exception:
                    if stop.is_set():
                        break
                    # Full exception strings can contain credentials.
                    print(
                        json.dumps(
                            {"event": "maintenance_error" if maintenance else "worker_error"}
                        ),
                        flush=True,
                    )
                stop.wait(30 if maintenance else 3)
        return 0
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "worker",
            "child",
            "reap",
            "evaluate",
            "cleanup",
            "serve-worker",
            "maintain",
            "session",
        ],
    )
    parser.add_argument(
        "--action",
        choices=["init", "import", "status", "login", "logout", "models"],
        default="status",
    )
    parser.add_argument("--source", type=Path)
    parser.add_argument(
        "--apply", action="store_true", help="Only cleanup: delete verified orphan objects"
    )
    parser.add_argument("arguments", nargs="*")
    args = parser.parse_args(argv)
    if args.apply and args.command != "cleanup":
        parser.error("--apply is only valid for cleanup")
    # Provider/SDK logs are untrusted. Never log raw exception messages/headers.
    logging.basicConfig(level=logging.CRITICAL)
    try:
        settings = settings_from_env()
        if args.command == "session":
            from .personal import manage_session

            print(json.dumps(manage_session(settings, args.action, args.source)))
            return 0
        repo = Repository(settings)
        if args.command in {"serve-worker", "maintain"}:
            return serve(repo, maintenance=args.command == "maintain")
        with httpx.Client(timeout=15, trust_env=False) as client:
            if args.command == "worker":
                if settings.personal_chatgpt_enabled:
                    raise AnalysisServiceError("PERSONAL_DATABASE_DISPATCH_REQUIRED")
                import signal

                from .worker import Worker

                def interrupted(_signum, _frame):
                    raise AnalysisServiceError("WORKER_INTERRUPTED")

                previous = {
                    s: signal.signal(s, interrupted) for s in (signal.SIGTERM, signal.SIGINT)
                }
                try:
                    Worker(repo, ReportStorage(repo.settings, client)).run(UUID(args.arguments[0]))
                finally:
                    for signum, handler in previous.items():
                        signal.signal(signum, handler)
            elif args.command == "child":
                from .worker import run_child

                run_child(
                    repo, UUID(args.arguments[0]), UUID(args.arguments[1]), Path(args.arguments[2])
                )
            elif args.command == "reap":
                print(json.dumps({"expired_count": len(repo.reap())}))
            elif args.command == "cleanup":
                from .maintenance import cleanup_orphans

                result = cleanup_orphans(
                    repo, ReportStorage(repo.settings, client), apply=args.apply
                )
                print(json.dumps(result))
                return 1 if result["delete_failed"] else 0
            else:
                from .evaluation import EvaluationJob

                result = EvaluationJob(repo).run()
                print(json.dumps(result))
                return 1 if result["failed"] else 0
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, AnalysisServiceError) else "BACKEND_EXECUTION_FAILED"
        print(json.dumps({"error": {"code": code}}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
