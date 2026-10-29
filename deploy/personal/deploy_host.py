"""Root-owned EC2 deploy helper. Changes only the three backend services.

Install under /usr/local/libexec/finbot-tradingagents; configuration lives in /etc.
Never print subprocess output: it can include runtime configuration or credentials.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

SERVICES = ("api", "worker", "maintenance")
REPOSITORY = "500888846202.dkr.ecr.eu-west-1.amazonaws.com/lucrari/tradingagents-personal"
PROBE = """
import json
try:
    from tradingagents.service.backend.runtime import settings_from_env
    from tradingagents.service.backend.repository import Repository
    repo = Repository(settings_from_env())
    models = repo.provider_models('chatgpt')
    with repo.transaction() as conn:
        clock = conn.execute('SELECT EXTRACT(EPOCH FROM clock_timestamp()) AS value').fetchone()['value']
        row = conn.execute("SELECT EXTRACT(EPOCH FROM refreshed_at) AS value FROM analysis_backend.provider_model_catalog WHERE provider='chatgpt'").fetchone()
        active = conn.execute("SELECT count(*) AS value FROM analysis_backend.jobs WHERE status IN ('reserved','running')").fetchone()['value']
    print(json.dumps({'ok': True, 'clock': float(clock), 'refreshed': float(row['value']) if row else 0, 'models': len(models), 'active_jobs': active}))
except Exception:
    print(json.dumps({'ok': False}))
"""


class DeploymentError(RuntimeError):
    pass


def image_setting(text):
    matches = re.findall(r"^LUCRARI_IMAGE=(.*)$", text, re.MULTILINE)
    if len(matches) != 1:
        raise DeploymentError("Expected exactly one LUCRARI_IMAGE setting")
    value = matches[0].strip().strip("\"'")
    if not re.fullmatch(re.escape(REPOSITORY) + r"@sha256:[0-9a-f]{64}", value):
        raise DeploymentError("Current image must be an immutable digest in the expected repository")
    return value


def update_image(text, image):
    image_setting(text)
    return re.sub(r"^LUCRARI_IMAGE=.*$", "LUCRARI_IMAGE=" + image, text, flags=re.MULTILINE)


def atomic_env(path, text):
    info = path.stat()
    fd, name = tempfile.mkstemp(prefix=".env-deploy-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
            os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class Commands:
    def __init__(self, directory, docker_config):
        self.directory = directory
        self.environment = {**os.environ, "DOCKER_CONFIG": docker_config}

    def __call__(self, args, *, input_text=None):
        try:
            result = subprocess.run(args, cwd=self.directory, env=self.environment,
                                    input=input_text, text=True, capture_output=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired):
            raise DeploymentError("Command unavailable or timed out") from None
        if result.returncode:
            raise DeploymentError("Command failed; private output suppressed")
        return result.stdout.strip()


class Deployment:
    def __init__(self, config, run, *, clock=time.monotonic, sleep=time.sleep, health=None):
        self.config, self.run = config, run
        self.clock, self.sleep = clock, sleep
        self.health = health or self.https_health
        self.base = Path(config["directory"])
        self.env_path = self.base / ".env"
        self.compose = ["docker", "compose", "--project-name", "lucrari-personal",
                        "--env-file", str(self.env_path), "-f", str(self.base / "compose.personal.yml")]

    def https_health(self):
        # TLS certificate verification stays enabled, including IP SAN checking.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(self.config["health_url"], timeout=10) as response:
            return json.load(response) == {"status": "ok"}

    def container(self, service):
        cid = self.run([*self.compose, "ps", "-q", service])
        if not cid or "\n" in cid:
            raise DeploymentError("Expected one running container per service")
        return json.loads(self.run(["docker", "inspect", cid]))[0]

    def probe(self):
        cid = self.container("api")["Id"]
        return json.loads(self.run(["docker", "exec", cid, "python", "-c", PROBE]))

    def verify_image(self, image, commit):
        info = json.loads(self.run(["docker", "image", "inspect", image]))[0]
        if (info["Architecture"] != "arm64" or info["Os"] != "linux"
                or (info["Config"].get("Labels") or {}).get("org.opencontainers.image.revision") != commit):
            raise DeploymentError("Image architecture or source revision mismatch")

    def verify(self, image, edge_id, refreshed_after):
        end = self.clock() + self.config.get("health_timeout", 180)
        stable = 0
        while self.clock() < end:
            try:
                if self.container("edge")["Id"] != edge_id:
                    raise DeploymentError("Caddy container changed unexpectedly")
                for service in SERVICES:
                    info = self.container(service)
                    if info["Config"]["Image"] != image or info["State"]["Status"] != "running":
                        raise DeploymentError("Backend image or service state mismatch")
                probe = self.probe()
                ready = (self.health() and probe.get("ok") and probe["models"] > 0
                         and probe["refreshed"] >= refreshed_after)
                stable = stable + 1 if ready else 0
                if stable >= 2:
                    return
            except (DeploymentError, OSError, ValueError):
                stable = 0
            self.sleep(3)
        raise DeploymentError("Readiness failed: services, HTTPS or fresh model catalog unavailable")

    def deploy(self, digest, commit):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise DeploymentError("Invalid digest or commit")
        if self.env_path.is_symlink() or stat.S_IMODE(self.env_path.stat().st_mode) & 0o077:
            raise DeploymentError("Unsafe environment file permissions")
        previous_text = self.env_path.read_text()
        previous = image_setting(previous_text)
        image = REPOSITORY + "@" + digest
        self.run([*self.compose, "config", "--quiet"])
        edge = self.container("edge")["Id"]
        for service in SERVICES:
            if self.container(service)["Config"]["Image"] != previous:
                raise DeploymentError("Running containers and environment file disagree")
        before = self.probe()
        if not before.get("ok"):
            raise DeploymentError("Database preflight failed")
        if previous == image:
            self.verify_image(image, commit)
            self.verify(image, edge, 0)
            return {"status": "already_current", "image": image}
        password = self.run(["aws", "ecr", "get-login-password", "--region", "eu-west-1"])
        self.run(["docker", "login", "--username", "AWS", "--password-stdin", REPOSITORY.split("/")[0]], input_text=password)
        self.run(["docker", "pull", image])
        self.verify_image(image, commit)
        backup_dir = Path(self.config["backup_directory"])
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if backup_dir.is_symlink() or stat.S_IMODE(backup_dir.stat().st_mode) & 0o077:
            raise DeploymentError("Unsafe backup directory")
        backup = backup_dir / f"{time.time_ns()}-{commit[:12]}.env"
        with backup.open("x") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(previous_text)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            atomic_env(self.env_path, update_image(previous_text, image))
            self.run([*self.compose, "up", "--detach", "--no-deps", "--no-build", "--force-recreate", *SERVICES])
            self.verify(image, edge, before["clock"])
        except Exception:
            atomic_env(self.env_path, previous_text)
            try:
                self.run([*self.compose, "up", "--detach", "--no-deps", "--no-build", "--force-recreate", *SERVICES])
                self.verify(previous, edge, 0)
            except Exception:
                raise DeploymentError("Deployment and rollback failed; private backup retained") from None
            raise DeploymentError("Deployment failed; previous image restored and verified") from None
        return {"status": "deployed", "image": image, "commit": commit,
                "previous_image": previous, "active_jobs_before": before["active_jobs"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise DeploymentError("Run only through the root-owned SSM deployment document")
    config_file = Path("/etc/finbot-tradingagents/cd-config.json")
    if config_file.is_symlink() or config_file.stat().st_uid != 0 or stat.S_IMODE(config_file.stat().st_mode) & 0o022:
        raise DeploymentError("Unsafe deployment configuration")
    config = json.loads(config_file.read_text())
    lock_directory = Path("/run/finbot-tradingagents")
    lock_directory.mkdir(mode=0o700, exist_ok=True)
    if lock_directory.is_symlink() or lock_directory.stat().st_uid != 0 or stat.S_IMODE(lock_directory.stat().st_mode) & 0o077:
        raise DeploymentError("Unsafe deployment lock directory")
    with (lock_directory / "deploy.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeploymentError("Another server deployment is still running") from None
        def interrupted(signum, frame):
            raise DeploymentError("Deployment interrupted")
        signal.signal(signal.SIGTERM, interrupted)
        with tempfile.TemporaryDirectory(prefix="finbot-ecr-") as credentials:
            result = Deployment(config, Commands(config["directory"], credentials)).deploy(args.digest, args.commit)
            print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except DeploymentError as error:
        print(json.dumps({"status": "failed", "reason": str(error)}))
        raise SystemExit(1) from None
