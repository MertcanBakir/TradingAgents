"""Apply the production provider settings from Parameter Store to the EC2 worker."""

import fcntl
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

if Path(__file__).with_name("deploy_host.py").is_file():
    from deploy_host import PROBE, atomic_env
else:
    from deploy import PROBE, atomic_env

PARAMETER = "/finbot/tradingagents/production/data-providers"
NAMES = ("FRED_API_KEY", "FMP_API_KEY", "SEC_EDGAR_USER_AGENT")
BASE = Path("/opt/lucrari")
ENV_PATH = BASE / ".env"
COMPOSE = [
    "docker", "compose", "--project-name", "lucrari-personal",
    "--env-file", str(ENV_PATH), "-f", str(BASE / "compose.personal.yml"),
]


def run(args, *, input_text=None):
    environment = {name: value for name, value in os.environ.items() if name not in NAMES}
    result = subprocess.run(args, cwd=BASE, env=environment, text=True, input=input_text,
                            capture_output=True, timeout=180, check=True)
    return result.stdout


def updated_env(previous, values):
    names = "|".join(NAMES)
    existing = re.compile(rf"^[ \t]*(?:export[ \t]+)?(?:{names})[ \t]*=", re.M)
    lines = [line for line in previous.splitlines(keepends=True) if not existing.match(line)]
    settings = []
    for name in NAMES:
        value = values[name]
        if not isinstance(value, str) or not value or any(char in value for char in "\r\n\x00"):
            raise ValueError("Invalid provider configuration")
        escaped = value.replace("'", "\\'")
        settings.append(f"{name}='{escaped}'\n")
    return "".join(lines).rstrip("\n") + "\n" + "".join(settings)


def worker_matches(values):
    probe = (
        "import json,os,sys; expected=json.load(sys.stdin); "
        "sys.exit(any(os.getenv(name) != value for name,value in expected.items()))"
    )
    try:
        run([*COMPOSE, "exec", "-T", "worker", "python", "-c", probe],
            input_text=json.dumps(values))
    except subprocess.CalledProcessError:
        return False
    return True


def read_provider_settings():
    values = json.loads(run([
        "aws", "--region", "eu-west-1", "ssm", "get-parameter", "--name", PARAMETER,
        "--with-decryption", "--query", "Parameter.Value", "--output", "text", "--no-cli-pager",
    ]))
    if set(values) != set(NAMES):
        raise ValueError("Unexpected provider settings")
    return values


def check_worker_configuration():
    compose = json.loads(run([*COMPOSE, "config", "--format", "json"]))
    services = compose["services"]
    if not set(NAMES) <= services["worker"]["environment"].keys():
        raise ValueError("Worker provider settings are missing from Compose")
    if any(set(NAMES) & service.get("environment", {}).keys()
           for name, service in services.items() if name != "worker"):
        raise ValueError("Unexpected provider settings outside the worker")


def sync():
    info = ENV_PATH.stat()
    if ENV_PATH.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("Unsafe environment file")
    values = read_provider_settings()
    previous = ENV_PATH.read_text()
    updated = updated_env(previous, values)
    if updated == previous and worker_matches(values):
        return "Provider settings already current"

    check_worker_configuration()

    status = json.loads(run([*COMPOSE, "exec", "-T", "api", "python", "-c", PROBE]))
    if not status.get("ok"):
        raise ValueError("Backend preflight failed")
    if status["active_jobs"]:
        return "Provider sync deferred while analyses are active"

    recreate = [*COMPOSE, "up", "--detach", "--no-deps", "--no-build", "--pull", "never",
                "--force-recreate", "worker"]
    try:
        atomic_env(ENV_PATH, updated)
        run(recreate)
        if not worker_matches(values):
            raise ValueError("Worker provider settings could not be verified")
    except Exception:
        atomic_env(ENV_PATH, previous)
        run(recreate)
        raise
    return "Provider settings applied and verified in worker"


def main():
    lock_dir = Path("/run/finbot-tradingagents")
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    if lock_dir.is_symlink() or lock_dir.stat().st_uid != 0 or lock_dir.stat().st_mode & 0o077:
        raise ValueError("Unsafe deployment lock directory")
    with (lock_dir / "deploy.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Provider sync deferred while deployment holds the lock")
            return
        print(sync())


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        sys.exit(f"Provider sync failed ({type(error).__name__}); private output suppressed")
