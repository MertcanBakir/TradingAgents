"""Deployment acceptance with fake Docker/AWS; never touches a live service."""

import copy
import hashlib
import io
import json
import re
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from deploy.personal import cd_pipeline as pipeline, deploy_host as host, deploy_request

SHA = "a" * 40
OLD = host.REPOSITORY + "@sha256:" + "1" * 64
DIGEST = "sha256:" + "2" * 64
NEW = host.REPOSITORY + "@" + DIGEST


def candidate():
    run = {"repository": {"full_name": pipeline.REPOSITORY},
           "head_repository": {"full_name": pipeline.REPOSITORY},
           "workflow_id": 10, "event": "push", "head_branch": "main",
           "status": "completed", "conclusion": "success", "head_sha": SHA,
           "id": 100, "run_attempt": 2}
    artifact = {"name": f"worker-image-{SHA}-2", "expired": False}
    return run, artifact, [{"name": "CI passed", "conclusion": "success"}]


def test_candidate_binds_image_to_exact_successful_main_run_and_attempt():
    run, artifact, jobs = candidate()
    result = pipeline.validate_run(run, 10, SHA, [artifact], jobs)
    assert result["artifact"] == artifact["name"]
    assert result["tag"] == f"git-{SHA}-run-100-2"
    assert pipeline.validate_run(run, 10, "b" * 40, [artifact], jobs) is None


@pytest.mark.parametrize("field,value", [("event", "pull_request"), ("event", "workflow_dispatch"),
    ("head_branch", "feature"), ("workflow_id", 11), ("conclusion", "failure"),
    ("status", "in_progress"), ("head_sha", "invalid")])
def test_untrusted_or_incomplete_runs_cannot_request_aws_access(field, value):
    run, artifact, jobs = candidate()
    run[field] = value
    with pytest.raises(ValueError):
        pipeline.validate_run(run, 10, SHA, [artifact], jobs)


def test_fork_artifact_failed_gate_expired_duplicate_and_previous_attempt_are_rejected():
    run, artifact, jobs = candidate()
    fork = copy.deepcopy(run)
    fork["head_repository"]["full_name"] = "someone/TradingAgents"
    with pytest.raises(ValueError):
        pipeline.validate_run(fork, 10, SHA, [artifact], jobs)
    for artifacts, checks in [([artifact], []), ([artifact], [{"name": "CI passed", "conclusion": "skipped"}]),
                             ([{**artifact, "expired": True}], jobs), ([artifact, artifact], jobs),
                             ([{**artifact, "name": f"worker-image-{SHA}-1"}], jobs)]:
        with pytest.raises(ValueError):
            pipeline.validate_run(run, 10, SHA, artifacts, checks)


def test_archive_checksum_and_provenance_are_checked_before_docker_load(tmp_path, monkeypatch):
    archive = tmp_path / "image.tar.gz"
    config = json.dumps({"architecture": "arm64", "os": "linux",
                         "config": {"Labels": {"org.opencontainers.image.revision": SHA}}}).encode()
    with tarfile.open(archive, "w:gz") as bundle:
        for name, raw in [("manifest.json", json.dumps([{"RepoTags": [pipeline.IMAGE], "Config": "config.json"}]).encode()), ("config.json", config)]:
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            bundle.addfile(member, io.BytesIO(raw))
    original_archive = archive.read_bytes()
    manifest = {"sha": SHA, "run_id": "100", "attempt": "2", "config_digest": "sha256:" + hashlib.sha256(config).hexdigest(),
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    for name, value in (("CI_SHA", SHA), ("CI_RUN_ID", "100"), ("CI_ATTEMPT", "2")):
        monkeypatch.setenv(name, value)
    assert pipeline.verify_package(tmp_path) == manifest
    archive.write_bytes(b"changed archive")
    with pytest.raises(ValueError, match="checksum"):
        pipeline.verify_package(tmp_path)
    archive.write_bytes(original_archive)
    monkeypatch.setenv("CI_ATTEMPT", "3")
    with pytest.raises(ValueError, match="provenance"):
        pipeline.verify_package(tmp_path)


def test_ecr_configuration_verification_supports_manifests_and_arm64_indexes():
    expected = "sha256:" + "3" * 64
    direct = {"config": {"digest": expected}}
    assert pipeline.published_config(direct, lambda _: pytest.fail("No index lookup needed")) == expected
    index = {"manifests": [{"digest": "sha256:" + "4" * 64,
                            "platform": {"os": "linux", "architecture": "arm64"}},
                           {"digest": "sha256:" + "5" * 64,
                            "platform": {"os": "unknown", "architecture": "unknown"}}]}
    looked_up = []
    def fetch(reference):
        looked_up.append(reference)
        return direct
    assert pipeline.published_config(index, fetch) == expected
    assert looked_up == ["imageDigest=sha256:" + "4" * 64]
    index["manifests"][0]["platform"]["architecture"] = "amd64"
    with pytest.raises(ValueError):
        pipeline.published_config(index, fetch)


def test_an_existing_ecr_tag_cannot_substitute_another_image(tmp_path, monkeypatch):
    (tmp_path / "manifest.json").write_text(json.dumps({"config_digest": "sha256:" + "3" * 64}))
    monkeypatch.setenv("IMAGE_TAG", "git-test-run-100-2")
    reply = {"images": [{"imageManifest": json.dumps({"config": {"digest": "sha256:" + "4" * 64}})}]}
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: json.dumps(reply))
    with pytest.raises(ValueError, match="differs from the CI-tested"):
        pipeline.verify_published(tmp_path)


class Docker:
    def __init__(self, root):
        self.root = root
        self.image = OLD
        self.updates = []
        self.bad_catalog = False
        self.stale_catalog = False
        self.pull_failure = False
        self.architecture = "arm64"
        self.revision = SHA
        self.ticks = 0

    def clock(self):
        self.ticks += 1
        return self.ticks

    def __call__(self, args, **kwargs):
        if args[:4] == ["aws", "ecr", "get-login-password", "--region"]:
            return "SYNTHETIC_ECR_PASSWORD"
        if args[:2] == ["docker", "login"]:
            assert kwargs["input_text"] == "SYNTHETIC_ECR_PASSWORD"
            return ""
        if args[:2] == ["docker", "pull"]:
            if self.pull_failure:
                raise host.DeploymentError("Pull failed")
            return ""
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Os": "linux", "Architecture": self.architecture,
                "Config": {"Labels": {"org.opencontainers.image.revision": self.revision}}}])
        if args[:2] == ["docker", "inspect"]:
            name = args[2]
            return json.dumps([{"Id": name, "Config": {"Image": self.image}, "State": {"Status": "running"}}])
        if args[:2] == ["docker", "exec"]:
            bad = self.image == NEW and self.bad_catalog
            stale = self.image == NEW and self.stale_catalog
            return json.dumps({"ok": True, "clock": 100, "refreshed": 90 if stale else 101,
                               "models": 0 if bad else 7, "active_jobs": 1})
        if args[:2] == ["docker", "compose"]:
            if "ps" in args:
                return args[-1]
            if "up" in args:
                assert args[-3:] == ["api", "worker", "maintenance"]
                assert "--no-deps" in args and "--no-build" in args
                self.image = host.image_setting((self.root / ".env").read_text())
                self.updates.append(self.image)
            else:
                assert args[-2:] == ["config", "--quiet"]
            return ""
        pytest.fail(f"Unexpected command: {args}")


@pytest.fixture
def deployment(tmp_path):
    env = tmp_path / ".env"
    original = f"# private runtime\nLUCRARI_IMAGE={OLD}\nPRIVATE_VALUE=FAKE_SECRET\n"
    env.write_text(original)
    env.chmod(0o600)
    docker = Docker(tmp_path)
    config = {"directory": str(tmp_path), "backup_directory": str(tmp_path / "backups"),
              "health_url": "https://example.invalid/healthz", "health_timeout": 10}
    deploy = host.Deployment(config, docker, clock=docker.clock, sleep=lambda _: None, health=lambda: True)
    return deploy, docker, env, original


def test_deploy_preserves_private_settings_permissions_and_caddy(deployment):
    deploy, docker, env, original = deployment
    result = deploy.deploy(DIGEST, SHA)
    assert result["status"] == "deployed" and result["active_jobs_before"] == 1
    assert env.read_text() == original.replace(OLD, NEW)
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    backups = list(env.parent.joinpath("backups").glob("*.env"))
    assert len(backups) == 1 and backups[0].read_text() == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert docker.updates == [NEW]
    assert deploy.container("edge")["Id"] == "edge"


@pytest.mark.parametrize("failure", ["bad_catalog", "stale_catalog", "https"])
def test_failed_readiness_restores_and_verifies_previous_image(deployment, failure):
    deploy, docker, env, original = deployment
    if failure == "https":
        deploy.health = lambda: docker.image == OLD
    else:
        setattr(docker, failure, True)
    with pytest.raises(host.DeploymentError, match="previous image restored and verified"):
        deploy.deploy(DIGEST, SHA)
    assert env.read_text() == original
    assert docker.updates == [NEW, OLD]


def test_failed_pull_does_not_change_runtime_or_create_backup(deployment):
    deploy, docker, env, original = deployment
    docker.pull_failure = True
    with pytest.raises(host.DeploymentError, match="Pull failed"):
        deploy.deploy(DIGEST, SHA)
    assert env.read_text() == original and docker.updates == []
    assert not env.parent.joinpath("backups").exists()


def test_insecure_backup_directory_never_receives_private_env(deployment):
    deploy, docker, env, original = deployment
    directory = env.parent / "backups"
    directory.mkdir(mode=0o755)
    with pytest.raises(host.DeploymentError, match="Unsafe backup"):
        deploy.deploy(DIGEST, SHA)
    assert list(directory.iterdir()) == []
    assert env.read_text() == original and docker.updates == []


def test_running_configuration_drift_stops_before_any_update(deployment):
    deploy, docker, env, original = deployment
    docker.image = host.REPOSITORY + "@sha256:" + "9" * 64
    with pytest.raises(host.DeploymentError, match="disagree"):
        deploy.deploy(DIGEST, SHA)
    assert env.read_text() == original and docker.updates == []


@pytest.mark.parametrize("field,value", [("architecture", "amd64"), ("revision", "b" * 40)])
def test_wrong_architecture_or_revision_never_changes_runtime(deployment, field, value):
    deploy, docker, env, original = deployment
    setattr(docker, field, value)
    with pytest.raises(host.DeploymentError, match="architecture or source"):
        deploy.deploy(DIGEST, SHA)
    assert env.read_text() == original and not docker.updates


def test_rollback_failure_is_not_reported_as_success(deployment):
    deploy, docker, env, original = deployment
    deploy.health = lambda: False
    with pytest.raises(host.DeploymentError, match="Deployment and rollback failed"):
        deploy.deploy(DIGEST, SHA)
    assert env.read_text() == original
    assert docker.updates == [NEW, OLD]


def test_unsafe_env_and_invalid_parameters_are_rejected_before_commands(deployment):
    deploy, docker, env, original = deployment
    with pytest.raises(host.DeploymentError, match="Invalid"):
        deploy.deploy("sha256:bad;touch /tmp/unsafe", SHA)
    env.chmod(0o644)
    with pytest.raises(host.DeploymentError, match="permissions"):
        deploy.deploy(DIGEST, SHA)
    assert docker.updates == []


def test_repeated_deploy_is_a_verified_noop(deployment):
    deploy, docker, env, _ = deployment
    deploy.deploy(DIGEST, SHA)
    assert deploy.deploy(DIGEST, SHA)["status"] == "already_current"
    assert docker.updates == [NEW]
    assert len(list(env.parent.joinpath("backups").glob("*.env"))) == 1


def test_subprocess_failures_never_expose_private_output(tmp_path, monkeypatch):
    def failure(*args, **kwargs):
        return subprocess.CompletedProcess([], 1, "FAKE_SECRET", "FAKE_SECRET")
    monkeypatch.setattr(subprocess, "run", failure)
    with pytest.raises(host.DeploymentError) as caught:
        host.Commands(tmp_path, str(tmp_path))(["docker", "compose", "config"])
    assert "FAKE_SECRET" not in str(caught.value)


def test_ssm_wait_handles_eventual_consistency_and_checks_helper_confirmation():
    responses = iter([None, {"Status": "InProgress"},
                      {"Status": "Success", "ResponseCode": 0,
                       "StandardOutputContent": '{"status":"deployed"}'}])
    result = deploy_request.wait("command", invoke=lambda *a: next(responses), sleep=lambda _: None)
    assert result["status"] == "deployed"
    for response in [{"Status": "Failed", "ResponseCode": 1},
                     {"Status": "Success", "ResponseCode": 0, "StandardOutputContent": '{"status":"failed"}'}]:
        with pytest.raises(RuntimeError):
            deploy_request.wait("command", invoke=lambda *a, response=response: response, sleep=lambda _: None)


def test_ssm_arguments_cannot_inject_shell_commands():
    assert deploy_request.parameters(DIGEST, SHA, "c" * 64)["CommitSha"] == [SHA]
    document = json.loads((Path(__file__).resolve().parents[1] / "deploy/personal/ssm-deploy.json").read_text())
    for name, value in (("ImageDigest", DIGEST), ("CommitSha", SHA), ("ScriptSha256", "c" * 64)):
        pattern = document["parameters"][name]["allowedPattern"]
        assert re.fullmatch(pattern, value)
        assert not re.fullmatch(pattern, value + ";id")
        assert not re.fullmatch(pattern, value + "'\n")
    for values in [(DIGEST + ";id", SHA, "c" * 64), (DIGEST, SHA + "'", "c" * 64)]:
        with pytest.raises(ValueError):
            deploy_request.parameters(*values)
