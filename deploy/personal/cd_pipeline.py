"""Validate the trusted CI run and its image before any AWS credentials are used."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
import urllib.request
from pathlib import Path

REPOSITORY = "MertcanBakir/TradingAgents"
REGISTRY = "500888846202.dkr.ecr.eu-west-1.amazonaws.com"
ECR_REPOSITORY = "lucrari/tradingagents-personal"
REGION = "eu-west-1"
INSTANCE = "i-0cc2d47020ed7347c"
DOCUMENT = "FinBotDeployTradingAgents"
IMAGE = "tradingagents-worker:ci"


def github(path):
    request = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                 "Accept": "application/vnd.github+json", "User-Agent": "FinBot-CD"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def validate_run(run, workflow_id, head_sha, artifacts, jobs):
    """A workflow_run can originate from a PR: never trust the event alone."""
    if not (
        run["repository"]["full_name"] == REPOSITORY
        and run["head_repository"]["full_name"] == REPOSITORY
        and run["workflow_id"] == workflow_id
        and run["event"] == "push" and run["head_branch"] == "main"
        and run["status"] == "completed" and run["conclusion"] == "success"
        and re.fullmatch(r"[0-9a-f]{40}", run["head_sha"])
    ):
        raise ValueError("CI run is not a successful trusted main push")
    if not any(j["name"] == "CI passed" and j["conclusion"] == "success" for j in jobs):
        raise ValueError("The aggregate CI gate did not succeed")
    if run["head_sha"] != head_sha:
        return None  # An older successful run must not replace a newer main.
    name = f"worker-image-{run['head_sha']}-{run['run_attempt']}"
    matches = [a for a in artifacts if a["name"] == name and not a["expired"]]
    if len(matches) != 1:
        raise ValueError("Expected exactly one unexpired image from this CI attempt")
    return {"sha": run["head_sha"], "run_id": str(run["id"]),
            "attempt": str(run["run_attempt"]), "artifact": name,
            "tag": f"git-{run['head_sha']}-run-{run['id']}-{run['run_attempt']}"}


def prepare(run_id):
    if not re.fullmatch(r"[0-9]+", run_id):
        raise ValueError("Invalid CI run ID")
    base = f"/repos/{REPOSITORY}"
    run = github(f"{base}/actions/runs/{run_id}")
    workflow = github(f"{base}/actions/workflows/ci.yml")
    branch = github(f"{base}/git/ref/heads/main")
    artifacts = github(f"{base}/actions/runs/{run_id}/artifacts?per_page=100")["artifacts"]
    jobs = github(f"{base}/actions/runs/{run_id}/attempts/{run['run_attempt']}/jobs?per_page=100")["jobs"]
    result = validate_run(run, workflow["id"], branch["object"]["sha"], artifacts, jobs)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"eligible={'true' if result else 'false'}\n")
        for key, value in (result or {}).items():
            output.write(f"{key}={value}\n")
    if result is None:
        print("Skipped: a newer commit is already on main.")


def checksum(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def saved_config(archive):
    # Docker's inspect Id can mean a config or index digest depending on its
    # image store. The saved config blob is portable across both implementations.
    with tarfile.open(archive, "r:gz") as bundle:
        entries = json.load(bundle.extractfile("manifest.json"))
        matches = [entry for entry in entries if IMAGE in (entry.get("RepoTags") or [])]
        if len(matches) != 1:
            raise ValueError("Archive must contain exactly one candidate image tag")
        raw = bundle.extractfile(matches[0]["Config"]).read()
    config = json.loads(raw)
    if config.get("architecture") != "arm64" or config.get("os") != "linux":
        raise ValueError("Archive is not a Linux ARM64 image")
    return "sha256:" + hashlib.sha256(raw).hexdigest(), config


def package(directory):
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    archive = target / "image.tar.gz"
    # Streaming keeps the ARM runner's memory usage bounded.
    with archive.open("wb") as stream:
        save = subprocess.Popen(["docker", "save", IMAGE], stdout=subprocess.PIPE)
        try:
            compressed = subprocess.run(["gzip", "-1"], stdin=save.stdout, stdout=stream, check=False)
            save.stdout.close()
            if save.wait() or compressed.returncode:
                raise RuntimeError("Cannot export the tested image")
        finally:
            if save.poll() is None:
                save.kill()
                save.wait()
    config_digest, config = saved_config(archive)
    if config["config"]["Labels"].get("org.opencontainers.image.revision") != os.environ["GITHUB_SHA"]:
        raise ValueError("Archive source label does not match CI")
    (target / "manifest.json").write_text(json.dumps({
        "sha": os.environ["GITHUB_SHA"], "run_id": os.environ["GITHUB_RUN_ID"],
        "attempt": os.environ["GITHUB_RUN_ATTEMPT"], "config_digest": config_digest,
        "archive_sha256": checksum(archive),
    }))


def verify_package(directory):
    target = Path(directory)
    manifest = json.loads((target / "manifest.json").read_text())
    for key, variable in (("sha", "CI_SHA"), ("run_id", "CI_RUN_ID"), ("attempt", "CI_ATTEMPT")):
        if manifest[key] != os.environ[variable]:
            raise ValueError("Image provenance does not match the validated CI run")
    if checksum(target / "image.tar.gz") != manifest["archive_sha256"]:
        raise ValueError("Image archive checksum mismatch")
    digest, config = saved_config(target / "image.tar.gz")
    if digest != manifest["config_digest"] or config["config"]["Labels"].get("org.opencontainers.image.revision") != manifest["sha"]:
        raise ValueError("Archive configuration does not match its provenance")
    return manifest


def load(directory):
    manifest = verify_package(directory)
    subprocess.run(["docker", "load", "--input", str(Path(directory) / "image.tar.gz")], check=True)
    info = json.loads(subprocess.check_output(["docker", "image", "inspect", IMAGE], text=True))[0]
    _, config = saved_config(Path(directory) / "image.tar.gz")
    if (info["RootFS"]["Layers"] != config["rootfs"]["diff_ids"] or info["Architecture"] != "arm64"
            or info["Os"] != "linux"
            or info["Config"]["Labels"].get("org.opencontainers.image.revision") != manifest["sha"]):
        raise ValueError("Loaded image does not match the tested ARM64 image")
    for key in ("Env", "Entrypoint", "Cmd", "User", "WorkingDir", "Labels", "Healthcheck", "StopSignal"):
        if info["Config"].get(key) != config["config"].get(key):
            raise ValueError("Loaded execution configuration differs from the tested image")


def published_config(manifest, fetch):
    if "config" in manifest:
        return manifest["config"]["digest"]
    candidates = [item for item in manifest.get("manifests", [])
                  if item.get("platform", {}).get("os") == "linux"
                  and item.get("platform", {}).get("architecture") == "arm64"]
    if len(candidates) != 1:
        raise ValueError("ECR index must contain exactly one Linux ARM64 image")
    return fetch("imageDigest=" + candidates[0]["digest"])["config"]["digest"]


def verify_published(directory):
    manifest = json.loads(Path(directory, "manifest.json").read_text())

    def fetch(reference):
        raw = subprocess.check_output([
            "aws", "ecr", "batch-get-image", "--region", REGION,
            "--repository-name", ECR_REPOSITORY, "--image-ids", reference, "--output", "json",
        ], text=True)
        images = json.loads(raw)["images"]
        if len(images) != 1:
            raise ValueError("ECR did not return the published image")
        return json.loads(images[0]["imageManifest"])

    actual = published_config(fetch("imageTag=" + os.environ["IMAGE_TAG"]), fetch)
    if actual != manifest["config_digest"]:
        raise ValueError("Published ECR image differs from the CI-tested configuration")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "package", "load", "current", "published"])
    parser.add_argument("value", nargs="?", default="runtime-image")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.value)
    elif args.action == "package":
        package(args.value)
    elif args.action == "load":
        load(args.value)
    elif args.action == "published":
        verify_published(args.value)
    elif github(f"/repos/{REPOSITORY}/git/ref/heads/main")["object"]["sha"] != args.value:
        raise ValueError("Refusing to publish or deploy a superseded main commit")


if __name__ == "__main__":
    main()
