"""Production-image smoke test. Run read-only, non-root and --network none.

Uses fabricated configuration and an empty ChatGPT session, never real secrets.
Only loopback HTTP is used to verify the real packaged API starts correctly.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory


def main():
    assert os.getuid() == 1000, "Expected production non-root UID"
    assert os.statvfs("/").f_flag & os.ST_RDONLY, "Expected a read-only root filesystem"
    # No actual credential file should be baked into the runtime image.
    assert not list(Path("/home/appuser").rglob("chatgpt-auth.json"))
    with TemporaryDirectory(prefix="personal-container-smoke-") as directory:
        root = Path(directory)
        auth_dir = root / "chatgpt"
        auth_dir.mkdir(mode=0o700)
        config = {
            "database_url": "postgresql://postgres@localhost/smoke",
            "supabase_url": "https://smoke.invalid",
            "supabase_publishable_key": "FAKE_PUBLIC_SMOKE_KEY",
            "supabase_service_key": "FAKE_SERVICE_SMOKE_KEY",
            "encryption_keys": {"v1": base64.b64encode(b"s" * 32).decode()},
            "active_encryption_key": "v1",
            "personal_user_id": "00000000-0000-4000-8000-000000000001",
            "personal_chatgpt_enabled": True,
            "personal_chatgpt_auth_file": str(auth_dir / "chatgpt-auth.json"),
            "allowed_models": {"chatgpt": ["smoke-only-model"]},
        }
        settings_file = root / "settings.json"
        settings_file.write_text(json.dumps(config))
        settings_file.chmod(0o600)
        env = {
            **os.environ,
            "LUCRARI_BACKEND_SETTINGS_FILE": str(settings_file),
            "LUCRARI_DISPATCHER": "database",
        }
        command = [sys.executable, "-m", "tradingagents.service.backend.runtime"]
        for action, expected in (
            ("init", {"initialized": True, "session_imported": False}),
            ("status", {"configured": True, "signed_in": False}),
        ):
            result = subprocess.run(
                [*command, "session", "--action", action],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            assert json.loads(result.stdout) == expected
        auth_file = auth_dir / "chatgpt-auth.json"
        assert auth_file.stat().st_mode & 0o777 == 0o600
        assert list(json.loads(auth_file.read_text())) == ["host_id"]
        # Real API must boot without access to the worker's credential directory.
        config["personal_chatgpt_auth_file"] = "/run/worker-only/chatgpt-auth.json"
        settings_file.write_text(json.dumps(config))
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tradingagents.service.backend.runtime:application",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "18000",
                "--no-access-log",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Explicitly bypass any inherited proxy for loopback HTTP.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            deadline = time.monotonic() + 30
            while True:
                assert process.poll() is None, "Packaged API exited before becoming ready"
                try:
                    with opener.open("http://127.0.0.1:18000/healthz", timeout=1) as response:
                        assert json.load(response) == {"status": "ok"}
                    break
                except urllib.error.URLError:
                    if time.monotonic() >= deadline:
                        raise AssertionError("Packaged API did not become ready") from None
                    time.sleep(0.1)
            try:
                opener.open("http://127.0.0.1:18000/v1/providers", timeout=2)
                raise AssertionError("Unauthenticated private route was not denied")
            except urllib.error.HTTPError as exc:
                assert exc.code == 401
                assert json.load(exc)["error"]["code"] == "AUTHENTICATION_REQUIRED"
                assert exc.headers["Cache-Control"] == "no-store"
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    print("PRODUCTION_CONTAINER_SMOKE_PASSED")


if __name__ == "__main__":
    main()
