"""Operator-only ChatGPT session management for a single self-hosted owner.

No HTTP login/import endpoint, no credentials in PostgreSQL. Reuses upstream
OAuth, token refresh and interprocess token locking without modifying the graph.
"""

from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from ..errors import AnalysisServiceError


def _private_path(path: Path, *, existing: bool):
    if not path.is_absolute() or path.is_symlink() or path.parent.is_symlink():
        raise AnalysisServiceError("CHATGPT_STORAGE_UNSAFE")
    # All ancestor paths must be real directories (no indirect symlink escape).
    if any(parent.is_symlink() for parent in path.parents):
        raise AnalysisServiceError("CHATGPT_STORAGE_UNSAFE")
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
        raise AnalysisServiceError("CHATGPT_STORAGE_UNSAFE")
    if existing or path.exists():
        info = path.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > 65536
        ):
            raise AnalysisServiceError("CHATGPT_STORAGE_UNSAFE")


def configure_auth(settings, *, require_session=True):
    if not settings.personal_chatgpt_enabled or settings.personal_user_id is None:
        raise AnalysisServiceError("PROVIDER_NOT_AVAILABLE")
    from tradingagents.llm_clients import chatgpt_auth as auth

    path = settings.personal_chatgpt_auth_file
    try:
        _private_path(path, existing=require_session)
        auth.AUTH_FILE = path
        if require_session:
            data = json.loads(path.read_text())
            _validate_session(data)
    except AnalysisServiceError:
        raise
    except Exception:
        raise AnalysisServiceError("CHATGPT_LOGIN_REQUIRED") from None
    return auth


def _validate_session(data):
    try:
        UUID(data["host_id"].removeprefix("urn:uuid:"))
        account, tokens = data["account"], data["tokens"]
        if not (
            all(isinstance(account[k], str) and account[k] for k in ("client_id", "subject"))
            and all(
                isinstance(tokens[k], str) and tokens[k] for k in ("access_token", "refresh_token")
            )
            and isinstance(tokens["expires_at"], (int, float))
            and isinstance(tokens["scopes"], list)
            and "chatgpt.tokens.use.direct" in tokens["scopes"]
        ):
            raise ValueError("INVALID_SESSION")
    except Exception:
        raise AnalysisServiceError("CHATGPT_LOGIN_REQUIRED") from None


def manage_session(settings, action: str, source: Path | None = None):
    """Explicit local admin command. Does not run during API/worker startup."""
    auth = configure_auth(settings, require_session=False)
    from tradingagents.dataflows.files import locked

    if action in {"init", "import"}:
        with locked(auth.AUTH_FILE):
            current = auth._read()
            if current.get("tokens"):
                raise AnalysisServiceError("CHATGPT_SESSION_ALREADY_EXISTS")
            host_id = current.get("host_id") or f"urn:uuid:{uuid4()}"
            if action == "import":
                try:
                    _private_path(source, existing=True)
                    data = json.loads(source.read_text())
                    _validate_session(data)
                except AnalysisServiceError:
                    raise
                except Exception:
                    raise AnalysisServiceError("CHATGPT_IMPORT_INVALID") from None
                # Never import the laptop's host ID into this host.
                current = {**data, "host_id": host_id}
            else:
                current = {"host_id": host_id}
            auth._write(current)
        return {"initialized": True, "session_imported": action == "import"}
    if action == "status":
        try:
            configure_auth(settings)
        except AnalysisServiceError as exc:
            if exc.code == "CHATGPT_LOGIN_REQUIRED":
                return {"configured": True, "signed_in": False}
            raise
        return {"configured": True, "signed_in": True}
    if action == "login":
        auth.sign_in()
        return {"signed_in": True}
    if action == "logout":
        auth.sign_out()
        return {"signed_in": False}
    if action == "models":
        configure_auth(settings)
        return {"models": auth.list_models()}
    raise AnalysisServiceError("INVALID_SESSION_ACTION")


class DatabaseDispatcher:
    """Reservation commit IS dispatch. The independent worker discovers it."""

    def start(self, analysis_id):
        return None


@contextmanager
def execution_guard(settings):
    """A surviving child keeps the host execution lock even if its parent dies."""
    if not settings.personal_chatgpt_enabled:
        yield
        return
    from tradingagents.dataflows.files import locked

    auth = configure_auth(settings, require_session=False)
    with locked(auth.AUTH_FILE.parent / "analysis-execution", wait=False) as held:
        if not held:
            raise AnalysisServiceError("PERSONAL_WORKER_BUSY")
        yield
